import logging
import os
import random
import re
import threading
import time
import traceback
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


SQLITE_BUSY_TIMEOUT_MS = 15000
"""How long a SQLite connection waits for another writer's lock before ``database is
locked`` (the app, the workers' threads, the telemetry writer and the CLIs share one
file)."""

# --- write-transaction instrumentation ---------------------------------------------------

WRITE_TXN_WARN_SECONDS = float(os.environ.get("DISCOVER_DB_WRITE_TXN_WARN_SECONDS", "2"))
"""A write transaction held longer than this logs ``write txn held Ns by ...``."""
INSTRUMENT_LOCKS = os.environ.get("DISCOVER_DB_INSTRUMENT_LOCKS", "1") != "0"
"""Env switch for the lock instrumentation (default on; negligible overhead)."""

_LOCK_STATS: dict[str, Any] = {
    "lock_errors_since_start": 0,
    "lock_retries_since_start": 0,
    "longest_write_txn_seconds": 0.0,
    "last_lock_error": None,
}
_write_txns: dict[int, dict] = {}
_write_txns_lock = threading.Lock()
_monitor_started = False

_WRITE_RE = re.compile(r"^\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
_TABLE_RE = re.compile(
    r"(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+[\"'`\[]?(\w+)",
    re.IGNORECASE,
)


def lock_stats() -> dict[str, Any]:
    """Process-wide counters: lock errors, retry_locked retries, the longest write
    transaction seen and the last lock error (table, time)."""
    return dict(_LOCK_STATS)


def is_lock_error(exc: BaseException) -> bool:
    return isinstance(exc, OperationalError) and "database is locked" in str(exc).lower()


def _caller() -> str:
    """First frame outside app/db.py and SQLAlchemy, formatted
    ``app/<path>.py:<line>:<func>`` (e.g. ``app/telemetry/writer.py:88:_flush_once``).

    The stack is walked untruncated: SQLAlchemy's event dispatch alone is deeper
    than any small ``limit=`` and would leave only db.py/sqlalchemy frames, which
    is how callers degraded to ``?``."""
    for f in reversed(traceback.extract_stack()):
        name = (f.filename or "").replace("\\", "/")
        if (
            not name.endswith(".py")  # also skips "<sqlalchemy generated ...>"
            or name.endswith("/app/db.py")
            or "/sqlalchemy/" in name
        ):
            continue
        path = name[name.rindex("/app/") + 1 :] if "/app/" in name else name
        return f"{path}:{f.lineno}:{f.name}"
    return "<unknown>"


def open_write_txns() -> list[dict]:
    """Snapshot of every write transaction open right now (thread, age, caller)."""
    now = time.monotonic()
    with _write_txns_lock:
        return [
            {
                "thread": t["thread"],
                "age_seconds": round(now - t["started"], 1),
                "caller": t["caller"],
                "story_id": t.get("story_id"),
            }
            for ref, t in _write_txns.values()
            if ref() is not None
        ]


def _current_story() -> int | None:
    try:
        from app import telemetry

        return telemetry.current_story()
    except Exception:
        return None


def _statement_meta(statement: str | None) -> tuple[str | None, str | None]:
    """``(table, op)`` of a SQL statement — the verb and name only, never the text."""
    if not statement:
        return None, None
    match = _TABLE_RE.search(statement)
    verb = re.match(r"^\s*(\w+)", statement)
    op = verb.group(1).upper() if verb else None
    if op == "REPLACE":
        op = "INSERT"
    return (match.group(1) if match else None), op


def _emit_db_event(kind: str, **fields) -> None:
    """Best-effort db_events emission via telemetry (never raises, never blocks)."""
    try:
        from app import telemetry

        telemetry.emit_db_event(kind=kind, **fields)
    except Exception:
        pass


def _holder_fields() -> dict:
    """holder_* columns for the write transaction open longest right now."""
    holders = open_write_txns()
    if not holders:
        return {}
    holder = max(holders, key=lambda h: h["age_seconds"])
    return {
        "holder_thread": holder["thread"],
        "holder_caller": holder["caller"],
        "holder_story_id": holder.get("story_id"),
        "holder_age_ms": int(holder["age_seconds"] * 1000),
    }


def _end_write_txn(conn) -> None:
    info = conn.info.pop("write_txn", None)
    with _write_txns_lock:
        _write_txns.pop(id(conn), None)
    if info is None:
        return
    held = time.monotonic() - info["started"]
    if held > _LOCK_STATS["longest_write_txn_seconds"]:
        _LOCK_STATS["longest_write_txn_seconds"] = round(held, 3)


def _watch_write_txns() -> None:
    while True:
        time.sleep(1.0)
        try:
            now = time.monotonic()
            with _write_txns_lock:
                entries = [t for ref, t in _write_txns.values() if ref() is not None]
            for t in entries:
                held = now - t["started"]
                if held >= WRITE_TXN_WARN_SECONDS and not t.get("warned"):
                    t["warned"] = True
                    logger.warning(
                        "write txn held %.1fs by %s at %s",
                        held,
                        t["thread"],
                        t["caller"],
                    )
                    _emit_db_event(
                        "slow_write",
                        story_id=t.get("story_id"),
                        thread=t["thread"],
                        caller=t["caller"],
                        duration_ms=int(held * 1000),
                        outcome="held",
                    )
        except Exception:  # the monitor never dies
            logger.exception("write-txn monitor failed")


def _instrument_locks(engine: Engine) -> None:
    """Track each connection's write transaction (first INSERT/UPDATE/DELETE to
    commit/rollback): warn on a long hold, and on ``database is locked`` log the
    statement's table plus every write transaction still open."""

    @event.listens_for(engine, "before_cursor_execute")
    def _first_write(conn, cursor, statement, parameters, context, executemany) -> None:
        if "write_txn" in conn.info or not _WRITE_RE.match(statement):
            return
        conn.info["write_txn"] = {
            "thread": threading.current_thread().name,
            "caller": _caller(),
            "started": time.monotonic(),
            "story_id": _current_story(),
        }
        with _write_txns_lock:
            _write_txns[id(conn)] = (weakref.ref(conn), conn.info["write_txn"])

    for name in ("commit", "rollback"):
        event.listen(engine, name, lambda conn: _end_write_txn(conn))

    @event.listens_for(engine, "checkin")
    def _on_checkin(dbapi_conn, _record) -> None:
        # Fairies returned to the pool without commit/rollback leave a stale
        # entry; drop any whose fairy is gone.
        with _write_txns_lock:
            stale = [k for k, (ref, _info) in _write_txns.items() if ref() is None]
            for k in stale:
                del _write_txns[k]

    @event.listens_for(engine, "handle_error")
    def _on_error(ctx) -> None:
        exc = ctx.original_exception or ctx.sqlalchemy_exception
        if exc is None or "database is locked" not in str(exc).lower():
            return
        match = _TABLE_RE.search(ctx.statement or "")
        table = match.group(1) if match else None
        _LOCK_STATS["lock_errors_since_start"] += 1
        _LOCK_STATS["last_lock_error"] = {
            "table": table,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        holders = open_write_txns()
        logger.warning(
            "database is locked on %s; open write txns: %s; statement: %s",
            table or "?",
            holders,
            (ctx.statement or "")[:200],
        )
        _table, op = _statement_meta(ctx.statement)
        _emit_db_event(
            "lock_error",
            story_id=_current_story(),
            table_name=_table,
            op=op,
            thread=threading.current_thread().name,
            caller=_caller(),
            **_holder_fields(),
        )

    global _monitor_started
    with _write_txns_lock:
        if not _monitor_started:
            _monitor_started = True
            threading.Thread(
                target=_watch_write_txns, name="db-write-txn-monitor", daemon=True
            ).start()


def retry_locked[T](fn: Callable[..., T], *args, retries: int = 3, **kwargs) -> T:
    """Run ``fn``; on ``database is locked`` retry with jittered backoff (0.5, 1, 2 s,
    ``retries`` tries). For short idempotent writes only — never a whole worker pass
    or a call that spans a network/LLM call."""
    delays = (0.5, 1.0, 2.0)
    for attempt in range(retries + 1):
        started = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except OperationalError as exc:
            if not is_lock_error(exc):
                raise
            table, op = _statement_meta(getattr(exc, "statement", None))
            base = {
                "story_id": _current_story(),
                "table_name": table,
                "op": op,
                "thread": threading.current_thread().name,
                "caller": _caller(),
                "attempt": attempt + 1,
                "waited_ms": int((time.monotonic() - started) * 1000),
                **_holder_fields(),
            }
            if attempt >= retries:
                _emit_db_event("lock_giveup", outcome="gave_up", **base)
                raise
            _emit_db_event("lock_retry", outcome="retried", **base)
            _LOCK_STATS["lock_retries_since_start"] += 1
            delay = delays[min(attempt, len(delays) - 1)] * (0.5 + random.random())
            name = getattr(fn, "__qualname__", repr(fn))
            logger.info(
                "database is locked; retrying %s in %.1fs (try %d of %d)",
                name,
                delay,
                attempt + 2,
                retries + 1,
            )
            time.sleep(delay)
            continue
        if attempt:
            _emit_db_event(
                "lock_retry",
                outcome="ok_after_retry",
                story_id=_current_story(),
                thread=threading.current_thread().name,
                caller=_caller(),
                attempt=attempt + 1,
                waited_ms=int((time.monotonic() - started) * 1000),
            )
        return result
    raise AssertionError("unreachable")  # pragma: no cover


def make_engine(database_url: str, **kwargs) -> Engine:
    url = make_url(database_url)
    connect_args: dict = {}
    if url.get_backend_name() == "sqlite":
        connect_args["check_same_thread"] = False
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, connect_args=connect_args, **kwargs)
    apply_sqlite_pragmas(engine)
    if url.get_backend_name() == "sqlite" and INSTRUMENT_LOCKS:
        _instrument_locks(engine)
    return engine


def apply_sqlite_pragmas(engine: Engine) -> None:
    """``PRAGMA busy_timeout`` on every new SQLite connection of ``engine``."""
    if engine.url.get_backend_name() != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _busy_timeout(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        finally:
            cursor.close()


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    from app import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(engine)
    _add_article_url(engine)
    _add_article_pipeline_columns(engine)
    _add_summary_kind(engine)
    _add_story_lifecycle(engine)
    _add_attempt_quality_columns(engine)
    _add_source_flags(engine)
    _add_alert_columns(engine)
    _add_source_management_columns(engine)
    _add_image_columns(engine)
    _add_enrichment_columns(engine)
    _add_context_columns(engine)
    _add_perspectives_columns(engine)
    _add_llm_call_columns(engine)


_LLM_CALL_COLUMNS = {
    "finish_reason": "VARCHAR(20)",
    "reasoning_chars": "INTEGER",
    "run_id": "VARCHAR(32)",
}


def _add_llm_call_columns(engine: Engine) -> None:
    """Pre-step-log DBs: llm_calls gains finish_reason/reasoning_chars/run_id
    (existing rows stay NULL)."""
    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(conn).get_columns("llm_calls")}
        for name, ddl in _LLM_CALL_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE llm_calls ADD COLUMN {name} {ddl}"))


def _add_article_url(engine: Engine) -> None:
    """Pre-`url` DBs: add the column, backfilled from the stored feed entry's link, else from
    canonical_url."""
    with engine.begin() as conn:
        if "url" in {c["name"] for c in inspect(conn).get_columns("articles")}:
            return
        conn.execute(text("ALTER TABLE articles ADD COLUMN url VARCHAR(2048) NOT NULL DEFAULT ''"))
        conn.execute(
            text(
                """
                UPDATE articles SET url = CASE
                    WHEN json_valid(raw_json)
                         AND lower(trim(json_extract(raw_json, '$.link'))) LIKE 'http%'
                    THEN trim(json_extract(raw_json, '$.link'))
                    ELSE canonical_url
                END
                """
            )
        )


_PIPELINE_COLUMNS = {
    "text": "TEXT",
    "text_status": "VARCHAR(10) NOT NULL DEFAULT 'pending'",
    "text_fetched_at": "DATETIME",
    "word_count": "INTEGER",
    "story_id": "INTEGER REFERENCES stories(id) ON DELETE SET NULL",
}


def _add_article_pipeline_columns(engine: Engine) -> None:
    """Pre-extraction DBs: add the text/story columns (existing rows start as `pending`)."""
    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(conn).get_columns("articles")}
        for name, ddl in _PIPELINE_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE articles ADD COLUMN {name} {ddl}"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_articles_text_status ON articles (text_status)")
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_articles_story_id ON articles (story_id)"))


