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

_queue: "queue.Queue[tuple[str, dict[str, Any], Any]]" = queue.Queue()
_session_factory = None
_warned = threading.local()


def configure(session_factory) -> None:
    global _session_factory
    _session_factory = session_factory
    _ensure_thread()


def submit(kind: str, values: dict[str, Any]) -> None:
    """Enqueue one write; never blocks the caller beyond a queue put."""
    try:
        # bind the destination now: if configure() is called again before the
        # daemon drains this item it still lands in the DB it was queued for
        _queue.put_nowait((kind, values, _session_factory))
    except Exception as exc:
        _warn("submit", exc)


def _warn(what: str, exc: Exception) -> None:
    if getattr(_warned, what, False):
        return
    _warned.__setattr__(what, True)
    logger.warning("telemetry writer dropped %s: %s", what, exc)


def _drain(max_items: int) -> list[tuple[str, dict[str, Any], Any]]:
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
    if not items:
        return 0
    by_factory: dict[Any, list[tuple[str, dict[str, Any]]]] = {}
    for kind, values, factory in items:
        by_factory.setdefault(factory, []).append((kind, values))
    written = 0
    for factory, group in by_factory.items():
        if factory is None:
            continue
        try:
            with factory() as session:
                _apply(session, group)
                session.commit()
            written += len(group)
        except Exception as exc:
            _warn("flush", exc)
    return written


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
