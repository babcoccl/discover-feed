"""Thin async client for ``POST {base_url}/chat/completions``; no vendor SDK.

Everything provider-specific lives in :class:`app.config.LLMRole` (structured output mode,
llama.cpp's ``chat_template_kwargs``, auth), so llama.cpp and cloud providers share this code.
API keys never reach the logs: errors are passed through :func:`redact`.
"""

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import LLMRole, StructuredOutput

logger = logging.getLogger(__name__)

Message = dict[str, str]


class _Inflight:
    """Process-wide cap on concurrent LLM requests (``summaries.max_inflight``), shared by
    every client, kind and thread.

    Interactive chat has priority (``summaries.chat_priority``): while a chat request is
    waiting for a slot or streaming (:meth:`chat_begin` .. :meth:`chat_end`), background
    requests do not start (one already running is never aborted) and the chat takes the
    next free slot."""

    def __init__(self, limit: int = 1) -> None:
        self._lock = threading.Lock()
        self.limit = limit
        self.active = 0
        self.peak = 0
        self.chat_priority = True
        self.chat_demand = 0
        """Chat requests waiting for a slot or streaming."""

    def set_limit(self, limit: int) -> None:
        with self._lock:
            self.limit = max(1, limit)

    def _try(self, priority: bool = False) -> bool:
        with self._lock:
            if self.active >= self.limit:
                return False
            if not priority and self.chat_priority and self.chat_demand > 0:
                return False
            self.active += 1
            self.peak = max(self.peak, self.active)
            return True

    async def acquire(self, priority: bool = False) -> None:
        while not self._try(priority):
            await asyncio.sleep(0.02)

    def chat_begin(self) -> None:
        with self._lock:
            self.chat_demand += 1

    def chat_end(self) -> None:
        with self._lock:
            self.chat_demand = max(0, self.chat_demand - 1)

    @property
    def chat_active(self) -> bool:
        return self.chat_priority and self.chat_demand > 0

    async def wait_for_chat(self) -> None:
        """Background work: wait while a chat request has priority."""
        while self.chat_active:
            await asyncio.sleep(0.05)

    def release(self) -> None:
        with self._lock:
            self.active -= 1


INFLIGHT = _Inflight()


def set_max_inflight(limit: int) -> None:
    INFLIGHT.set_limit(limit)


def set_chat_priority(enabled: bool) -> None:
    INFLIGHT.chat_priority = enabled


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


class LLMStreamError(LLMError):
    """The endpoint failed after the stream had started (error chunk, dropped connection)."""

    kind = "upstream_error"


class LLMBusy(LLMError):
    """No model slot became free in time."""

    kind = "model_busy"


@dataclass
class StreamEvent:
    type: str
    """``waiting`` (no free slot yet), ``delta`` (``text``) or ``done``."""
    text: str = ""
    model: str = ""
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


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
    call_ref: str = ""
    """Telemetry id of the ``llm_calls`` row (``app.telemetry.update_llm_call``)."""

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


