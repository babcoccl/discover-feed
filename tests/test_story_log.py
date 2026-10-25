"""Per-story step log: db_events capture, ``queries.story_log`` merging/filtering,
the text export, the inspector UI section and the new llm_calls columns."""

import re
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app import db, telemetry
from app.db import init_db, is_lock_error, make_engine, retry_locked
from app.models import DbEvent, LlmCall, StageEvent, Story, utcnow
from tests.pipeline_helpers import T0


@pytest.fixture
def telemetry_on(session_factory):
    old = telemetry._config
    telemetry.configure(session_factory, config=None)
    telemetry.set_enabled(True)
    yield session_factory
    telemetry._config = old


def locked_error() -> OperationalError:
    return OperationalError("UPDATE x", {}, sqlite3.OperationalError("database is locked"))


def _db_events(session_factory):
    from app.telemetry import writer

    writer.flush(timeout=5)
    with session_factory() as session:
        return session.scalars(select(DbEvent).order_by(DbEvent.id)).all()


# --- db_events emission ----------------------------------------------------------


@pytest.fixture
def fast_sleep(monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda s: None)


def test_retry_locked_emits_lock_retry_events(telemetry_on, fast_sleep) -> None:
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise locked_error()
        return "ok"

    assert retry_locked(flaky) == "ok"
    rows = _db_events(telemetry_on)
    kinds = [(r.kind, r.outcome) for r in rows]
    assert ("lock_retry", "retried") in kinds
    assert kinds[-1] == ("lock_retry", "ok_after_retry")
    retried = [r for r in rows if r.outcome == "retried"]
    assert all(r.waited_ms is not None and r.attempt for r in retried)
    # no SQL text or params anywhere
    assert all(r.table_name is None or isinstance(r.table_name, str) for r in rows)
    for r in rows:
        for col in r.__table__.columns:
            value = getattr(r, col.name)
            assert "UPDATE x" not in str(value), col.name


def test_retry_locked_giveup_emits_lock_giveup(telemetry_on, fast_sleep) -> None:
    def stuck():
        raise locked_error()

    with pytest.raises(OperationalError):
        retry_locked(stuck, retries=1)
    kinds = [(r.kind, r.outcome) for r in _db_events(telemetry_on)]
    assert ("lock_retry", "retried") in kinds
    assert ("lock_giveup", "gave_up") in kinds


def test_lock_retry_records_holder_fields(telemetry_on, fast_sleep) -> None:
    holder = telemetry_on()
    holder.add(Story(first_seen_at=T0, last_updated_at=T0))
    holder.flush()  # write txn open on the holder's connection
    try:

        def flaky():
            if not hasattr(flaky, "done"):
                flaky.done = True
                raise locked_error()
            return "ok"

        assert retry_locked(flaky) == "ok"
    finally:
        holder.rollback()
        holder.close()
    rows = [r for r in _db_events(telemetry_on) if r.kind == "lock_retry"]
    assert rows and rows[0].holder_caller and rows[0].holder_thread
    assert rows[0].holder_age_ms is not None


def test_handle_error_emits_lock_error(tmp_path: Path, telemetry_on, monkeypatch) -> None:
    """A real locked commit lands as a lock_error row (victim) with the holder named."""
    monkeypatch.setattr(db, "SQLITE_BUSY_TIMEOUT_MS", 150)
    path = tmp_path / "locks.db"
    engine = make_engine(f"sqlite:///{path}")
    init_db(engine)
    factory = db.make_session_factory(engine)
    hold = threading.Event()
    release = threading.Event()

    def holder() -> None:
        conn = sqlite3.connect(path)
        conn.execute("BEGIN IMMEDIATE")
        hold.set()
        release.wait(10)
        conn.commit()
        conn.close()

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert hold.wait(10)
    try:
        with factory() as session:
            session.add(Story(first_seen_at=T0, last_updated_at=T0))
            with pytest.raises(OperationalError) as exc:
                session.commit()
            assert is_lock_error(exc.value)
    finally:
        release.set()
        thread.join(10)
    rows = [r for r in _db_events(telemetry_on) if r.kind == "lock_error"]
    assert rows
    row = rows[0]
    assert row.caller and row.thread
    assert row.holder_caller and row.holder_thread
    for col in row.__table__.columns:
        assert "INSERT INTO" not in str(getattr(row, col.name) or ""), col.name


