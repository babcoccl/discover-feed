"""Background summary queue: a story's brief and report wait in ``summary_jobs`` (one row
per story and kind). Each run does briefs first (newest first, ``concurrency`` at a time, at
most ``max_per_run``), then reports (``report_concurrency`` at a time, at most
``max_reports_per_run``). Runs in its own thread (scheduler job, "Summarize now" or a story
page asking for its report), so ingestion, extraction and clustering never wait on it."""

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import sessionmaker

from app.config import ReportMode
from app.models import Story, StorySummary, SummaryJob, SummaryKind, SummaryStatus, utcnow
from app.summarize.service import KINDS, Kind, Summarizer, SummaryOutcome

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
    rejected: int = 0
    """Reports that failed validation (stored as failed)."""
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
    """Average generation speed over the last 20 generated briefs/reports."""
    seconds: dict[str, float | None] = {}
    """Median latency (all attempts) of the last 20 generated briefs / reports."""
    summaries: dict[str, int]
    """Stored brief versions by status."""
    reports: dict[str, int]
    """Stored report versions by status."""
    report_mode: str
    queued_reports: int


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
        report_mode: ReportMode = ReportMode.AUTO,
        report_auto_max_age_hours: float = 48,
        max_reports_per_run: int = 10,
        report_concurrency: int = 1,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.summarizer = summarizer
        self.concurrency = max(1, concurrency)
        self.max_per_run = max_per_run
        self.debounce = timedelta(minutes=debounce_minutes)
        self.failure_limit = max(1, failure_limit)
        self.pause = timedelta(minutes=pause_minutes)
        self.report_mode = ReportMode(report_mode)
        self.report_window = timedelta(hours=report_auto_max_age_hours)
        self.max_reports_per_run = max_reports_per_run
        self.report_concurrency = max(1, report_concurrency)
        self.clock = clock
        self.state = WorkerState()
        self._lock = threading.Lock()
        self._kick: dict | None = None

    @property
    def enabled(self) -> bool:
        return self.summarizer is not None

    @property
    def model(self) -> str | None:
        return self.summarizer.client.model if self.summarizer else None

    # --- queue -----------------------------------------------------------------------------

    def enqueue(
        self, story_ids: Iterable[int], *, force: bool = False, kind: Kind = "brief"
    ) -> int:
        """Queue stories' ``kind``; a story whose ``kind`` was generated less than
        ``debounce`` ago waits until then (briefs and reports debounce independently)."""
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
                        StorySummary.kind == kind,
                        StorySummary.status != SummaryStatus.FAILED,
                    )
                    .group_by(StorySummary.story_id)
                ).all()
            )
            for story_id in ids:
                job = session.get(SummaryJob, (story_id, kind))
                not_before = None
                if not force and story_id in last:
                    not_before = last[story_id] + self.debounce
                if not_before is not None and not_before <= now:
                    not_before = None
                if job is None:
                    session.add(
                        SummaryJob(
                            story_id=story_id,
                            kind=kind,
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

    def enqueue_all(self, *, force: bool = False, kind: str = "both") -> int:
        """Every story's brief, plus the reports ``report_mode: auto`` wants."""
        with self.session_factory() as session:
            ids = list(session.scalars(select(Story.id)))
        count = 0
        if kind in ("brief", "both"):
            count += self.enqueue(ids, force=force)
        if kind in ("report", "both"):
            count += self.enqueue_auto_reports(ids, force=force)
        return count

    def auto_report_ids(self, story_ids: Iterable[int] | None = None) -> list[int]:
        """Stories ``auto`` generates reports for ahead of time: multi-source stories updated
        within ``report_auto_max_age_hours``. Other stories get theirs on demand."""
        if self.report_mode is not ReportMode.AUTO:
            return []
        stmt = select(Story.id).where(
            Story.source_count >= 2, Story.last_updated_at >= self.clock() - self.report_window
        )
        if story_ids is not None:
            stmt = stmt.where(Story.id.in_(list(story_ids)))
        with self.session_factory() as session:
            return list(session.scalars(stmt))

    def enqueue_auto_reports(
        self, story_ids: Iterable[int] | None = None, *, force: bool = False
    ) -> int:
        return self.enqueue(self.auto_report_ids(story_ids), force=force, kind="report")

    def report_job(self, story_id: int) -> SummaryJob | None:
        with self.session_factory() as session:
            return session.get(SummaryJob, (story_id, SummaryKind.REPORT.value))

    def request_report(self, story_id: int) -> bool:
        """A story page was opened: queue its report unless it is current or reports are off;
        starts a run. True when a report job is waiting for this story."""
        if not self.enabled or self.report_mode is ReportMode.OFF:
            return False
        if self.report_job(story_id) is None:
            with self.session_factory() as session:
                if self.summarizer.is_current(session, story_id, SummaryKind.REPORT):
                    return False
            self.enqueue([story_id], kind="report")
        self.start()
        return True

    def queue_counts(self) -> dict[str, int]:
        now = self.clock()
        with self.session_factory() as session:
            total = session.scalar(select(func.count()).select_from(SummaryJob)) or 0
            due = session.scalar(select(func.count()).select_from(SummaryJob).where(_due(now))) or 0
        return {"queued": total, "due": due, "waiting": total - due}

    def _due_jobs(self, session, limit: int, story_id: int | None, kind: str) -> list[SummaryJob]:
        stmt = (
            select(SummaryJob)
            .join(Story, Story.id == SummaryJob.story_id, isouter=True)
            .where(SummaryJob.kind == kind)
        )
        if story_id is not None:
            stmt = stmt.where(SummaryJob.story_id == story_id)
        else:
            now = self.clock()
            stmt = stmt.where(_due(now))
        stmt = stmt.order_by(Story.last_updated_at.desc(), SummaryJob.story_id.desc()).limit(limit)
        return list(session.scalars(stmt))

    # --- running ---------------------------------------------------------------------------

    async def run(
        self,
        *,
        limit: int | None = None,
        story_id: int | None = None,
        manual: bool = False,
        kinds: Iterable[str] = KINDS,
    ) -> SummarizeRunResult:
        """Process due jobs (or only ``story_id``'s), briefs before reports. ``manual``
        ignores the failure pause."""
        if self.summarizer is None:
            return SummarizeRunResult(status="disabled")
        if not self._lock.acquire(blocking=False):
            self._kick = {}
            return SummarizeRunResult(status="busy")
        try:
            now = self.clock()
            if not manual and self.state.paused_until and self.state.paused_until > now:
                return SummarizeRunResult(status="paused", **self.queue_counts_only())
            if manual:
                self.state.paused_until = None
            self.state.running = True
            result = SummarizeRunResult()
            for kind in [k for k in KINDS if k in set(kinds)]:
                if kind == SummaryKind.REPORT:
                    budget, concurrency = self.max_reports_per_run, self.report_concurrency
                else:
                    budget, concurrency = self.max_per_run, self.concurrency
                with self.session_factory() as session:
                    jobs = [
                        (j.story_id, kind, j.force, j.attempts)
                        for j in self._due_jobs(session, limit or budget, story_id, kind)
                    ]
                semaphore = asyncio.Semaphore(concurrency)

                async def one(job: tuple[int, str, bool, int], semaphore=semaphore) -> None:
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
            if self._kick is not None:  # a story page asked for a report during this run
                self._kick = None
                self.start()

    def queue_counts_only(self) -> dict[str, int]:
        return {"queued": self.queue_counts()["queued"]}

    async def _summarize(
        self, story_id: int, kind: Kind, force: bool, attempts: int
    ) -> SummaryOutcome:
        try:
            outcome = await self.summarizer.summarize_story(
                self.session_factory, story_id, force=force, kind=kind
            )
        except Exception as exc:  # isolate: one broken story never stops the others
            logger.exception("summarizing story %s (%s) failed", story_id, kind)
            outcome = SummaryOutcome(
                story_id=story_id, kind=kind, status="failed", reason=f"error: {exc}"
            )
        with self.session_factory() as session:
            job = session.get(SummaryJob, (story_id, kind))
            if outcome.status == "failed":
                # Only brief failures pause the worker: a slow report timing out must not
                # stop the cards from being written. Report jobs back off on their own.
                if kind == SummaryKind.BRIEF:
                    self._record_failure(outcome.reason)
                else:
                    self.state.last_error = f"report: {outcome.reason}"
                    self.state.last_error_at = self.clock()
                if job is not None:
                    job.attempts = attempts + 1
                    job.last_error = outcome.reason
                    delay = min(2**job.attempts, MAX_RETRY_MINUTES)
                    job.next_attempt_at = self.clock() + timedelta(minutes=delay)
            else:
                self.state.consecutive_failures = 0
                if job is not None:
                    session.execute(
                        delete(SummaryJob).where(
                            SummaryJob.story_id == story_id, SummaryJob.kind == kind
                        )
                    )
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
        if not self.enabled:
            return False
        if self._lock.locked():
            self._kick = {}
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
            counts = {
                (kind, status): n
                for kind, status, n in session.execute(
                    select(StorySummary.kind, StorySummary.status, func.count()).group_by(
                        StorySummary.kind, StorySummary.status
                    )
                ).all()
            }
            queued_reports = session.scalar(
                select(func.count())
                .select_from(SummaryJob)
                .where(SummaryJob.kind == SummaryKind.REPORT)
            )
            seconds = {}
            for kind in KINDS:
                ms_list = sorted(
                    session.scalars(
                        select(StorySummary.latency_ms)
                        .where(StorySummary.kind == kind, StorySummary.latency_ms > 0)
                        .order_by(StorySummary.id.desc())
                        .limit(20)
                    )
                )
                seconds[kind] = round(ms_list[len(ms_list) // 2] / 1000, 1) if ms_list else None
        state = self.state
        return WorkerStatus(
            seconds=seconds,
            enabled=self.enabled,
            model=self.model,
            running=state.running,
            **self.queue_counts(),
            last_run_at=state.last_run_at or last_saved,
            last_error=state.last_error,
            last_error_at=state.last_error_at,
            paused_until=state.paused_until,
            tokens_per_second=round(tokens / (ms / 1000), 1) if ms and tokens else None,
            summaries={
                s.value: counts.get(("brief", s.value), 0)
                for s in SummaryStatus
                if s is not SummaryStatus.SKIPPED
            },
            reports={s.value: counts.get(("report", s.value), 0) for s in SummaryStatus},
            report_mode=self.report_mode.value,
            queued_reports=queued_reports or 0,
        )

    def enqueue_missing(self) -> int:
        """Queue briefs never generated (and reports ``auto`` wants), e.g. after upgrading a DB
        or a prompt version bump (old briefs stay visible until replaced)."""
        count = 0
        candidates = {"brief": None, "report": self.auto_report_ids()}
        for kind, only in candidates.items():
            if only == []:
                continue
            stmt = select(Story.id).where(
                ~Story.id.in_(
                    select(StorySummary.story_id).where(
                        StorySummary.story_id.is_not(None), StorySummary.kind == kind
                    )
                ),
                ~Story.id.in_(select(SummaryJob.story_id).where(SummaryJob.kind == kind)),
            )
            if only is not None:
                stmt = stmt.where(Story.id.in_(only))
            with self.session_factory() as session:
                ids = list(session.scalars(stmt))
            count += self.enqueue(ids, kind=kind)
        return count