def _add_summary_kind(engine: Engine) -> None:
    """Pre-report DBs: summaries get ``kind`` (existing rows are briefs) and ``content_json``
    (NULL: old plain-text summaries stay readable); ``summary_jobs`` is keyed by
    ``(story_id, kind)``, rebuilt keeping queued stories as brief jobs."""
    with engine.begin() as conn:
        columns = {c["name"] for c in inspect(conn).get_columns("story_summaries")}
        if "kind" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE story_summaries ADD COLUMN kind VARCHAR(10) NOT NULL "
                    "DEFAULT 'brief'"
                )
            )
        if "content_json" not in columns:
            conn.execute(text("ALTER TABLE story_summaries ADD COLUMN content_json JSON"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_story_summaries_kind ON story_summaries (kind)")
        )
        if "kind" in {c["name"] for c in inspect(conn).get_columns("summary_jobs")}:
            return
        conn.execute(text("ALTER TABLE summary_jobs RENAME TO summary_jobs_old"))
    from app.models import SummaryJob

    SummaryJob.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO summary_jobs (story_id, kind, queued_at, force, attempts,"
                " next_attempt_at, last_error) SELECT story_id, 'brief', queued_at, force,"
                " attempts, next_attempt_at, last_error FROM summary_jobs_old"
            )
        )
        conn.execute(text("DROP TABLE summary_jobs_old"))


