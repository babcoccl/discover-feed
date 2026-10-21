"""Batched telemetry writer: callers enqueue rows; a daemon thread flushes them in
short transactions so the LLM hot path never waits on a DB write, and a DB failure
is logged once and dropped — it can never break a worker or an LLM call."""

import logging
import queue
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

BATCH = 200
FLUSH_SECONDS = 1.0

_queue: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue()
_session_factory = None
_warned = threading.local()


def configure(session_factory) -> None:
    global _session_factory
    _session_factory = session_factory
    _ensure_thread()


def submit(kind: str, values: dict[str, Any]) -> None:
    """Enqueue one write; never blocks the caller beyond a queue put."""
    try:
        _queue.put_nowait((kind, values))
    except Exception as exc:
        _warn("submit", exc)


def _warn(what: str, exc: Exception) -> None:
    if getattr(_warned, what, False):
        return
    _warned.__setattr__(what, True)
    logger.warning("telemetry writer dropped %s: %s", what, exc)


def _drain(max_items: int) -> list[tuple[str, dict[str, Any]]]:
    items = []
    while len(items) < max_items:
        try:
            items.append(_queue.get_nowait())
        except queue.Empty:
            break
    return items


def _apply(session, items: list[tuple[str, dict[str, Any]]]) -> None:
    from sqlalchemy import select, update

    from app.models import LlmCall, StageEvent

    for kind, values in items:
        if kind == "llm_call":
            session.add(LlmCall(**values))
        elif kind == "stage_event":
            session.add(StageEvent(**values))
    # inserts before lookups: sessions run autoflush=False, so *_update items must
    # trigger an explicit flush to see the row they patch
    session.flush()
    for kind, values in items:
        if kind == "llm_call_update":
            row_id = session.scalar(
                select(LlmCall.id).where(LlmCall.call_ref == values["call_ref"]).limit(1)
            )
            if row_id is not None:
                session.execute(
                    update(LlmCall)
                    .where(LlmCall.id == row_id)
                    .values(
                        status=values["status"],
                        validation_error=values["validation_error"],
                    )
                )
        elif kind == "stage_event_update":
            row_id = session.scalar(
                select(StageEvent.id).where(StageEvent.ref == values["ref"]).limit(1)
            )
            if row_id is not None:
                session.execute(
                    update(StageEvent)
                    .where(StageEvent.id == row_id)
                    .values(
                        finished_at=values["finished_at"],
                        status=values["status"],
                        detail=values["detail"],
                    )
                )


def _flush_once() -> int:
    items = _drain(BATCH)
    if not items or _session_factory is None:
        return 0
    try:
        with _session_factory() as session:
            _apply(session, items)
            session.commit()
        return len(items)
    except Exception as exc:
        _warn("flush", exc)
        return 0


def _loop() -> None:
    while True:
        try:
            if _queue.empty():
                time.sleep(FLUSH_SECONDS)
                continue
            _flush_once()
        except Exception as exc:  # the writer thread must never die silently
            _warn("loop", exc)
            time.sleep(FLUSH_SECONDS)


_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def _ensure_thread() -> None:
    global _thread
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="telemetry-writer", daemon=True)
            _thread.start()


def flush(timeout: float = 5.0) -> None:
    """Block until the queue is empty (tests and shutdown)."""
    deadline = time.monotonic() + timeout
    while not _queue.empty() and time.monotonic() < deadline:
        _flush_once()
    if not _queue.empty():
        _flush_once()


def pending() -> int:
    return _queue.qsize()
