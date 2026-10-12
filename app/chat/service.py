"""Stream an answer from the ``chat`` role: priority over the generation worker, upstream
cancellation when the client goes away, post-hoc verification and storage."""

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field

import httpx
from sqlalchemy.orm import sessionmaker

from app.chat import store
from app.chat.context import chat_context
from app.chat.prompt import CHAT_PROMPT_VERSION, build_chat_messages
from app.chat.verify import verify
from app.config import AppConfig, LLMRole, Profile, SummariesConfig
from app.llm import LLMClient, LLMError, LLMHTTPError
from app.llm.client import INFLIGHT, set_chat_priority
from app.llm.health import EndpointHealth, check_endpoint
from app.models import ChatMessage, Conversation, utcnow
from app.summarize.prompt import SourceDoc

logger = logging.getLogger(__name__)

ERROR_MESSAGES = {
    "timeout": "The model did not answer in time.",
    "model_busy": "The model stayed busy with background work.",
    "unreachable": "The chat endpoint is unreachable.",
    "upstream_error": "The model stopped with an error mid-answer.",
    "empty_output": "The model returned an empty answer.",
    "http_401": "The chat endpoint rejected the API key (HTTP 401).",
    "client_disconnected": "Stopped before the answer was complete.",
    "story_unavailable": "This story is no longer published.",
}


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def error_reason(exc: LLMError) -> str:
    if isinstance(exc, LLMHTTPError):
        return f"http_{exc.status_code}"
    if exc.kind == "connection":
        return "unreachable"
    return exc.kind


def citation(source: SourceDoc) -> dict:
    return {
        "index": source.index,
        "article_id": source.article_id,
        "source": source.source,
        "publisher": source.publisher or source.source,
        "via": source.via,
        "headline": source.headline,
        "url": source.url,
    }


@dataclass
class Answer:
    model: str = ""
    text: str = ""
    chunks: int = 0
    tokens: int | None = None
    time_to_first_token_ms: int | None = None
    latency_ms: int | None = None
    finish_reason: str | None = None
    status: str = "streaming"
    error: str | None = None
    detail: str | None = None
    citations: list[dict] = field(default_factory=list)
    verification: dict | None = None
    waited: bool = False

    def error_event(self) -> dict:
        reason = self.error or "error"
        message = ERROR_MESSAGES.get(reason) or (
            f"The chat endpoint answered {reason.replace('_', ' ').upper()}."
            if reason.startswith("http_")
            else "The answer failed."
        )
        return {
            "reason": reason,
            "message": message,
            "detail": self.detail,
            "partial": bool(self.text),
        }

    def done_event(self) -> dict:
        tps = None
        if self.tokens and self.latency_ms and self.time_to_first_token_ms is not None:
            gen_ms = max(self.latency_ms - self.time_to_first_token_ms, 1)
            tps = round(self.tokens / (gen_ms / 1000), 1)
        return {
            "verification": self.verification,
            "tokens": self.tokens,
            "latency_ms": self.latency_ms,
            "time_to_first_token_ms": self.time_to_first_token_ms,
            "tokens_per_second": tps,
            "finish_reason": self.finish_reason,
            "model": self.model,
        }


async def stream_answer(
    client: LLMClient,
    messages: Sequence[dict],
    sources: Sequence[SourceDoc],
    question: str,
    answer: Answer,
    *,
    shared_slot: bool = True,
    started: float | None = None,
) -> AsyncIterator[tuple[str, dict]]:
    """``(event, data)``: ``waiting``, ``token``..., then ``citations`` + ``done``, or
    ``error`` (the partial text stays in ``answer``)."""
    started = started or time.perf_counter()

    def ms() -> int:
        return round((time.perf_counter() - started) * 1000)

    try:
        async with aclosing(client.stream_chat(messages, shared_slot=shared_slot)) as stream:
            async for ev in stream:
                if ev.type == "waiting":
                    answer.waited = True
                    yield "waiting", {"reason": "model_busy", "message": "Waiting for the model..."}
                elif ev.type == "delta":
                    if answer.time_to_first_token_ms is None:
                        answer.time_to_first_token_ms = ms()
                    answer.text += ev.text
                    answer.chunks += 1
                    yield "token", {"text": ev.text}
                else:
                    answer.model = ev.model or answer.model
                    answer.finish_reason = ev.finish_reason
                    answer.tokens = ev.completion_tokens
    except LLMError as exc:
        answer.latency_ms = ms()
        answer.status = "incomplete" if answer.text else "error"
        answer.error, answer.detail = error_reason(exc), str(exc)
        yield "error", answer.error_event()
        return
    answer.latency_ms = ms()
    answer.tokens = answer.tokens or answer.chunks
    if not answer.text.strip():
        answer.status, answer.error = "error", "empty_output"
        yield "error", answer.error_event()
        return
    result = verify(answer.text, sources, question)
    answer.verification = result.as_json()
    by_index = {s.index: s for s in sources}
    answer.citations = [citation(by_index[i]) for i in result.cited]
    answer.status = "complete"
    yield "citations", {"citations": answer.citations}
    yield "done", answer.done_event()