_STORY_COLUMNS = {
    # Stories from before the publish gate were already clustered.
    "state": "VARCHAR(20) NOT NULL DEFAULT 'clustered'",
    "state_reason": "TEXT",
    "priority": "INTEGER NOT NULL DEFAULT 0",
    "merged_into": "INTEGER",
    "ingested_at": "DATETIME",
    "clustered_at": "DATETIME",
    "brief_ready_at": "DATETIME",
    "published_version": "INTEGER",
    "published_at": "DATETIME",
    "published_sort_at": "DATETIME",
}


def _add_story_lifecycle(engine: Engine) -> None:
    """Pre-publish-gate DBs: add the story state columns (``story_views`` is built from the
    old summaries at startup, see ``app.publish.migrate``)."""
    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(conn).get_columns("stories")}
        for name, ddl in _STORY_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE stories ADD COLUMN {name} {ddl}"))
        for name in ("state", "published_version", "published_sort_at"):
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_stories_{name} ON stories ({name})"))


_ATTEMPT_COLUMNS = {
    "run_id": "VARCHAR(32)",
    "attempt": "INTEGER",
    "prompt": "VARCHAR(10)",
    "temperature": "FLOAT",
    "warnings": "JSON",
}


def _add_attempt_quality_columns(engine: Engine) -> None:
    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(conn).get_columns("generation_attempts")}
        for name, ddl in _ATTEMPT_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE generation_attempts ADD COLUMN {name} {ddl}"))


