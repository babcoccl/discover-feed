import threading
import time

from apscheduler.triggers.interval import IntervalTrigger

from app.config import Source
from app.ingest import Ingestor
from app.scheduler import create_scheduler, schedule_enrichment, schedule_ingestion


def test_one_job_per_source_with_interval_and_jitter(session_factory) -> None:
    sources = [
        Source(name="Fast", url="https://a.example/rss", refresh_minutes=10),
        Source(name="Default", url="https://b.example/rss"),
    ]
    scheduler = create_scheduler()
    schedule_ingestion(scheduler, sources, Ingestor(session_factory), jitter_seconds=90)

    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"ingest:fast", "ingest:default"}
    fast, default = jobs["ingest:fast"].trigger, jobs["ingest:default"].trigger
    assert isinstance(fast, IntervalTrigger)
    assert fast.interval.total_seconds() == 600
    assert default.interval.total_seconds() == 30 * 60
    assert default.jitter == 90
    assert fast.jitter == 90  # capped at a quarter interval (150s) -> 90 fits
    assert jobs["ingest:default"].args == ([sources[1]],)


def _wait_for(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_enrichment_jobs_are_registered_with_their_intervals() -> None:
    from app.enrich.service import Enricher

    scheduler = create_scheduler()
    schedule_enrichment(scheduler, Enricher.__new__(Enricher))
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert jobs["enrich"].trigger.interval.total_seconds() == 60
    assert jobs["enrich-watchdog"].trigger.interval.total_seconds() == 120  # within 2 min


def test_scheduled_enrichment_job_fires_on_its_interval_and_calls_schedule(
    session_factory,
) -> None:
    from tests.test_enrich import FakeSearch, make_enricher

    enricher = make_enricher(session_factory, FakeSearch())
    calls: list[object] = []
    enricher.schedule = lambda *a, **kw: calls.append(a) or 0
    scheduler = create_scheduler()
    schedule_enrichment(
        scheduler,
        enricher,
        interval_seconds=0.1,
        watchdog_seconds=3600,
        first_run_delay_seconds=0,
    )
    scheduler.start()
    try:
        assert _wait_for(lambda: len(calls) >= 3)  # no pipeline run involved
    finally:
        scheduler.shutdown(wait=False)
    assert enricher.last_sweep_at is not None


def test_watchdog_job_restarts_a_dead_worker_thread(session_factory) -> None:
    from app.models import Story
    from tests.test_enrich import FakeSearch, make_enricher, seed

    story_id = seed(session_factory)
    enricher = make_enricher(session_factory, FakeSearch())
    with session_factory() as session:
        session.get(Story, story_id).enrich_status = "pending"  # left by a dead worker
        session.commit()
    enricher._thread = threading.Thread(target=lambda: None)
    enricher._thread.start()
    enricher._thread.join()  # dead
    dead = enricher._thread
    scheduler = create_scheduler()
    schedule_enrichment(scheduler, enricher, interval_seconds=3600, watchdog_seconds=0.1)
    scheduler.start()
    try:
        assert _wait_for(lambda: enricher._thread is not dead)
    finally:
        scheduler.shutdown(wait=False)
    assert enricher.watchdog_restarts == 1
    enricher._thread.join(30)
    with session_factory() as session:
        assert session.get(Story, story_id).enrich_status != "pending"


def test_every_sqlite_engine_waits_15s_for_a_lock(tmp_path) -> None:
    from app import telemetry
    from app.db import make_engine

    engine = make_engine(f"sqlite:///{tmp_path / 'a.db'}")
    with engine.connect() as conn:  # stories, source_usage, search_provider_state
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 15000
    from sqlalchemy.orm import sessionmaker

    writer = telemetry._writer_factory(sessionmaker(bind=engine))  # the telemetry engine
    with writer() as session:
        assert session.connection().exec_driver_sql("PRAGMA busy_timeout").scalar() == 15000
        assert session.get_bind() is not engine
