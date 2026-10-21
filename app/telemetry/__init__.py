"""Workflow observability (Phase 16): every LLM call and pipeline stage recorded with
timings, tokens and outcome so Settings > Workflow shows where time and LLM effort go.

Design rules from the spec:
- Writes go through a small batched writer (``writer.py``), never inside the hot path
  of each call; a telemetry failure is caught, logged once and dropped — the pipeline
  never blocks or fails on telemetry.
- ``purpose`` is required on every LLM call and labelled from the single map below.
- Payloads hold article text: local only, admin-only, scrubbed by the existing
  ``app.llm.client.redact`` plus generic key patterns.
"""

import contextlib
import contextvars
import hashlib
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import AppConfig, PayloadMode, TelemetryConfig

logger = logging.getLogger(__name__)

# --- purposes and categories (single source of truth) --------------------------------------

PURPOSES: dict[str, str] = {
    # Interpretation: understanding/enrichment work
    "query_llm": "interpretation",
    "context_planner": "interpretation",
    "context_extract": "interpretation",
    "context_compose": "interpretation",
    "context_angles": "interpretation",
    "history_extract": "interpretation",
    "perspective_digest": "interpretation",
    "judge": "interpretation",
    # Summarization
    "brief": "summarization",
    "followups": "summarization",
    # Reports
    "report": "reports",
    # Perspectives
    "perspective_synthesis": "perspectives",
    # Chat
    "chat": "chat",
    # System
    "health_check": "system",
}
"""``purpose -> category`` for every allowed LLM call purpose."""

STAGES = (
    "ingest",
    "extract",
    "cluster",
    "enrich",
    "context",
    "perspectives",
    "brief",
    "report",
    "followups",
    "publish_gate",
    "regenerate",
    "images",
    "alerts",
)

_KIND_PURPOSE = {"summary": "brief", "brief": "brief", "report": "report", "followups": "followups"}
"""The ``summary`` prompt inside brief generation counts as brief work."""

# --- per-task correlation (set by the workers, read by the client) -------------------------

_ctx_story: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "telemetry_story", default=None
)
_ctx_article: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "telemetry_article", default=None
)
_ctx_profile: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "telemetry_profile", default=None
)


def set_story(story_id: int | None) -> contextvars.Token:
    return _ctx_story.set(story_id)


def set_article(article_id: int | None) -> contextvars.Token:
    return _ctx_article.set(article_id)


def set_profile(profile: str | None) -> contextvars.Token:
    return _ctx_profile.set(profile)


def reset_story(token: contextvars.Token) -> None:
    _ctx_story.reset(token)


def reset_article(token: contextvars.Token) -> None:
    _ctx_article.reset(token)


def reset_profile(token: contextvars.Token) -> None:
    _ctx_profile.reset(token)


@contextlib.contextmanager
def story_context(story_id: int | None, article_id: int | None = None) -> Iterator[None]:
    """Tag every LLM call / stage event emitted inside with this story (and article)."""
    ts = _ctx_story.set(story_id)
    ta = _ctx_article.set(article_id)
    try:
        yield
    finally:
        _ctx_article.reset(ta)
        _ctx_story.reset(ts)


def current_story() -> int | None:
    return _ctx_story.get()


def current_article() -> int | None:
    return _ctx_article.get()


def current_profile() -> str | None:
    return _ctx_profile.get()


# --- runtime configuration ------------------------------------------------------------------

_config = TelemetryConfig()
_session_factory = None
_enabled_override: bool | None = None
_app_version = ""
_config_hash = ""
_warned = threading.local()


def configure(session_factory, config: AppConfig | None, *, app_version: str = "") -> None:
    """Called once at startup (lifespan / demo / tests). ``app_version``: build SHA env or
    package version; ``config_hash``: short hash of the active profile config."""
    global _session_factory, _config, _app_version, _config_hash
    _session_factory = session_factory
    _config = config.telemetry if config else TelemetryConfig()
    _app_version = app_version or os.environ.get("DISCOVER_BUILD_SHA", "")
    if not _app_version:
        try:
            from app.main import __version__

            _app_version = __version__
        except Exception:
            _app_version = "dev"
    if config and config.profiles:
        text = config.model_dump_json(
            exclude={"profiles": {"__all__": {"llm": {"__all__": {"api_key"}}}}}
        )
        _config_hash = hashlib.sha256(text.encode()).hexdigest()[:12]
    else:
        _config_hash = "none"
    from app.telemetry import writer

    writer.configure(_writer_factory(session_factory))


