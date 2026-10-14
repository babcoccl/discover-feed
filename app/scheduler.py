import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Source
from app.images.service import ImageWorker
from app.ingest import Ingestor
from app.pipeline import Pipeline
from app.summarize.worker import SummaryWorker


def create_scheduler() -> BackgroundScheduler:
    return BackgroundScheduler(timezone="UTC")


def schedule_ingestion(
    scheduler: BackgroundScheduler,
    sources: Sequence[Source],
    ingestor: Ingestor,
    *,
    jitter_seconds: int = 60,
) -> None:
    """One interval job per source (every ``refresh_minutes``, plus jitter).

    The first run happens shortly after startup, spread over the jitter window.
    """
    now = datetime.now(UTC)
    for source in sources:
        # Keep jitter well under half an interval so it can't trip the backoff grace window.
        jitter = max(0, min(jitter_seconds, source.refresh_minutes * 60 // 4))
        scheduler.add_job(
            ingestor.run_blocking,
            trigger=IntervalTrigger(minutes=source.refresh_minutes, jitter=jitter or None),
            args=[[source]],
            id=f"ingest:{source.id}",
            name=f"Ingest {source.name}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            next_run_time=now + timedelta(seconds=random.uniform(1, max(jitter, 1))),
        )


def sync_ingestion_jobs(
    scheduler: BackgroundScheduler,
    sources: Sequence[Source],
    ingestor: Ingestor,
    *,
    jitter_seconds: int = 60,
) -> None:
    """Make the ``ingest:*`` jobs match ``sources`` after sources are added, edited,
    disabled or removed: unchanged jobs keep their schedule."""
    jobs = {job.id: job for job in scheduler.get_jobs() if job.id.startswith("ingest:")}
    changed = []
    for source in sources:
        job = jobs.pop(f"ingest:{source.id}", None)
        if job is None or list(job.args) != [[source]]:
            changed.append(source)
    for job_id in jobs:
        scheduler.remove_job(job_id)
    schedule_ingestion(scheduler, changed, ingestor, jitter_seconds=jitter_seconds)


def schedule_pipeline(
    scheduler: BackgroundScheduler,
    pipeline: Pipeline,
    *,
    interval_seconds: int = 60,
    first_run_delay_seconds: int = 10,
) -> None:
    """Extract + cluster on a short interval (each run only touches new or pending
    articles), so new stories move through text extraction and clustering in about a
    minute; the generation worker is kicked after each run."""
    scheduler.add_job(
        pipeline.run_blocking,
        trigger=IntervalTrigger(seconds=interval_seconds),
        id="pipeline",
        name="Extract text and cluster stories",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(UTC) + timedelta(seconds=first_run_delay_seconds),
    )


def schedule_image_backfill(
    scheduler: BackgroundScheduler,
    worker: ImageWorker,
    *,
    interval_minutes: float = 10,
    first_run_delay_seconds: int = 30,
) -> None:
    """Images for recently published stories that predate image support (or whose check
    was interrupted): a few articles per run, so it is rate limited and resumable."""
    scheduler.add_job(
        worker.backfill_blocking,
        trigger=IntervalTrigger(seconds=interval_minutes * 60),
        id="images-backfill",
        name="Backfill story images",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(UTC) + timedelta(seconds=first_run_delay_seconds),
    )


def schedule_summaries(
    scheduler: BackgroundScheduler,
    worker: SummaryWorker,
    *,
    interval_seconds: int = 60,
    first_run_delay_seconds: int = 15,
) -> None:
    """Generation worker: retries after backoff and publish timers (report wait, brief
    fallback) come due here."""
    scheduler.add_job(
        worker.run_blocking,
        trigger=IntervalTrigger(seconds=interval_seconds),
        id="summarize",
        name="Summarize stories",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(UTC) + timedelta(seconds=first_run_delay_seconds),
    )


def schedule_chat(scheduler: BackgroundScheduler, hub) -> None:
    """Re-check a chat endpoint that was down (every minute) and delete idle conversations
    (hourly, ``chat_retention_days``)."""
    if not hub.services:
        return
    now = datetime.now(UTC)
    scheduler.add_job(
        hub.recheck_blocking,
        trigger=IntervalTrigger(seconds=60),
        id="chat-health",
        name="Re-check chat endpoints",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        next_run_time=now + timedelta(seconds=60),
    )
    scheduler.add_job(
        hub.cleanup_blocking,
        trigger=IntervalTrigger(hours=1),
        id="chat-retention",
        name="Delete idle conversations",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        next_run_time=now + timedelta(minutes=5),
    )


def schedule_alerts(scheduler: BackgroundScheduler, engine, sender) -> None:
    """Retry due notifications every 15 s; digests and retention every minute (new alerts
    are sent right away, not on this timer)."""
    scheduler.add_job(
        sender.send_due,
        trigger=IntervalTrigger(seconds=15),
        id="alerts-send",
        name="Send queued notifications",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        engine.maintain,
        trigger=IntervalTrigger(seconds=60),
        id="alerts-maintenance",
        name="Alert digests and retention",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
