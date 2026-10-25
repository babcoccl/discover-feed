"""SQLite lock contention: write-transaction instrumentation, ``retry_locked``, the
transaction splits in Summarizer/ImageWorker/enrich and the telemetry re-queue."""

import asyncio
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app import db
from app.config import LLMRole
from app.db import init_db, is_lock_error, lock_stats, make_engine, retry_locked
from app.images.cache import image_key
from app.images.fetch import Fetched
from app.llm import LLMClient
from app.models import (
    GenerationAttempt,
    ImageAsset,
    Story,
    StorySummary,
    utcnow,
)
from app.summarize.fake import FakeLLM
from app.summarize.service import Summarizer
from app.telemetry import writer as telemetry_writer
from tests.pipeline_helpers import T0, add_article
from tests.test_enrich import make_enricher, no_sleep, seed
from tests.test_images import Server, jpeg, set_images, worker

ROLE = LLMRole(base_url="http://llm.test:8080/v1", model="test-model")


def locked_error() -> OperationalError:
    return OperationalError("UPDATE x", {}, sqlite3.OperationalError("database is locked"))


# --- retry_locked ------------------------------------------------------------------------


def test_retry_locked_retries_then_succeeds() -> None:
    calls = []
    before = lock_stats()["lock_retries_since_start"]

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise locked_error()
        return "ok"

    assert retry_locked(flaky) == "ok" and len(calls) == 3
    assert lock_stats()["lock_retries_since_start"] == before + 2


def test_retry_locked_gives_up_and_raises() -> None:
    def stuck():
        raise locked_error()

    with pytest.raises(OperationalError):
        retry_locked(stuck, retries=1)
    assert is_lock_error(locked_error())


def test_retry_locked_survives_a_held_write_txn(tmp_path: Path, monkeypatch) -> None:
    """A real write transaction held past busy_timeout fails first; the retried write
    lands once the holder commits."""
    monkeypatch.setattr(db, "SQLITE_BUSY_TIMEOUT_MS", 150)
    engine = make_engine(f"sqlite:///{tmp_path / 'locks.db'}")
    init_db(engine)
    factory = db.make_session_factory(engine)
    path = tmp_path / "locks.db"
    hold = threading.Event()
    release = threading.Event()

    def holder() -> None:
        conn = sqlite3.connect(path)
        # BEGIN IMMEDIATE alone holds SQLite's write lock until commit/rollback
        conn.execute("BEGIN IMMEDIATE")
        hold.set()
        release.wait(10)
        conn.commit()
        conn.close()

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert hold.wait(10)
    slept = []
    real_sleep = time.sleep
    monkeypatch.setattr(db.time, "sleep", lambda s: slept.append(s) or real_sleep(0.01))
    threading.Timer(0.5, release.set).start()  # commits while the write is failing

    wrote = []

    def write() -> None:
        with factory() as session:
            session.add(Story(first_seen_at=T0, last_updated_at=T0))
            session.commit()
            wrote.append(1)

    retry_locked(write)
    thread.join(10)
    assert wrote and slept  # locked at least once, retried, then committed
    assert lock_stats()["lock_errors_since_start"] > 0


# --- write-txn instrumentation -----------------------------------------------------------


def test_instrumentation_tracks_and_clears_write_txns(session_factory) -> None:
    session = session_factory()
    session.add(Story(first_seen_at=T0, last_updated_at=T0))
    session.flush()  # INSERT ran: a write transaction is open on this connection
    open_txns = db.open_write_txns()
    assert len(open_txns) == 1 and open_txns[0]["caller"]
    session.commit()
    assert db.open_write_txns() == []


def test_write_txn_caller_names_the_calling_function(session_factory) -> None:
    """The tracked caller is ``file:line:func`` of the function that opened the
    write transaction — not ``?``."""
    session = session_factory()

    def _do_the_write() -> None:
        session.add(Story(first_seen_at=T0, last_updated_at=T0))
        session.flush()

    _do_the_write()
    open_txns = db.open_write_txns()
    session.commit()
    assert len(open_txns) == 1
    caller = open_txns[0]["caller"]
    assert caller not in ("?", "<unknown>")
    assert re.fullmatch(r".*test_db_locks\.py:\d+:_do_the_write", caller)


