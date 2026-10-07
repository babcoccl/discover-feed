from apscheduler.triggers.interval import IntervalTrigger

from app.config import Source
from app.ingest import Ingestor
from app.scheduler import create_scheduler, schedule_ingestion


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