class ChatService:
    """One profile's chat role."""

    def __init__(
        self,
        profile: str,
        role: LLMRole,
        options: SummariesConfig,
        session_factory: sessionmaker,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        shared_slot: bool = True,
    ) -> None:
        self.profile = profile
        self.role = role
        self.options = options
        self.session_factory = session_factory
        self.transport = transport
        self.client = LLMClient(role, transport=transport)
        self.shared_slot = shared_slot
        self.health = EndpointHealth("ok", "not checked yet", self.client.endpoint_host)
        self._lock = threading.Lock()
        self.active = 0
        self.tasks: set[asyncio.Task] = set()
        """Running answers (tests await them)."""

    @property
    def model(self) -> str:
        return self.role.model

    @property
    def available(self) -> bool:
        return self.health.state in ("ok", "error")

    async def check(self) -> EndpointHealth:
        self.health = await check_endpoint(self.role, transport=self.transport)
        return self.health

    def _try_slot(self) -> bool:
        with self._lock:
            if self.active >= self.options.chat_max_concurrent:
                return False
            self.active += 1
            return True

    def _release_slot(self) -> None:
        with self._lock:
            self.active = max(0, self.active - 1)

    # --- storage ---------------------------------------------------------------------------

    def start(self, story_id: int, question: str, conversation_id: str | None):
        """Store the question and an empty ``streaming`` answer; ``(conversation, answer id)``
        or None when ``conversation_id`` is unknown or belongs to another story."""
        now = utcnow()
        with self.session_factory() as session:
            if conversation_id:
                conv = session.get(Conversation, conversation_id)
                if conv is None or conv.story_id != story_id:
                    return None
            else:
                conv = store.new_conversation(session, story_id, now)
            conv.last_active_at = now
            session.add(ChatMessage(conversation_id=conv.id, role="user", content=question))
            reply = ChatMessage(
                conversation_id=conv.id,
                role="assistant",
                status="streaming",
                model=self.model,
                prompt_version=CHAT_PROMPT_VERSION,
            )
            session.add(reply)
            session.commit()
            return conv.id, reply.id

    def finish(self, message_id: int, answer: Answer) -> None:
        with self.session_factory() as session:
            row = session.get(ChatMessage, message_id)
            if row is None:  # the conversation was deleted meanwhile
                return
            row.content = answer.text
            row.status = answer.status
            row.error = answer.error
            row.citations_json = answer.citations
            row.verification_json = answer.verification
            row.model = answer.model or row.model
            row.tokens = answer.tokens
            row.latency_ms = answer.latency_ms
            row.time_to_first_token_ms = answer.time_to_first_token_ms
            conv = session.get(Conversation, row.conversation_id)
            if conv is not None:
                conv.last_active_at = utcnow()
            session.commit()

    # --- answering -------------------------------------------------------------------------

    async def _produce(
        self,
        queue: asyncio.Queue,
        story_id: int,
        conversation_id: str,
        message_id: int,
        question: str,
        answer: Answer,
    ) -> None:
        started = time.perf_counter()
        priority = self.shared_slot
        if priority:
            INFLIGHT.chat_begin()
        slot = False
        try:
            if not self._try_slot():
                answer.waited = True
                queue.put_nowait(
                    ("waiting", {"reason": "chat_busy", "message": "Waiting for the model..."})
                )
                deadline = time.monotonic() + self.role.timeout_seconds
                while not self._try_slot():
                    if time.monotonic() > deadline:
                        answer.status, answer.error = "error", "model_busy"
                        queue.put_nowait(("error", answer.error_event()))
                        return
                    await asyncio.sleep(0.05)
            slot = True

            def build():
                with self.session_factory() as session:
                    turns = store.history(session, conversation_id, before_id=message_id)
                    return chat_context(
                        session,
                        story_id,
                        question,
                        role=self.role,
                        options=self.options,
                        history=turns,
                    )

            ctx = await asyncio.to_thread(build)
            if ctx is None:
                answer.status, answer.error = "error", "story_unavailable"
                queue.put_nowait(("error", answer.error_event()))
                return
            messages = build_chat_messages(ctx, question)
            async with aclosing(
                stream_answer(
                    self.client,
                    messages,
                    ctx.sources,
                    question,
                    answer,
                    shared_slot=self.shared_slot,
                    started=started,
                )
            ) as events:
                async for item in events:
                    if item[0] == "waiting" and answer.waited and item[1]["reason"] != "model_busy":
                        continue
                    queue.put_nowait(item)
            if answer.error == "unreachable":
                self.health = EndpointHealth("unreachable", answer.detail or "", self.health.host)
            elif answer.status == "complete":
                self.health = EndpointHealth("ok", "", self.health.host)
        except asyncio.CancelledError:
            answer.status = "incomplete" if answer.text else "error"
            answer.error = "client_disconnected"
            raise
        finally:
            if priority:
                INFLIGHT.chat_end()
            if slot:
                self._release_slot()
            if answer.status == "streaming":
                answer.status = "incomplete" if answer.text else "error"
                answer.error = answer.error or "client_disconnected"
            try:
                self.finish(message_id, answer)
            except Exception:
                logger.exception("could not store chat answer %s", message_id)
            queue.put_nowait(None)

    async def ask(
        self,
        story_id: int,
        conversation_id: str,
        message_id: int,
        question: str,
        *,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
        poll: float = 0.25,
        heartbeat: float = 15,
    ) -> AsyncIterator[str]:
        """SSE text: ``meta``, then ``waiting``/``token``... and ``citations`` + ``done`` or
        ``error``. The answer runs in a task; when the client disconnects (``is_disconnected``
        polled every ``poll`` s) or this generator is closed, the task is cancelled, which
        closes the upstream request."""
        answer = Answer(model=self.model)
        queue: asyncio.Queue = asyncio.Queue()
        yield sse(
            "meta",
            {"conversation_id": conversation_id, "message_id": message_id, "model": self.model},
        )
        task = asyncio.create_task(
            self._produce(queue, story_id, conversation_id, message_id, question, answer)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        idle = 0.0
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), poll)
                except TimeoutError:
                    if is_disconnected is not None and await is_disconnected():
                        return
                    idle += poll
                    if idle >= heartbeat:
                        idle = 0.0
                        yield ": keep-alive\n\n"
                    continue
                if item is None:
                    return
                idle = 0.0
                yield sse(*item)
        finally:
            if not task.done():
                task.cancel()