def test_slow_write_warning_and_event_carry_the_real_caller(
    telemetry_on, monkeypatch, caplog
) -> None:
    """A write txn held past the warn threshold logs and emits ``file:line:func``
    of the holder — never ``?``."""
    monkeypatch.setattr(db, "WRITE_TXN_WARN_SECONDS", 0)
    session = telemetry_on()
    session.add(Story(first_seen_at=T0, last_updated_at=T0))
    session.flush()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any("write txn held" in r.getMessage() for r in caplog.records):
            break
        time.sleep(0.05)
    session.commit()
    warnings = [r.getMessage() for r in caplog.records if "write txn held" in r.getMessage()]
    assert warnings
    assert " at ?" not in warnings[0] and "<unknown>" not in warnings[0]
    assert re.search(r"test_story_log\.py:\d+:test_slow_write_warning", warnings[0]), warnings[0]
    rows = [r for r in _db_events(telemetry_on) if r.kind == "slow_write"]
    assert rows and re.search(r"test_story_log\.py:\d+:test_slow_write", rows[0].caller)


def test_db_events_are_rate_limited(telemetry_on) -> None:
    from app.telemetry import db_events_dropped

    before = db_events_dropped()
    for _ in range(telemetry.DB_EVENTS_PER_SECOND + 15):
        telemetry.emit_db_event(kind="lock_retry", started_at=utcnow())
    assert db_events_dropped() - before >= 15
    time.sleep(1.1)  # let the sliding window clear so the flush isn't also limited
    rows = _db_events(telemetry_on)
    assert len(rows) <= telemetry.DB_EVENTS_PER_SECOND


def test_emit_db_event_never_runs_on_writer_thread(telemetry_on, monkeypatch) -> None:
    from app.telemetry import writer

    monkeypatch.setattr(writer, "on_writer_thread", lambda: True)
    before = telemetry.db_events_dropped()
    telemetry.emit_db_event(kind="lock_retry", started_at=utcnow())  # dropped silently
    assert telemetry.db_events_dropped() == before
    writer.flush(timeout=5)
    with telemetry_on() as session:
        assert session.scalar(select(func.count(DbEvent.id))) == 0


def test_emit_db_event_survives_writer_errors(telemetry_on, monkeypatch) -> None:
    from app.telemetry import writer

    def boom(*a, **k):
        raise RuntimeError("queue dead")

    monkeypatch.setattr(writer, "submit", boom)
    telemetry.emit_db_event(kind="slow_write", started_at=utcnow())  # must not raise


# --- llm_calls new columns -------------------------------------------------------


def test_llm_call_new_columns(telemetry_on) -> None:
    from app.telemetry import writer

    with telemetry.run_context("deadbeef"):
        telemetry.emit_llm_call(
            call_ref="n",
            purpose="brief",
            finish_reason="stop",
            reasoning_chars=1234,
        )
    writer.flush(timeout=5)
    with telemetry_on() as session:
        row = session.scalar(select(LlmCall))
    assert row.finish_reason == "stop" and row.reasoning_chars == 1234
    assert row.run_id == "deadbeef"


# --- story_log -------------------------------------------------------------------


def _seed_story(factory) -> int:
    with factory() as session:
        story = Story(first_seen_at=T0, last_updated_at=T0, clustered_at=T0)
        session.add(story)
        session.commit()
        return story.id