def _writer_factory(session_factory):
    """The writer gets its own engine (pool of 1) so a telemetry flush can never
    compete with the app's connections — on a saturated pool it would otherwise
    starve real work; on :memory: databases there is no separate DB, so reuse."""
    try:
        engine = session_factory.kw.get("bind")
        url = engine.url if engine is not None else None
        if url is None or url.database in (None, ":memory:"):
            return session_factory
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        separate = create_engine(str(url), connect_args={"check_same_thread": False}, pool_size=1)
        with separate.connect() as conn:
            # WAL persists on the file: readers then proceed during writer commits,
            # so batched telemetry flushes never stall app queries on the SQLite lock.
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA synchronous=OFF")
        return sessionmaker(bind=separate, autoflush=False, expire_on_commit=False)
    except Exception:
        return session_factory


def app_version() -> str:
    return _app_version


def config_hash() -> str:
    return _config_hash


def set_enabled(enabled: bool) -> None:
    """The Settings toggle: overrides the YAML ``telemetry.enabled`` for this process."""
    global _enabled_override
    _enabled_override = enabled
    _persist_flag(enabled)


def enabled() -> bool:
    if _enabled_override is not None:
        return _enabled_override
    return _config.enabled


def _persist_flag(enabled: bool) -> None:
    if _session_factory is None:
        return
    try:
        from app.models import AppSetting, utcnow

        with _session_factory() as session:
            row = session.get(AppSetting, "telemetry.enabled")
            if row is None:
                row = AppSetting(key="telemetry.enabled")
            row.value = {"enabled": enabled}
            row.updated_at = utcnow()
            session.add(row)
            session.commit()
    except Exception:
        logger.exception("telemetry: could not persist enabled flag")


def load_flags() -> None:
    """Startup: read the persisted toggle so it survives restarts."""
    global _enabled_override
    if _session_factory is None:
        return
    try:
        from app.models import AppSetting

        with _session_factory() as session:
            row = session.get(AppSetting, "telemetry.enabled")
            if row is not None and isinstance(row.value, dict):
                _enabled_override = bool(row.value.get("enabled"))
    except Exception:
        logger.exception("telemetry: could not load enabled flag")


def check_purpose(purpose: str) -> str:
    """Every LLM call must carry a known purpose — new features cannot go unlabeled."""
    if purpose not in PURPOSES:
        raise ValueError(
            f"LLM call purpose {purpose!r} is not in app.telemetry.PURPOSES; "
            "label it so workflow telemetry can classify it"
        )
    return purpose


def kind_purpose(kind: str) -> str:
    return _KIND_PURPOSE.get(kind, kind)


def _warn(what: str, exc: Exception) -> None:
    """Log once per process per failure kind, then drop (telemetry never breaks work)."""
    if getattr(_warned, what, False):
        return
    _warned.__setattr__(what, True)
    logger.warning("telemetry dropped (%s): %s", what, exc)


# --- payload capture ------------------------------------------------------------------------

_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{10,}|[A-Za-z_]*API_KEY\s*[:=]\s*\S+)",
    re.IGNORECASE,
)


def scrub(text: str, key: str | None) -> str:
    """Remove the role's API key and generic secret patterns from stored payloads."""
    from app.llm.client import redact

    text = redact(text, key)
    if _config is None or _config.redact:
        text = _SECRET_RE.sub("***", text)
    return text


def _payload_allowed(status: str) -> bool:
    mode = _config.store_payloads
    if mode is PayloadMode.NONE:
        return False
    if mode is PayloadMode.FAILURES:
        return status in ("invalid", "error", "timeout")
    return True


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    return text[: _config.payload_max_chars]


def prompt_text_of(messages) -> str:
    parts = []
    for m in messages or ():
        if isinstance(m, dict):
            parts.append(f"{m.get('role', '?')}: {m.get('content', '')}")
        else:
            parts.append(str(m))
    return "\n".join(parts)


# --- emit entry points (never raise) --------------------------------------------------------


def new_call_ref() -> str:
    return uuid.uuid4().hex


def emit_llm_call(
    *,
    call_ref: str,
    purpose: str,
    role: str = "",
    model: str = "",
    story_id: int | None = None,
    article_id: int | None = None,
    attempt: int = 1,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    queue_wait_ms: int | None = None,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    status: str = "ok",
    error_kind: str | None = None,
    validation_error: str | None = None,
    prompt_text: str | None = None,
    response_text: str | None = None,
) -> None:
    """Queue one ``llm_calls`` row. ``story_id``/``article_id`` fall back to the
    correlation context set by the calling worker."""
    if not enabled():
        return
    try:
        from app.telemetry import writer

        payload = None
        if _payload_allowed(status):
            payload = {}
            if prompt_text is not None:
                payload["prompt"] = _clip(prompt_text)
            if response_text is not None:
                payload["response"] = _clip(response_text)
        tps = None
        if completion_tokens and latency_ms and latency_ms > 0:
            tps = completion_tokens / (latency_ms / 1000)
        writer.submit(
            "llm_call",
            {
                "call_ref": call_ref,
                "started_at": started_at or datetime.now(UTC),
                "finished_at": finished_at or datetime.now(UTC),
                "purpose": purpose,
                "category": PURPOSES.get(purpose, "interpretation"),
                "role": role,
                "model": model,
                "story_id": story_id if story_id is not None else current_story(),
                "article_id": article_id if article_id is not None else current_article(),
                "attempt": attempt,
                "queue_wait_ms": queue_wait_ms,
                "latency_ms": latency_ms,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "tokens_per_second": tps,
                "status": status,
                "error_kind": error_kind,
                "validation_error": (validation_error or "")[:2000] or None,
                "prompt_chars": len(prompt_text or ""),
                "response_chars": len(response_text or ""),
                "payload": payload,
                "app_version": _app_version,
                "config_hash": _config_hash,
            },
        )
    except Exception as exc:
        _warn("emit_llm_call", exc)


