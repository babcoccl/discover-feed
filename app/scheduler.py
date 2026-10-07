import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Source
from app.ingest import Ingestor


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
