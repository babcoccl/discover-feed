"""Thin async client for ``POST {base_url}/chat/completions``; no vendor SDK.

Everything provider-specific lives in :class:`app.config.LLMRole` (structured output mode,
llama.cpp's ``chat_template_kwargs``, auth), so llama.cpp and cloud providers share this code.
API keys never reach the logs: errors are passed through :func:`redact`.
"""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import LLMRole, StructuredOutput

logger = logging.getLogger(__name__)

Message = dict[str, str]


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
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": role.model,
        "messages": list(messages),
        "temperature": role.temperature,
        "max_tokens": role.max_tokens,
        "stream": False,
    }
    if json_schema is not None:
        if role.structured_output is StructuredOutput.JSON_SCHEMA:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": json_schema},
            }
        elif role.structured_output is StructuredOutput.JSON_OBJECT:
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

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response",
    ) -> ChatResult:
        payload = build_payload(
            self.role, messages, json_schema=json_schema, schema_name=schema_name
        )
        key = _api_key(self.role)
        logger.debug("POST %s model=%s", self.url, self.role.model)
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
        return parse_response(data, latency_ms=latency_ms, default_model=self.role.model)

    def _fail(self, error: LLMError, key: str | None) -> LLMError:
        error.args = (redact(str(error), key),)
        logger.warning("LLM request to %s failed: %s", self.url, error)
        return error