# --- Summarizer._save: two transactions --------------------------------------------------


def test_summarizer_save_commits_attempts_before_the_view_rebuild(session_factory) -> None:
    """``on_save`` (the story_views rebuild) must run in a second transaction: the
    generation_attempts insert is already committed and visible in a fresh session,
    and no other write transaction is still open."""
    article_id = add_article(session_factory, url="https://a.example/x", text="t")
    with session_factory() as session:
        story = Story(first_seen_at=T0, last_updated_at=T0)
        session.add(story)
        session.commit()
        story_id = story.id
    seen = {}

    def on_save(session, sid: int) -> None:
        # txn A (record_attempts) is already committed: visible from a new session
        with session_factory() as fresh:
            seen["attempts_committed"] = fresh.scalar(
                select(func.count())
                .select_from(GenerationAttempt)
                .where(GenerationAttempt.story_id == sid)
            )
        # txn B holds the only open write txn (the summary row + view rebuild)
        seen["write_txns"] = db.open_write_txns()

    s = Summarizer(LLMClient(ROLE, transport=FakeLLM().transport()), clock=utcnow)
    s.on_save = on_save
    attempts = [{"kind": "brief", "outcome": "ok", "model": "m"}]
    s._save(
        session_factory,
        story_id,
        StorySummary(
            kind="brief",
            text="t",
            status="ok",
            basis="summary",
            prompt_version="v1",
            input_hash="h1",
        ),
        attempts,
        [{"article_id": article_id}],
    )
    assert seen["attempts_committed"] == 1
    assert len(seen["write_txns"]) == 1
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(StorySummary)
                .where(StorySummary.story_id == story_id)
            )
            == 1
        )


# --- ImageWorker: no write txn across fetch/render ----------------------------------------


def test_image_worker_holds_no_write_txn_during_fetch_and_render(
    session_factory, tmp_path, monkeypatch
) -> None:
    server = Server()
    url = "https://cdn.example/big.jpg"
    server.routes[url] = jpeg(1600, 900)
    aid = add_article(session_factory, url="https://pub.example/a", text="t")
    set_images(session_factory, aid, feed=url)
    w = worker(session_factory, tmp_path, server)
    observed = {"fetch": None, "render": None}

    async def fake_fetch(client, guard, u, **kw):
        observed["fetch"] = db.open_write_txns()
        return Fetched(u, jpeg(1600, 900), "image/jpeg", 1600, 900)

    def fake_render(data, width, **kw):
        observed["render"] = db.open_write_txns()
        return b"webp"

    monkeypatch.setattr("app.images.service.fetch_image", fake_fetch)
    monkeypatch.setattr("app.images.service.render_webp", fake_render)
    result = asyncio.run(w.run())
    assert result.status == "ok" and result.fetched > 0
    assert observed["fetch"] == [] and observed["render"] == []


def test_image_cache_hit_still_skips_fetch_after_the_split(session_factory, tmp_path) -> None:
    server = Server()
    url = "https://cdn.example/seen.jpg"
    with session_factory() as session:
        session.add(
            ImageAsset(
                url=url,
                key=image_key(url),
                kind="photo",
                status="ok",
                checked_at=T0,
                cached_bytes=1,
                width=800,
                height=450,
            )
        )
        session.commit()
    aid = add_article(session_factory, url="https://pub.example/a", text="t")
    set_images(session_factory, aid, feed=url)
    result = asyncio.run(worker(session_factory, tmp_path, server).run())
    assert result.cache_hits >= 1 and url not in server.hits  # icon guesses may 404


# --- enrichment: no write txn across search; lock failures retry --------------------------