def _add_source_flags(engine: Engine) -> None:
    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(conn).get_columns("sources")}
        for name in ("aggregator", "first_party"):
            if name not in existing:
                conn.execute(
                    text(f"ALTER TABLE sources ADD COLUMN {name} BOOLEAN NOT NULL DEFAULT 0")
                )


def _add_alert_columns(engine: Engine) -> None:
    """Pre-alerts DBs: event payloads and the once-only alert seeding marker."""
    with engine.begin() as conn:
        if "data" not in {c["name"] for c in inspect(conn).get_columns("story_events")}:
            conn.execute(text("ALTER TABLE story_events ADD COLUMN data JSON"))
        if "alerts_seeded_at" not in {c["name"] for c in inspect(conn).get_columns("profiles")}:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN alerts_seeded_at DATETIME"))


def _add_source_management_columns(engine: Engine) -> None:
    """Pre-source-management DBs: API params/limits, soft delete and auto-disable markers."""
    columns = {
        "params": "JSON",
        "api_key_env": "VARCHAR(100)",
        "limits": "JSON",
        "removed_at": "DATETIME",
        "auto_disabled_at": "DATETIME",
        "disabled_reason": "TEXT",
        "created_at": "DATETIME",
    }
    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(conn).get_columns("sources")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE sources ADD COLUMN {name} {ddl}"))
        if "items_last_run" not in {c["name"] for c in inspect(conn).get_columns("source_status")}:
            conn.execute(text("ALTER TABLE source_status ADD COLUMN items_last_run INTEGER"))