def _sse_data(line: str) -> str | None:
    """The payload of an SSE ``data:`` line (None for comments, ids and blank lines)."""
    if not line.startswith("data:"):
        return None
    return line[5:].strip()


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
        purpose: str,
        story_id: int | None = None,
        article_id: int | None = None,
        attempt: int = 1,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response",
        structured_output: StructuredOutput | None = None,
    ) -> ChatResult:
        """``purpose`` (required) labels the call for telemetry (``app.telemetry.PURPOSES``);
        ``story_id``/``article_id`` default to the worker's telemetry context."""
        from app import telemetry

        purpose = telemetry.check_purpose(purpose)
        payload = build_payload(
            self.role,
            messages,
            json_schema=json_schema,
            schema_name=schema_name,
            structured_output=structured_output,
        )
        key = _api_key(self.role)
        logger.debug("POST %s model=%s", self.url, self.role.model)
        call_ref = telemetry.new_call_ref()
        queued = time.perf_counter()
        await INFLIGHT.acquire()
        queue_wait_ms = round((time.perf_counter() - queued) * 1000)
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        try:
            prompt_text = telemetry.scrub(telemetry.prompt_text_of(messages), key)
        except Exception:
            prompt_text = None
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=httpx.Timeout(self.role.timeout_seconds)
            ) as client:
                response = await client.post(
                    self.url, json=payload, headers=build_headers(self.role)
                )
        except httpx.TimeoutException as exc:
            error = LLMTimeout(f"timed out after {self.role.timeout_seconds:g}s")
            self._emit(
                call_ref,
                purpose,
                story_id,
                article_id,
                attempt,
                started_at,
                queue_wait_ms,
                started,
                "timeout",
                error.kind,
                None,
                prompt_text,
            )
            raise self._fail(error, key) from exc
        except httpx.HTTPError as exc:
            error = LLMConnectionError(f"{type(exc).__name__}: {exc}")
            self._emit(
                call_ref,
                purpose,
                story_id,
                article_id,
                attempt,
                started_at,
                queue_wait_ms,
                started,
                "error",
                error.kind,
                None,
                prompt_text,
            )
            raise self._fail(error, key) from exc
        finally:
            INFLIGHT.release()
        latency_ms = round((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            body = " ".join(response.text.split())[:300]
            error = LLMHTTPError(response.status_code, f"HTTP {response.status_code}: {body}")
            self._emit(
                call_ref,
                purpose,
                story_id,
                article_id,
                attempt,
                started_at,
                queue_wait_ms,
                started,
                "error",
                error.kind,
                None,
                prompt_text,
            )
            raise self._fail(error, key)
        try:
            data = response.json()
        except ValueError:
            error = LLMBadResponse("response is not JSON")
            self._emit(
                call_ref,
                purpose,
                story_id,
                article_id,
                attempt,
                started_at,
                queue_wait_ms,
                started,
                "error",
                error.kind,
                None,
                prompt_text,
            )
            raise self._fail(error, key) from None
        result = parse_response(data, latency_ms=latency_ms, default_model=self.role.model)
        result.call_ref = call_ref
        self._emit(
            call_ref,
            purpose,
            story_id,
            article_id,
            attempt,
            started_at,
            queue_wait_ms,
            started,
            "ok",
            None,
            result,
            prompt_text,
        )
        if self.role.disable_thinking and result.reasoning_length:
            logger.warning(
                "%s returned %d characters of reasoning although disable_thinking is set; "
                "the server may ignore chat_template_kwargs",
                self.endpoint_host,
                result.reasoning_length,
            )
        return result

    def _emit(
        self,
        call_ref: str,
        purpose: str,
        story_id: int | None,
        article_id: int | None,
        attempt: int,
        started_at,
        queue_wait_ms: int,
        started: float,
        status: str,
        error_kind: str | None,
        result: ChatResult | None,
        prompt_text: str | None,
    ) -> None:
        from app import telemetry

        response_text = None
        if result is not None:
            try:
                response_text = telemetry.scrub(result.content, _api_key(self.role))
            except Exception:
                response_text = None
        telemetry.emit_llm_call(
            call_ref=call_ref,
            purpose=purpose,
            role=self.endpoint_host,
            model=(result.model if result else self.role.model) or self.role.model,
            story_id=story_id,
            article_id=article_id,
            attempt=attempt,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            queue_wait_ms=queue_wait_ms,
            latency_ms=round((time.perf_counter() - started) * 1000),
            prompt_tokens=result.prompt_tokens if result else None,
            completion_tokens=result.completion_tokens if result else None,
            status=status,
            error_kind=error_kind,
            prompt_text=prompt_text,
            response_text=response_text,
            finish_reason=result.finish_reason if result else None,
            reasoning_chars=result.reasoning_length if result else None,
        )

    async def stream_chat(
        self,
        messages: Sequence[Message],
        *,
        purpose: str,
        story_id: int | None = None,
        article_id: int | None = None,
        attempt: int = 1,
        shared_slot: bool = True,
        wait_timeout: float | None = None,
        first_token_timeout: float | None = None,
        idle_timeout: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """``stream: true`` chat completion: yields ``waiting`` (once, when the model slot is
        busy), a ``delta`` per content chunk and a final ``done``. Closing the generator
        (client gone, Stop) closes the upstream connection, so llama.cpp stops generating.

        ``shared_slot``: the endpoint is the summarizer's, so take a priority slot of the
        process-wide cap. Timeouts default to the role's ``timeout_seconds``; the idle timeout
        applies between chunks (a stalled stream fails instead of hanging)."""
        from app import telemetry

        purpose = telemetry.check_purpose(purpose)
        payload = build_payload(self.role, messages)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        key = _api_key(self.role)
        limit = self.role.timeout_seconds
        headers = {**build_headers(self.role), "Accept": "text/event-stream"}
        call_ref = telemetry.new_call_ref()
        queued = time.perf_counter()
        acquired = False
        if shared_slot:
            if not INFLIGHT._try(priority=True):
                yield StreamEvent("waiting")
                try:
                    await asyncio.wait_for(INFLIGHT.acquire(priority=True), wait_timeout or limit)
                except TimeoutError:
                    error = LLMBusy(f"the model stayed busy for {wait_timeout or limit:g}s")
                    self._emit_stream(
                        call_ref,
                        purpose,
                        story_id,
                        article_id,
                        attempt,
                        queued,
                        "error",
                        error.kind,
                        None,
                        messages,
                        key,
                    )
                    raise self._fail(error, key) from None
            acquired = True
        queue_wait_ms = round((time.perf_counter() - queued) * 1000)
        started_at = datetime.now(UTC)
        started = False
        done = StreamEvent("done", model=self.role.model)
        stream_failed: tuple[str, str] | None = None

        def _raise(error: LLMError) -> None:
            nonlocal stream_failed
            status = "timeout" if isinstance(error, LLMTimeout) else "error"
            stream_failed = (status, error.kind)
            raise self._fail(error, key)

        try:
            async with (
                httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(limit)) as client,
                client.stream("POST", self.url, json=payload, headers=headers) as response,
            ):
                if response.status_code >= 400:
                    body = " ".join((await response.aread()).decode(errors="replace").split())
                    _raise(
                        LLMHTTPError(
                            response.status_code,
                            f"HTTP {response.status_code}: {body[:300]}",
                        )
                    )
                lines = response.aiter_lines().__aiter__()
                while True:
                    wait = (idle_timeout if started else first_token_timeout) or limit
                    try:
                        line = await asyncio.wait_for(lines.__anext__(), wait)
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        what = "between chunks" if started else "before the first token"
                        _raise(LLMTimeout(f"no data for {wait:g}s {what}"))
                    chunk = _sse_data(line)
                    if chunk is None:
                        continue
                    if chunk == "[DONE]":
                        break
                    try:
                        data = json.loads(chunk)
                    except ValueError:
                        continue
                    if data.get("error"):
                        error = data["error"]
                        message = error.get("message") if isinstance(error, dict) else error
                        _raise(LLMStreamError(f"upstream error: {message}"))
                    usage = data.get("usage") or {}
                    done.prompt_tokens = usage.get("prompt_tokens", done.prompt_tokens)
                    done.completion_tokens = usage.get("completion_tokens", done.completion_tokens)
                    done.model = str(data.get("model") or done.model)
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = _content_text(delta.get("content"))
                        if choice.get("finish_reason"):
                            done.finish_reason = choice["finish_reason"]
                        if text:
                            started = True
                            yield StreamEvent("delta", text=text)
        except httpx.TimeoutException as exc:
            error = LLMTimeout(f"timed out after {limit:g}s")
            stream_failed = ("timeout", error.kind)
            raise self._fail(error, key) from exc
        except httpx.HTTPError as exc:
            error_type = LLMStreamError if started else LLMConnectionError
            error = error_type(f"{type(exc).__name__}: {exc}")
            stream_failed = ("error", error.kind)
            raise self._fail(error, key) from exc
        finally:
            if acquired:
                INFLIGHT.release()
            status, error_kind = stream_failed or ("ok", None)
            self._emit_stream(
                call_ref,
                purpose,
                story_id,
                article_id,
                attempt,
                queued,
                status,
                error_kind,
                done,
                messages,
                key,
                queue_wait_ms=queue_wait_ms,
                started_at=started_at,
            )
        yield done

    def _emit_stream(
        self,
        call_ref: str,
        purpose: str,
        story_id: int | None,
        article_id: int | None,
        attempt: int,
        queued: float,
        status: str,
        error_kind: str | None,
        done: StreamEvent | None,
        messages: Sequence[Message],
        key: str | None,
        *,
        queue_wait_ms: int | None = None,
        started_at: datetime | None = None,
    ) -> None:
        from app import telemetry

        try:
            prompt_text = telemetry.scrub(telemetry.prompt_text_of(messages), key)
        except Exception:
            prompt_text = None
        elapsed_ms = round((time.perf_counter() - queued) * 1000)
        telemetry.emit_llm_call(
            call_ref=call_ref,
            purpose=purpose,
            role=self.endpoint_host,
            model=(done.model if done else self.role.model) or self.role.model,
            story_id=story_id,
            article_id=article_id,
            attempt=attempt,
            started_at=started_at or datetime.now(UTC),
            finished_at=datetime.now(UTC),
            queue_wait_ms=queue_wait_ms,
            latency_ms=elapsed_ms,
            prompt_tokens=done.prompt_tokens if done else None,
            completion_tokens=done.completion_tokens if done else None,
            status=status,
            error_kind=error_kind,
            prompt_text=prompt_text,
            response_text=None,
            finish_reason=done.finish_reason if done else None,
        )

    def _fail(self, error: LLMError, key: str | None) -> LLMError:
        error.args = (redact(str(error), key),)
        logger.warning("LLM request to %s failed: %s", self.url, error)
        return error