def test_enrich_story_holds_no_write_txn_during_search(session_factory, monkeypatch) -> None:
    story_id = seed(session_factory)
    enricher = make_enricher(session_factory, fake_search())
    observed = {}

    async def search(*a, **kw):
        observed["txns"] = db.open_write_txns()
        return search_outcome()

    from app.search.service import SearchOutcome

    def search_outcome():
        return SearchOutcome(provider="gdelt", query="q", hits=[], error="locked")

    monkeypatch.setattr(enricher.search, "search", search)
    result = asyncio.run(enricher.enrich_story(story_id))
    assert observed["txns"] == []
    assert result.status == "retry"  # nothing answered: retry, never failed


def test_lock_failure_leaves_the_story_in_retry(session_factory, monkeypatch) -> None:
    story_id = seed(session_factory)
    enricher = make_enricher(session_factory, fake_search())

    async def blow_up(result, dry_run, **kw):
        raise locked_error()

    monkeypatch.setattr(enricher, "_enrich", blow_up)
    outcome = asyncio.run(enricher.enrich_story(story_id))
    assert outcome.status == "retry" and outcome.reason == "locked"
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_status == "retry" and story.enrich_reason == "locked"
        assert story.enrich_next_attempt_at is not None


def test_requeue_lock_failures_repairs_failed_stories(session_factory) -> None:
    story_id = seed(session_factory)
    with session_factory() as session:
        story = session.get(Story, story_id)
        story.enrich_status = "failed"
        story.enrich_reason = "OperationalError: (sqlite3.OperationalError) database is locked"
        story.enriched_at = T0
        session.commit()
    enricher = make_enricher(session_factory, fake_search())
    assert enricher.requeue_lock_failures() == 1
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_status == "retry"


def fake_search():
    from tests.test_enrich import FakeSearch

    return FakeSearch([("https://other.example/x", "Other coverage")])


# --- SearchService hot path ---------------------------------------------------------------


def test_record_none_skips_the_state_write_when_clean(session_factory, monkeypatch) -> None:
    from app.search.service import SearchService

    service = SearchService(session_factory, sleep=no_sleep)
    calls = []
    real = service._with_state_once

    def wrapped(name, change):
        calls.append(name)
        return real(name, change)

    monkeypatch.setattr(service, "_with_state_once", wrapped)
    service._record("gdelt", None)  # creates the state row: one write
    service._record("gdelt", None)  # already clean: skipped
    assert calls == ["gdelt"]
    service._record("gdelt", "boom")
    service._record("gdelt", None)  # dirty row clears: one more write
    assert calls == ["gdelt", "gdelt", "gdelt"]
    service._record("gdelt", None)  # clean again: skipped
    assert calls == ["gdelt", "gdelt", "gdelt"]


# --- telemetry writer ---------------------------------------------------------------------


def test_telemetry_requeues_a_failed_flush(tmp_path: Path, monkeypatch) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    init_db(engine)
    factory = db.make_session_factory(engine)
    calls = {"n": 0}
    real_apply = telemetry_writer._apply

    def flaky(session, items):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("INSERT", {}, sqlite3.OperationalError("database is locked"))
        return real_apply(session, items)

    monkeypatch.setattr(telemetry_writer, "_apply", flaky)
    telemetry_writer._session_factory = factory
    telemetry_writer.submit(
        "stage_event",
        {
            "ref": "r1",
            "started_at": utcnow(),
            "finished_at": utcnow(),
            "stage": "enrich",
            "status": "ok",
        },
    )
    deadline = time.monotonic() + 10
    while calls["n"] < 2 and time.monotonic() < deadline:
        telemetry_writer._flush_once()
    assert calls["n"] >= 2
    telemetry_writer.flush()
    with factory() as session:
        from app.models import StageEvent

        assert (
            session.scalar(
                select(func.count()).select_from(StageEvent).where(StageEvent.ref == "r1")
            )
            == 1
        )
    assert telemetry_writer.dropped_rows() == 0
