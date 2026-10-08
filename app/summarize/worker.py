"""Background summary queue: stories wait in ``summary_jobs`` and are summarized newest
first, ``concurrency`` at a time and at most ``max_per_run`` per run. Runs in its own thread
(scheduler job or "Summarize now"), so ingestion, extraction and clustering never wait on it."""

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import sessionmaker

from app.models import Story, StorySummary, SummaryJob, SummaryStatus, utcnow
from app.summarize.service import Summarizer, SummaryOutcome

logger = logging.getLogger(__name__)

MAX_RETRY_MINUTES = 6 * 60


def _due(now: datetime):
    return or_(SummaryJob.next_attempt_at.is_(None), SummaryJob.next_attempt_at <= now)


class SummarizeRunResult(BaseModel):
    status: str = "ok"
    """``ok``, ``busy`` (a run is in progress), ``disabled`` (no summarizer), ``paused``."""
    processed: int = 0
    ok: int = 0
    fallback: int = 0
    failed: int = 0
    cached: int = 0
    queued: int = 0
    """Jobs still waiting after this run."""
    outcomes: list[SummaryOutcome] = []


class WorkerStatus(BaseModel):
    enabled: bool
    model: str | None
    running: bool
    queued: int
    due: int
    waiting: int
    """Queued but debounced or backing off after a failure."""
    last_run_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    paused_until: datetime | None
    tokens_per_second: float | None
    """Average generation speed over the last 20 generated summaries."""
    summaries: dict[str, int]
    """Stored summary versions by status."""


@dataclass
class WorkerState:
    running: bool = False
    last_run_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int = 0
    paused_until: datetime | None = None