def ping(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


_IMAGE_COLUMNS = {
    "articles": {
        "page_image_url": "VARCHAR(2048)",
        "icon_url": "VARCHAR(2048)",
        "page_scanned_at": "DATETIME",
        "image_source": "VARCHAR(10)",
        "image_checked_at": "DATETIME",
    },
    "stories": {"image_source": "VARCHAR(10)"},
}


def _add_image_columns(engine: Engine) -> None:
    """Pre-image DBs: article/story image columns (old articles are picked up by the image
    backfill)."""
    with engine.begin() as conn:
        for table, columns in _IMAGE_COLUMNS.items():
            existing = {c["name"] for c in inspect(conn).get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        for name in ("page_image_url", "image_checked_at", "image_url"):
            conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS ix_articles_{name} ON articles ({name})")
            )


_ENRICHMENT_COLUMNS = {
    "articles": {
        "origin": "VARCHAR(12) NOT NULL DEFAULT 'feed'",
        "discovered_for_story_id": "INTEGER",
        "anchor_article_id": "INTEGER",
    },
    "stories": {
        "enrich_status": "VARCHAR(10)",
        "enrich_started_at": "DATETIME",
        "enriched_at": "DATETIME",
        "enrich_added": "INTEGER NOT NULL DEFAULT 0",
        "enrich_regens": "INTEGER NOT NULL DEFAULT 0",
        "enrich_reason": "TEXT",
        "enrich_next_attempt_at": "DATETIME",
        "enrich_attempts": "INTEGER NOT NULL DEFAULT 0",
        "enrich_wait_until": "DATETIME",
        "enrich_passes": "INTEGER NOT NULL DEFAULT 0",
        "enrich_regen_at": "DATETIME",
        "enrich_depth": "VARCHAR(10)",
    },
    "enrichment_candidates": {
        "title_similarity": "FLOAT",
        "shared_entities": "INTEGER",
        "flagged": "VARCHAR(40) NOT NULL DEFAULT ''",
    },
    "profiles": {"enrichment_enabled": "BOOLEAN"},
}


def _add_enrichment_columns(engine: Engine) -> None:
    """Pre-enrichment DBs: article origin/pinning, story enrichment state, profile toggle."""
    with engine.begin() as conn:
        for table, columns in _ENRICHMENT_COLUMNS.items():
            existing = {c["name"] for c in inspect(conn).get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_articles_discovered_for_story_id "
                "ON articles (discovered_for_story_id)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_stories_enrich_status ON stories (enrich_status)")
        )


_CONTEXT_COLUMNS = {
    "articles": {
        "context_note": "VARCHAR(300)",
        "context_angle": "VARCHAR(120)",
        "context_kind": "VARCHAR(10)",
        "context_task": "VARCHAR(24)",
        "context_fact": "TEXT",
        "context_quote": "TEXT",
        "context_speaker": "VARCHAR(200)",
        "context_origin": "VARCHAR(10)",
    },
    "stories": {
        "context_status": "VARCHAR(10)",
        "context_at": "DATETIME",
        "context_attempts": "INTEGER NOT NULL DEFAULT 0",
        "context_next_attempt_at": "DATETIME",
        "context_reason": "TEXT",
    },
    "profiles": {"context_enabled": "BOOLEAN"},
    "context_candidates": {
        "flagged": "VARCHAR(40) NOT NULL DEFAULT ''",
        "is_opinion": "BOOLEAN NOT NULL DEFAULT 0",
        "judge_same_event": "BOOLEAN",
        "same_event_signal": "VARCHAR(20) NOT NULL DEFAULT ''",
    },
}


def _add_context_columns(engine: Engine) -> None:
    """Pre-context DBs: article note/angle/kind, story context state, profile toggle
    (``context_candidates`` itself is created by ``Base.metadata.create_all``; its
    flag/opinion columns are added here when an older DB already has the table)."""
    with engine.begin() as conn:
        for table, columns in _CONTEXT_COLUMNS.items():
            existing = {c["name"] for c in inspect(conn).get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_stories_context_status ON stories (context_status)")
        )


_PERSPECTIVES_COLUMNS = {
    "profiles": {"perspectives_enabled": "BOOLEAN"},
}


def _add_perspectives_columns(engine: Engine) -> None:
    """Pre-perspectives DBs: the profile toggle (``article_digests`` itself is created
    by ``Base.metadata.create_all``)."""
    with engine.begin() as conn:
        for table, columns in _PERSPECTIVES_COLUMNS.items():
            existing = {c["name"] for c in inspect(conn).get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
