"""Thin async client for ``POST {base_url}/chat/completions``; no vendor SDK.

Everything provider-specific lives in :class:`app.config.LLMRole` (structured output mode,
llama.cpp's ``chat_template_kwargs``, auth), so llama.cpp and cloud providers share this code.
API keys never reach the logs: errors are passed through :func:`redact`.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import LLMRole, StructuredOutput

logger = logging.getLogger(__name__)

Message = dict[str, str]


class _Inflight:
    """Process-wide cap on concurrent LLM requests (``summaries.max_inflight``), shared by
    every client, kind and thread."""

    def __init__(self, limit: int = 1) -> None:
        self._lock = threading.Lock()
        self.limit = limit
        self.active = 0
        self.peak = 0

    def set_limit(self, limit: int) -> None:
        with self._lock:
            self.limit = max(1, limit)

    def _try(self) -> bool:
        with self._lock:
            if self.active >= self.limit:
                return False
            self.active += 1
            self.peak = max(self.peak, self.active)
            return True

    async def acquire(self) -> None:
        while not self._try():
            await asyncio.sleep(0.02)

    def release(self) -> None:
        with self._lock:
            self.active -= 1


INFLIGHT = _Inflight()


def set_max_inflight(limit: int) -> None:
    INFLIGHT.set_limit(limit)


class LLMError(Exception):
    """The endpoint failed: HTTP error, timeout, unreachable or an unusable response."""

    kind = "error"


class LLMHTTPError(LLMError):
    kind = "http"

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMTimeout(LLMError):
    kind = "timeout"


class LLMConnectionError(LLMError):
    kind = "connection"


class LLMBadResponse(LLMError):
    kind = "bad_response"


@dataclass
class ChatResult:
    content: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    reasoning_length: int = 0
    """Characters of ``reasoning_content`` (thinking) the model returned."""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def tokens_per_second(self) -> float | None:
        if not self.completion_tokens or self.latency_ms <= 0:
            return None
        return self.completion_tokens / (self.latency_ms / 1000)


def redact(text: str, *secrets: str | None) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _api_key(role: LLMRole) -> str | None:
    return role.api_key.get_secret_value() if role.api_key else None


def chat_url(role: LLMRole) -> str:
    return str(role.base_url).rstrip("/") + "/chat/completions"


def build_headers(role: LLMRole) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key := _api_key(role):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def build_payload(
    role: LLMRole,
    messages: Sequence[Message],
    *,
    json_schema: dict[str, Any] | None = None,
    schema_name: str = "response",
    structured_output: StructuredOutput | None = None,
) -> dict[str, Any]:
    mode = structured_output or role.structured_output
    payload: dict[str, Any] = {
        "model": role.model,
        "messages": list(messages),
        "temperature": role.temperature,
        "max_tokens": role.max_tokens,
        "stream": False,
    }
    if json_schema is not None:
        if mode is StructuredOutput.JSON_SCHEMA:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": json_schema},
            }
        elif mode is StructuredOutput.JSON_OBJECT:
            payload["response_format"] = {"type": "json_object"}
    if role.disable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content parts
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def parse_response(data: Any, *, latency_ms: int = 0, default_model: str = "") -> ChatResult:
    """Parse a non-streaming chat/completions response body."""
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError, AttributeError):
        raise LLMBadResponse("response has no choices[0].message") from None
    usage = data.get("usage") or {}
    return ChatResult(
        content=_content_text(message.get("content")).strip(),
        model=str(data.get("model") or default_model),
        latency_ms=latency_ms,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        finish_reason=choice.get("finish_reason"),
        reasoning_length=len(
            _content_text(message.get("reasoning_content") or message.get("reasoning"))
        ),
        raw=data,
    )


class LLMClient:
    def __init__(self, role: LLMRole, *, transport: httpx.AsyncBaseTransport | None = None):
        self.role = role
        self.transport = transport

    @property
    def model(self) -> str:
        return self.role.model

    @property
    def url(self) -> str:
        return chat_url(self.role)

    @property
    def endpoint_host(self) -> str:
        """Host (and port) of the endpoint, for attempt logs; never the key."""
        return httpx.URL(str(self.role.base_url)).netloc.decode()

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response",
        structured_output: StructuredOutput | None = None,
    ) -> ChatResult:
        payload = build_payload(
            self.role,
            messages,
            json_schema=json_schema,
            schema_name=schema_name,
            structured_output=structured_output,
        )
        key = _api_key(self.role)
        logger.debug("POST %s model=%s", self.url, self.role.model)
        await INFLIGHT.acquire()
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=httpx.Timeout(self.role.timeout_seconds)
            ) as client:
                response = await client.post(
                    self.url, json=payload, headers=build_headers(self.role)
                )
        except httpx.TimeoutException as exc:
            raise self._fail(
                LLMTimeout(f"timed out after {self.role.timeout_seconds:g}s"), key
            ) from exc
        except httpx.HTTPError as exc:
            raise self._fail(LLMConnectionError(f"{type(exc).__name__}: {exc}"), key) from exc
        finally:
            INFLIGHT.release()
        latency_ms = round((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            body = " ".join(response.text.split())[:300]
            raise self._fail(
                LLMHTTPError(response.status_code, f"HTTP {response.status_code}: {body}"), key
            )
        try:
            data = response.json()
        except ValueError:
            raise self._fail(LLMBadResponse("response is not JSON"), key) from None
        result = parse_response(data, latency_ms=latency_ms, default_model=self.role.model)
        if self.role.disable_thinking and result.reasoning_length:
            logger.warning(
                "%s returned %d characters of reasoning although disable_thinking is set; "
                "the server may ignore chat_template_kwargs",
                self.endpoint_host,
                result.reasoning_length,
            )
        return result

    def _fail(self, error: LLMError, key: str | None) -> LLMError:
        error.args = (redact(str(error), key),)
        logger.warning("LLM request to %s failed: %s", self.url, error)
        return error