def test_story_log_orders_and_marks(telemetry_on) -> None:
    sid = _seed_story(telemetry_on)
    other = _seed_story(telemetry_on)
    t0 = T0 + timedelta(seconds=10)
    with telemetry_on() as session:
        session.add_all(
            [
                StageEvent(
                    ref="e",
                    stage="enrich",
                    story_id=sid,
                    status="done",
                    started_at=t0,
                    finished_at=t0 + timedelta(seconds=2),
                ),
                StageEvent(
                    ref="e",
                    stage="summarize",
                    story_id=sid,
                    status="running",
                    started_at=t0 + timedelta(seconds=40),
                ),
                StageEvent(
                    ref="e",
                    stage="brief",
                    story_id=other,
                    status="ok",
                    started_at=t0 + timedelta(seconds=1),
                    finished_at=t0 + timedelta(seconds=3),
                ),
                LlmCall(
                    call_ref="c1",
                    story_id=sid,
                    purpose="brief",
                    category="interpretation",
                    status="ok",
                    started_at=t0 + timedelta(seconds=1),
                    finished_at=t0 + timedelta(seconds=2),
                    latency_ms=1000,
                    prompt_tokens=100,
                    completion_tokens=50,
                    tokens_per_second=50.0,
                    finish_reason="stop",
                    reasoning_chars=10,
                    run_id="r1",
                ),
                DbEvent(
                    kind="lock_retry",
                    story_id=sid,
                    started_at=t0 + timedelta(seconds=1),
                    waited_ms=800,
                    attempt=2,
                    outcome="retried",
                    holder_caller="app/x.py:f",
                    holder_age_ms=500,
                ),
                DbEvent(
                    kind="lock_error",
                    story_id=None,
                    started_at=t0 + timedelta(seconds=1),
                    holder_story_id=sid,
                    holder_caller="app/y.py:g",
                    holder_age_ms=900,
                    caller="app/z.py:h",
                ),
            ]
        )
        session.commit()

    from app.telemetry import queries

    with telemetry_on() as session:
        log = queries.story_log(session, sid)
    assert [e["n"] for e in log["entries"]] == list(range(1, len(log["entries"]) + 1))
    names = [e["name"] for e in log["entries"]]
    assert names.index("enrich") < names.index("summarize")
    # offsets and gaps measured from the first entry
    first = log["entries"][0]
    assert first["offset_ms"] == 0
    summ = next(e for e in log["entries"] if e["name"] == "summarize")
    # first entry is the STATE first_seen at T0; summarize starts 50s later
    assert summ["offset_ms"] == 50_000 and summ["status"] == "running"
    gaps = [e["gap_ms"] for e in log["entries"] if e["gap_ms"]]
    assert gaps and max(gaps) >= 30_000
    # DB rows carry role
    victim = next(e for e in log["entries"] if e["kind"] == "DB" and e["role"] == "victim")
    assert victim["detail"]["waited_ms"] == 800 and victim["attempt"] == 2
    holder = next(e for e in log["entries"] if e["kind"] == "DB" and e["role"] == "holder")
    assert holder["name"] == "lock_error"
    # concurrent overlap counts the other story's work
    enrich = next(e for e in log["entries"] if e["name"] == "enrich")
    # the other story's "brief" stage + this story's own "brief" llm call
    assert enrich["concurrent"].get("brief") == 2
    # summary
    s = log["summary"]
    assert s["calls"] == 1 and s["prompt_tokens"] == 100 and s["completion_tokens"] == 50
    assert s["lock_events"]["lock_retry"] >= 1 and s["total_lock_wait_ms"] >= 800
    assert s["longest_holder"]["caller"] == "app/y.py:g"  # longest holder_age_ms wins
    assert log["active"] is True  # summarize still running


def test_story_log_filters_and_truncation(telemetry_on, monkeypatch) -> None:
    from app.telemetry import queries

    sid = _seed_story(telemetry_on)
    with telemetry_on() as session:
        for i in range(6):
            session.add(
                StageEvent(
                    ref="e",
                    stage="enrich",
                    story_id=sid,
                    status="ok",
                    started_at=T0 + timedelta(seconds=i),
                    finished_at=T0 + timedelta(seconds=i, milliseconds=10),
                )
            )
        session.add(
            LlmCall(
                call_ref="only",
                story_id=sid,
                purpose="brief",
                category="interpretation",
                status="failed",
                started_at=T0,
            )
        )
        session.commit()
    with telemetry_on() as session:
        llm_only = queries.story_log(session, sid, show="llm")
        assert llm_only["entries"] and all(e["kind"] == "LLM" for e in llm_only["entries"])
        problems = queries.story_log(session, sid, show="problems")
        assert all(
            e["kind"] == "DB" or e["status"] in queries._PROBLEM_STATUSES
            for e in problems["entries"]
        )
        monkeypatch.setattr(queries, "LOG_LIMIT", 3)
        capped = queries.story_log(session, sid)
        assert capped["truncated"] and len(capped["entries"]) == 3
        assert capped["total"] > 3