class ChatHub:
    """Every profile's :class:`ChatService` (profiles without ``llm.chat`` have none)."""

    def __init__(
        self,
        services: dict[str, ChatService],
        default: str | None,
        session_factory: sessionmaker,
        retention_days: float = 30,
    ) -> None:
        self.services = services
        self.default = default
        self.session_factory = session_factory
        self.retention_days = retention_days

    def for_profile(self, slug: str | None) -> ChatService | None:
        return self.services.get(slug or self.default or "")

    def state(self, slug: str | None) -> tuple[str, str]:
        """``(state, explanation)`` for the story page's panel."""
        service = self.for_profile(slug)
        if service is None:
            return "unset", "Follow-up questions are off: this profile has no llm.chat endpoint."
        health = service.health
        if health.state == "unreachable":
            return "unreachable", (
                f"Follow-up questions are unavailable: the chat endpoint {health.host} is "
                f"unreachable ({health.detail}). Retrying every minute."
            )
        if health.state == "unauthorized":
            return "unauthorized", (
                f"The chat endpoint {health.host} answered HTTP 401: check its API key."
            )
        return "ok", ""

    async def check_all(self) -> None:
        for service in self.services.values():
            await service.check()

    def recheck_blocking(self) -> None:
        """Scheduler job: re-check endpoints that were down."""
        down = [
            s for s in self.services.values() if s.health.state in ("unreachable", "unauthorized")
        ]
        for service in down:
            asyncio.run(service.check())

    def cleanup_blocking(self) -> int:
        removed = store.cleanup(self.session_factory, self.retention_days)
        if removed:
            logger.info("chat: deleted %d idle conversations", removed)
        return removed


def build_hub(
    session_factory: sessionmaker,
    config: AppConfig,
    summarize_profile: Profile | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ChatHub:
    """``summarize_profile`` (the worker's) decides chat priority and the shared slot: a chat
    role on the summarizer's endpoint competes with the worker for the same model slot."""
    worker_url = str(summarize_profile.llm.summarizer.base_url) if summarize_profile else None
    services = {}
    for profile in config.profiles:
        role = profile.llm.chat
        if role is None:
            continue
        services[profile.id] = ChatService(
            profile.id,
            role,
            profile.summaries,
            session_factory,
            transport=transport,
            shared_slot=str(role.base_url) == worker_url,
        )
    lead = summarize_profile or (config.profiles[0] if config.profiles else None)
    options = lead.summaries if lead else SummariesConfig()
    set_chat_priority(options.chat_priority)
    default = lead.id if lead else None
    return ChatHub(services, default, session_factory, options.chat_retention_days)


async def drain(service: ChatService) -> None:
    """Wait for running answers to finish (tests)."""
    while service.tasks:
        with contextlib.suppress(Exception):
            await asyncio.gather(*list(service.tasks), return_exceptions=True)