class SummaryWorker:
    def __init__(
        self,
        session_factory: sessionmaker,
        summarizer: Summarizer | None,
        *,
        concurrency: int = 1,
        max_per_run: int = 25,
        debounce_minutes: float = 30,
        failure_limit: int = 3,
        pause_minutes: float = 10,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.summarizer = summarizer
        self.concurrency = max(1, concurrency)
        self.max_per_run = max_per_run
        self.debounce = timedelta(minutes=debounce_minutes)
        self.failure_limit = max(1, failure_limit)
        self.pause = timedelta(minutes=pause_minutes)
        self.clock = clock
        self.state = WorkerState()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.summarizer is not None

    @property
    def model(self) -> str | None:
        return self.summarizer.client.model if self.summarizer else None

    # --- queue -----------------------------------------------------------------------------

    def enqueue(self, story_ids: Iterable[int], *, force: bool = False) -> int:
        """Queue stories; a story summarized less than ``debounce`` ago waits until then."""
        ids = sorted(set(story_ids))
        if not ids:
            return 0
        now = self.clock()
        with self.session_factory() as session:
            last = dict(
                session.execute(
                    select(StorySummary.story_id, func.max(StorySummary.created_at))
                    .where(
                        StorySummary.story_id.in_(ids),
                        StorySummary.status != SummaryStatus.FAILED,
                    )
                    .group_by(StorySummary.story_id)
                ).all()
            )
            for story_id in ids:
                job = session.get(SummaryJob, story_id)
                not_before = None
                if not force and story_id in last:
                    not_before = last[story_id] + self.debounce
                if not_before is not None and not_before <= now:
                    not_before = None
                if job is None:
                    session.add(
                        SummaryJob(
                            story_id=story_id,
                            queued_at=now,
                            force=force,
                            next_attempt_at=not_before,
                        )
                    )
                else:
                    job.force = job.force or force
                    job.attempts = 0
                    if force:
                        job.next_attempt_at = None
                    elif not_before is not None:
                        job.next_attempt_at = max(job.next_attempt_at or not_before, not_before)
            session.commit()
        return len(ids)

    def enqueue_all(self, *, force: bool = False) -> int:
        with self.session_factory() as session:
            ids = list(session.scalars(select(Story.id)))
        return self.enqueue(ids, force=force)

    def queue_counts(self) -> dict[str, int]:
        now = self.clock()
        with self.session_factory() as session:
            total = session.scalar(select(func.count()).select_from(SummaryJob)) or 0
            due = session.scalar(select(func.count()).select_from(SummaryJob).where(_due(now))) or 0
        return {"queued": total, "due": due, "waiting": total - due}

    def _due_jobs(self, session, limit: int, story_id: int | None) -> list[SummaryJob]:
        stmt = select(SummaryJob).join(Story, Story.id == SummaryJob.story_id, isouter=True)
        if story_id is not None:
            stmt = stmt.where(SummaryJob.story_id == story_id)
        else:
            now = self.clock()
            stmt = stmt.where(_due(now))
        stmt = stmt.order_by(Story.last_updated_at.desc(), SummaryJob.story_id.desc()).limit(limit)
        return list(session.scalars(stmt))

    # --- running ---------------------------------------------------------------------------

    async def run(
        self, *, limit: int | None = None, story_id: int | None = None, manual: bool = False
    ) -> SummarizeRunResult:
        """Process due jobs (or only ``story_id``). ``manual`` ignores the failure pause."""
        if self.summarizer is None:
            return SummarizeRunResult(status="disabled")
        if not self._lock.acquire(blocking=False):
            return SummarizeRunResult(status="busy")
        try:
            now = self.clock()
            if not manual and self.state.paused_until and self.state.paused_until > now:
                return SummarizeRunResult(status="paused", **self.queue_counts_only())
            if manual:
                self.state.paused_until = None
            self.state.running = True
            with self.session_factory() as session:
                jobs = [
                    (j.story_id, j.force, j.attempts)
                    for j in self._due_jobs(session, limit or self.max_per_run, story_id)
                ]
            result = SummarizeRunResult()
            semaphore = asyncio.Semaphore(self.concurrency)

            async def one(job: tuple[int, bool, int]) -> None:
                async with semaphore:
                    if self.state.paused_until and not manual:
                        return
                    outcome = await self._summarize(*job)
                    result.outcomes.append(outcome)

            await asyncio.gather(*(one(job) for job in jobs))
            for outcome in result.outcomes:
                result.processed += 1
                key = outcome.status
                if key in ("reused", "skipped"):
                    key = "cached"
                setattr(result, key, getattr(result, key) + 1)
            if self.state.paused_until:
                result.status = "paused"
            result.queued = self.queue_counts()["queued"]
            self.state.last_run_at = self.clock()
            return result
        finally:
            self.state.running = False
            self._lock.release()

    def queue_counts_only(self) -> dict[str, int]:
        return {"queued": self.queue_counts()["queued"]}

    async def _summarize(self, story_id: int, force: bool, attempts: int) -> SummaryOutcome:
        try:
            outcome = await self.summarizer.summarize_story(
                self.session_factory, story_id, force=force
            )
        except Exception as exc:  # isolate: one broken story never stops the others
            logger.exception("summarizing story %s failed", story_id)
            outcome = SummaryOutcome(story_id=story_id, status="failed", reason=f"error: {exc}")
        with self.session_factory() as session:
            job = session.get(SummaryJob, story_id)
            if outcome.status == "failed":
                self._record_failure(outcome.reason)
                if job is not None:
                    job.attempts = attempts + 1
                    job.last_error = outcome.reason
                    delay = min(2**job.attempts, MAX_RETRY_MINUTES)
                    job.next_attempt_at = self.clock() + timedelta(minutes=delay)
            else:
                self.state.consecutive_failures = 0
                if job is not None:
                    session.execute(delete(SummaryJob).where(SummaryJob.story_id == story_id))
            session.commit()
        return outcome

    def _record_failure(self, reason: str | None) -> None:
        state = self.state
        state.last_error, state.last_error_at = reason, self.clock()
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.failure_limit:
            state.paused_until = self.clock() + self.pause
            logger.warning(
                "summarizer: %d endpoint failures in a row, pausing until %s",
                state.consecutive_failures,
                state.paused_until.isoformat(timespec="seconds"),
            )

    def run_blocking(self) -> None:
        """Entry point for the scheduler thread."""
        result = asyncio.run(self.run())
        if result.processed:
            logger.info("summaries %s", result.model_dump(exclude={"outcomes"}))

    def start(self, **kwargs) -> bool:
        """Run in a background thread now; False if a run is already in progress."""
        if self._lock.locked() or not self.enabled:
            return False
        threading.Thread(
            target=lambda: asyncio.run(self.run(**kwargs)), name="summarize", daemon=True
        ).start()
        return True

    # --- status ----------------------------------------------------------------------------

    def status(self) -> WorkerStatus:
        """For the Settings > Pipeline panel and ``POST /api/admin/summarize``."""
        with self.session_factory() as session:
            recent = (
                select(StorySummary.latency_ms, StorySummary.completion_tokens)
                .where(
                    StorySummary.status != SummaryStatus.FAILED,
                    StorySummary.latency_ms > 0,
                    StorySummary.completion_tokens > 0,
                )
                .order_by(StorySummary.id.desc())
                .limit(20)
                .subquery()
            )
            ms, tokens = session.execute(
                select(func.sum(recent.c.latency_ms), func.sum(recent.c.completion_tokens))
            ).one()
            last_saved = session.scalar(select(func.max(StorySummary.created_at)))
            counts = dict(
                session.execute(
                    select(StorySummary.status, func.count()).group_by(StorySummary.status)
                ).all()
            )
        state = self.state
        return WorkerStatus(
            enabled=self.enabled,
            model=self.model,
            running=state.running,
            **self.queue_counts(),
            last_run_at=state.last_run_at or last_saved,
            last_error=state.last_error,
            last_error_at=state.last_error_at,
            paused_until=state.paused_until,
            tokens_per_second=round(tokens / (ms / 1000), 1) if ms and tokens else None,
            summaries={s.value: counts.get(s.value, 0) for s in SummaryStatus},
        )

    def enqueue_missing(self) -> int:
        """Queue stories that have never been summarized (e.g. after upgrading a DB)."""
        with self.session_factory() as session:
            ids = list(
                session.scalars(
                    select(Story.id).where(
                        ~Story.id.in_(
                            select(StorySummary.story_id).where(StorySummary.story_id.is_not(None))
                        ),
                        ~Story.id.in_(select(SummaryJob.story_id)),
                    )
                )
            )
        return self.enqueue(ids)