def update_llm_call(call_ref: str, *, status: str, validation_error: str | None = None) -> None:
    """Post-hoc update (validation result known only after the response parses)."""
    if not enabled():
        return
    try:
        from app.telemetry import writer

        writer.submit(
            "llm_call_update",
            {
                "call_ref": call_ref,
                "status": status,
                "validation_error": (validation_error or "")[:2000] or None,
            },
        )
    except Exception as exc:
        _warn("update_llm_call", exc)


def emit_stage(
    stage: str,
    *,
    story_id: int | None = None,
    article_id: int | None = None,
    profile: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    status: str = "ok",
    detail: dict[str, Any] | None = None,
    ref: str | None = None,
) -> None:
    """Queue one ``stage_events`` row."""
    if not enabled():
        return
    try:
        from app.telemetry import writer

        writer.submit(
            "stage_event",
            {
                "ref": ref or new_call_ref(),
                "started_at": started_at or datetime.now(UTC),
                "finished_at": finished_at,
                "stage": stage,
                "story_id": story_id if story_id is not None else current_story(),
                "article_id": article_id if article_id is not None else current_article(),
                "profile": profile or current_profile(),
                "status": status,
                "detail": detail,
                "app_version": _app_version,
                "config_hash": _config_hash,
            },
        )
    except Exception as exc:
        _warn("emit_stage", exc)


def _update_stage(ref: str, *, finished_at: datetime, status: str, detail: dict) -> None:
    if not enabled():
        return
    try:
        from app.telemetry import writer

        writer.submit(
            "stage_event_update",
            {"ref": ref, "finished_at": finished_at, "status": status, "detail": detail},
        )
    except Exception as exc:
        _warn("update_stage", exc)


class stage:
    """Context manager: record a stage execution with duration and outcome.

    ``with telemetry.stage("enrich", story_id=sid, detail={...}) as ev:`` —
    ``ev["status"]``/``ev["detail"]`` can be updated inside; on exception the event is
    stored with ``status="failed"`` and the exception re-raised."""

    def __init__(
        self,
        name: str,
        *,
        story_id: int | None = None,
        article_id: int | None = None,
        profile: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.story_id = story_id
        self.article_id = article_id
        self.profile = profile
        self.detail = detail if detail is not None else {}
        self._t0 = 0.0
        self._started: datetime | None = None

    def __enter__(self) -> "stage":
        self._t0 = time.perf_counter()
        self._started = datetime.now(UTC)
        self._ref = new_call_ref()
        self.detail.setdefault("status", "ok")
        emit_stage(
            self.name,
            story_id=self.story_id,
            article_id=self.article_id,
            profile=self.profile,
            started_at=self._started,
            status="running",
            detail=dict(self.detail),
            ref=self._ref,
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        finished = datetime.now(UTC)
        status = self.detail.pop("status", "ok")
        if exc_type is not None:
            status = "failed"
            self.detail["error"] = f"{exc_type.__name__}: {exc}"[:300]
        self.detail["duration_ms"] = round((time.perf_counter() - self._t0) * 1000)
        _update_stage(self._ref, finished_at=finished, status=status, detail=dict(self.detail))
        return False


# --- retention ------------------------------------------------------------------------------


def prune(now: datetime | None = None) -> int:
    """Drop llm_calls/stage_events older than ``retention_days`` (run daily)."""
    if _session_factory is None:
        return 0
    try:
        from sqlalchemy import delete

        from app.models import LlmCall, StageEvent

        cutoff = (now or datetime.now(UTC)) - timedelta(days=_config.retention_days)
        removed = 0
        with _session_factory() as session:
            for table in (LlmCall, StageEvent):
                res = session.execute(delete(table).where(table.started_at < cutoff))
                removed += res.rowcount or 0
            session.commit()
        return removed
    except Exception as exc:
        _warn("prune", exc)
        return 0


def prune_blocking() -> None:
    """Scheduler entry point."""
    removed = prune()
    if removed:
        logger.info("telemetry: pruned %d old rows", removed)


def flush(timeout: float = 5.0) -> None:
    """Force the writer queue to empty (tests, shutdown)."""
    from app.telemetry import writer

    writer.flush(timeout)