def test_story_log_text_has_no_payloads(telemetry_on) -> None:
    from app.telemetry import queries, writer

    sid = _seed_story(telemetry_on)
    telemetry.emit_llm_call(
        call_ref="p",
        purpose="brief",
        story_id=sid,
        prompt_text="PROMPT-BODY-SECRET",
        status="ok",
    )
    writer.flush(timeout=5)
    with telemetry_on() as session:
        log = queries.story_log(session, sid)
    log["story_id"] = sid
    text = queries.story_log_text(log)
    assert "PROMPT-BODY-SECRET" not in text
    assert f"story #{sid}" in text
    assert "brief" in text


# --- inspector UI -----------------------------------------------------------------


@pytest.fixture
def admin_client(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.settings import Settings

    db_url = f"sqlite:///{tmp_path / 'web.db'}"
    settings = Settings(
        config_path=Path(__file__).resolve().parent.parent / "config" / "profiles.example.yaml",
        database_url=db_url,
        scheduler_enabled=False,
        admin_key="admin-secret",  # type: ignore[arg-type]
    )
    with TestClient(create_app(settings)) as c:
        yield c, db_url


HEAD = {"x-api-key": "admin-secret"}


def _seed_web_story(db_url, *extra):
    """Insert a story (plus rows) against the app's DB via a second engine."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(db_url)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as session:
        story = Story(first_seen_at=T0, last_updated_at=T0)
        session.add(story)
        session.flush()
        for row in extra:
            row.story_id = row.story_id or story.id
            session.add(row)
        session.commit()
        return story.id


def test_step_log_page_renders_empty(admin_client) -> None:
    client, db_url = admin_client
    sid = _seed_web_story(db_url)
    resp = client.get(f"/p/personal-reader/settings/workflow/story/{sid}", headers=HEAD)
    assert resp.status_code == 200
    assert "Step log" in resp.text and "first_seen" in resp.text
    # nothing active → no polling attribute
    assert 'hx-trigger="every 5s"' not in resp.text


def test_step_log_page_renders_and_polls(admin_client) -> None:
    """Insert directly against the app's DB: a second engine on the same file
    sees the rows the app's own session factory committed."""
    client, db_url = admin_client
    sid = _seed_web_story(
        db_url,
        StageEvent(
            ref="e",
            stage="summarize",
            status="running",
            started_at=utcnow(),
        ),
        DbEvent(
            kind="lock_retry",
            started_at=utcnow(),
            waited_ms=500,
            attempt=1,
            outcome="retried",
        ),
    )

    resp = client.get(f"/p/personal-reader/settings/workflow/story/{sid}", headers=HEAD)
    assert resp.status_code == 200
    assert "summarize" in resp.text and "lock_retry" in resp.text
    assert "(victim)" in resp.text
    assert 'hx-trigger="every 5s"' in resp.text  # running → live polling

    partial = client.get(f"/p/personal-reader/settings/workflow/story/{sid}/log", headers=HEAD)
    assert partial.status_code == 200 and "lock_retry" in partial.text

    txt = client.get(f"/p/personal-reader/settings/workflow/story/{sid}/log.txt", headers=HEAD)
    assert txt.status_code == 200
    assert txt.headers["content-type"].startswith("text/plain")
    assert "lock_retry" in txt.text and "summarize" in txt.text


def test_step_log_cli(admin_client, capsys) -> None:
    from app.telemetry import report

    # no such story → still prints a header, exits 0
    assert report.main(["--story", "999"]) == 0
    out = capsys.readouterr().out
    assert "story #999" in out
