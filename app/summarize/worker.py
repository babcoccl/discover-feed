"""Background generation, one story at a time (newest first, Prioritize first): its brief,
then its report when eligible, then the publish decision (``app.publish.sync_story``) before
the next story starts. Runs in its own thread (scheduler job, after each pipeline run, or
an admin action); pages never start or wait on it. ``summary_jobs`` only holds retries
(backoff after a failure) and forced regenerations (Retry / Regenerate)."""

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import sessionmaker

from app import profiles as repo
from app.lifecycle import PRE_CLUSTER
from app.models import (
    GenerationAttempt,
    Story,
    StoryState,
    StorySummary,
    SummaryJob,
    SummaryKind,
    SummaryStatus,
    utcnow,
)
from app.publish import Policy, assess, sync_story
from app.summarize.service import KINDS, Kind, Summarizer, SummaryOutcome
from app.worker_status import SummarizeRunResult, WorkerState, WorkerStatus

logger = logging.getLogger(__name__)

MAX_RETRY_MINUTES = 6 * 60
REPORT_RETRY_LIMIT = 3
"""Endpoint failures (timeouts, HTTP errors) a report retries before it is final ``failed``."""
WORK_STATES = (
    StoryState.CLUSTERED.value,
    StoryState.BRIEF_READY.value,
    StoryState.REPORT_FINAL.value,
)
PAUSED_FOREVER = timedelta(days=36500)


def _due(now: datetime):
    return or_(SummaryJob.next_attempt_at.is_(None), SummaryJob.next_attempt_at <= now)


class SummaryWorker:
    def __init__(
        self,
        session_factory: sessionmaker,
        summarizer: Summarizer | None,
        *,
        max_per_run: int = 25,
        failure_limit: int = 3,
        pause_minutes: float = 10,
        policy: Policy | None = None,
        backfill_hours: float = 24,
        backfill_max: int = 40,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.summarizer = summarizer
        self.max_per_run = max_per_run
        self.failure_limit = max(1, failure_limit)
        self.pause = timedelta(minutes=pause_minutes)
        self.policy = policy or Policy()
        self.backfill = timedelta(hours=backfill_hours)
        self.backfill_max = backfill_max
        self.clock = clock
        self.state = WorkerState()
        self._lock = threading.Lock()
        self._kick: dict | None = None
        if summarizer is not None:
            summarizer.on_save = self._on_save

    @property
    def enabled(self) -> bool:
        return self.summarizer is not None

    @property
    def model(self) -> str | None:
        return self.summarizer.client.model if self.summarizer else None

    @property
    def report_mode(self):
        return self.policy.report_mode

    def _on_save(self, session, story_id: int) -> None:
        sync_story(
            session, story_id, summarizer=self.summarizer, policy=self.policy, now=self.clock()
        )

    # --- queue -----------------------------------------------------------------------------

    def enqueue(
        self, story_ids: Iterable[int], *, force: bool = False, kind: Kind = "brief"
    ) -> int:
        """Ask for ``kind`` again: ``force`` regenerates even when the stored one is current
        (Retry / Regenerate); otherwise clears any backoff so the next run tries it."""
        ids = sorted(set(story_ids))
        now = self.clock()
        with self.session_factory() as session:
            for story_id in ids:
                job = session.get(SummaryJob, (story_id, kind))
                if job is None:
                    session.add(
                        SummaryJob(story_id=story_id, kind=kind, queued_at=now, force=force)
                    )
                else:
                    job.force = job.force or force
                    job.attempts, job.next_attempt_at = 0, None
            session.commit()
        return len(ids)

    def enqueue_all(self, *, force: bool = False, kind: str = "both") -> int:
        with self.session_factory() as session:
            ids = list(session.scalars(select(Story.id).where(Story.state.in_(_LIVE))))
        count = 0
        for k in KINDS:
            if kind in (k, "both"):
                count = max(count, self.enqueue(ids, force=force, kind=k))
        return count

    def enqueue_missing(self) -> int:
        """Stories waiting for generation (states are derived; nothing to queue)."""
        with self.session_factory() as session:
            return len(self.candidates(session))

    def report_job(self, story_id: int) -> SummaryJob | None:
        with self.session_factory() as session:
            return session.get(SummaryJob, (story_id, SummaryKind.REPORT.value))

    def prioritize(self, story_id: int) -> bool:
        with self.session_factory() as session:
            story = session.get(Story, story_id)
            if story is None:
                return False
            top = session.scalar(select(func.max(Story.priority))) or 0
            story.priority = top + 1
            session.commit()
        return True

    def retry(self, story_id: int, kind: str = "both") -> bool:
        with self.session_factory() as session:
            if session.get(Story, story_id) is None:
                return False
        for k in KINDS:
            if kind in (k, "both"):
                self.enqueue([story_id], force=True, kind=k)
        self.prioritize(story_id)
        return True

    def set_paused(self, paused: bool) -> None:
        state = self.state
        state.paused_by_admin = paused
        state.paused_until = self.clock() + PAUSED_FOREVER if paused else None
        if not paused:
            state.consecutive_failures = 0

    def candidates(self, session, story_id: int | None = None) -> list[int]:
        """Stories to work on, in order: Prioritize first, then newest. Never-published
        stories must be updated within ``backfill_hours`` (at most ``backfill_max``)."""
        now = self.clock()
        backing_off = select(SummaryJob.story_id).where(SummaryJob.next_attempt_at > now)
        # Asked for explicitly (Retry, Regenerate, the admin API) or a retry that came due.
        requested = select(SummaryJob.story_id).where(_due(now))
        # A brief that failed for good (no retry queued) waits for the fallback timer.
        brief_failed = (Story.state == StoryState.CLUSTERED) & func.coalesce(
            Story.state_reason, ""
        ).startswith("brief failed")
        stmt = select(Story.id, Story.published_version).where(
            Story.state.in_(_LIVE),
            or_(Story.state.in_(WORK_STATES), Story.id.in_(requested)),
            ~Story.id.in_(backing_off) | Story.id.in_(requested),
            ~brief_failed | Story.id.in_(requested),
            or_(
                Story.published_version.is_not(None),
                Story.last_updated_at >= now - self.backfill,
                Story.id.in_(requested),
            ),
        )
        if story_id is not None:
            stmt = select(Story.id, Story.published_version).where(Story.id == story_id)
        stmt = stmt.order_by(Story.priority.desc(), Story.last_updated_at.desc(), Story.id.desc())
        ids, unpublished = [], 0
        for sid, version in session.execute(stmt).all():
            if version is None and story_id is None:
                unpublished += 1
                if unpublished > self.backfill_max:
                    continue
            ids.append(sid)
        return ids

    def queue_counts(self) -> dict[str, int]:
        now = self.clock()
        with self.session_factory() as session:
            queued = len(self.candidates(session))
            waiting = (
                session.scalar(select(func.count()).select_from(SummaryJob).where(~_due(now))) or 0
            )
        return {"queued": queued, "due": queued, "waiting": waiting}

    # --- running ---------------------------------------------------------------------------

    async def run(
        self,
        *,
        limit: int | None = None,
        story_id: int | None = None,
        manual: bool = False,
        kinds: Iterable[str] = KINDS,
    ) -> SummarizeRunResult:
        """Sweep publish timers, then work through stories one at a time. ``manual``
        ignores an endpoint-failure pause (not an admin pause)."""
        if self.summarizer is None:
            return SummarizeRunResult(status="disabled")
        if not self._lock.acquire(blocking=False):
            self._kick = {}
            return SummarizeRunResult(status="busy")
        try:
            kinds = set(kinds)
            self.sweep()
            now = self.clock()
            if self._paused(now, manual):
                return SummarizeRunResult(status="paused", **self.queue_counts_only())
            if manual and not self.state.paused_by_admin:
                self.state.paused_until = None
            self.state.running = True
            result = SummarizeRunResult()
            budget = limit or self.max_per_run  # stories needing the LLM (cache hits are free)
            done: set[int] = set()
            while budget > 0 and not self._paused(self.clock(), manual):
                with self.session_factory() as session:
                    ids = [i for i in self.candidates(session, story_id) if i not in done]
                if not ids:
                    break
                for sid in ids:
                    if budget <= 0 or self._paused(self.clock(), manual):
                        break
                    done.add(sid)
                    self.state.current_story = sid
                    outcomes = await self.process(sid, kinds, manual=manual)
                    result.outcomes.extend(outcomes)
                    if any(o.status not in ("cached", "reused", "skipped") for o in outcomes):
                        budget -= 1
            for outcome in result.outcomes:
                result.processed += 1
                key = outcome.status
                if key in ("reused", "skipped"):
                    key = "cached"
                setattr(result, key, getattr(result, key) + 1)
            if self.state.paused_until and self.state.paused_until > self.clock():
                result.status = "paused"
            result.queued = self.queue_counts()["queued"]
            self.state.last_run_at = self.clock()
            return result
        finally:
            self.state.running = False
            self.state.current_story = None
            self._lock.release()
            if self._kick is not None:
                self._kick = None
                self.start()

    def _paused(self, now: datetime, manual: bool) -> bool:
        until = self.state.paused_until
        if until is None or until <= now:
            return False
        return self.state.paused_by_admin or not manual

    def sweep(self) -> int:
        """Re-check the publish rule for every story waiting on a timer (no LLM)."""
        with self.session_factory() as session:
            ids = list(
                session.scalars(
                    select(Story.id).where(
                        Story.state.in_(WORK_STATES),
                        or_(Story.brief_ready_at.is_not(None), Story.clustered_at.is_not(None)),
                    )
                )
            )
            for sid in ids:
                sync_story(
                    session, sid, summarizer=self.summarizer, policy=self.policy, now=self.clock()
                )
            session.commit()
        return len(ids)

    async def process(
        self, story_id: int, kinds: Iterable[str] = KINDS, *, manual: bool = False
    ) -> list[SummaryOutcome]:
        """One story to its publish decision: brief, then report, then ``sync_story``."""
        kinds = set(kinds)
        outcomes: list[SummaryOutcome] = []
        for kind in KINDS:
            if kind not in kinds:
                continue
            todo = self._todo(story_id, kind, manual)
            if todo is None:
                continue
            force, attempts = todo
            outcome = await self._summarize(story_id, kind, force, attempts)
            outcomes.append(outcome)
            if kind == SummaryKind.BRIEF and outcome.status == "failed":
                break
        with self.session_factory() as session:
            sync_story(
                session, story_id, summarizer=self.summarizer, policy=self.policy, now=self.clock()
            )
            session.commit()
        return outcomes

    def _todo(self, story_id: int, kind: str, manual: bool) -> tuple[bool, int] | None:
        """``(force, attempts)`` when ``kind`` should be generated now, else None."""
        now = self.clock()
        with self.session_factory() as session:
            story = session.get(Story, story_id)
            item = repo.get_story(session, story_id)
            if story is None or item is None or story.state in _DEAD:
                return None
            job = session.get(SummaryJob, (story_id, kind))
            if job is not None and job.next_attempt_at and job.next_attempt_at > now and not manual:
                return None
            if job is not None and job.force:
                return True, job.attempts
            a = assess(
                session, story_id, item, repo.source_names(session), self.summarizer, self.policy
            )
        attempts = job.attempts if job is not None else 0
        if kind == SummaryKind.BRIEF:
            return (False, attempts) if a.brief == "pending" or job is not None else None
        if a.brief != "valid" or self.policy.reports_off:
            return None
        return (False, attempts) if a.report == "pending" else None

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
            retry = outcome.status == "failed"
            if retry and kind == SummaryKind.BRIEF:
                self._record_failure(outcome.reason)
            elif retry:
                # Report failures never pause the worker: briefs keep coming.
                self.state.last_error = f"report: {outcome.reason}"
                self.state.last_error_at = self.clock()
                retry = attempts + 1 < REPORT_RETRY_LIMIT
            else:
                self.state.consecutive_failures = 0
            if retry:
                if job is None:
                    job = SummaryJob(story_id=story_id, kind=kind, queued_at=self.clock())
                    session.add(job)
                job.force = job.force or force  # a Retry stays forced across backoff
                job.attempts = attempts + 1
                job.last_error = outcome.reason
                delay = min(2**job.attempts, MAX_RETRY_MINUTES)
                job.next_attempt_at = self.clock() + timedelta(minutes=delay)
            elif job is not None:
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
        if state.consecutive_failures >= self.failure_limit and not state.paused_by_admin:
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
                select(func.count()).select_from(Story).where(Story.state == StoryState.BRIEF_READY)
            )
        state = self.state
        return WorkerStatus(
            seconds=median_seconds(self.session_factory),
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
            paused_by_admin=state.paused_by_admin,
            current_story=state.current_story,
        )


_LIVE = (*WORK_STATES, StoryState.PUBLISHED.value)
_DEAD = (*(s.value for s in PRE_CLUSTER), StoryState.MERGED.value)


def median_seconds(session_factory: sessionmaker) -> dict[str, float | None]:
    """Median latency of the last 20 successful generation attempts per kind (for ETAs)."""
    seconds: dict[str, float | None] = {}
    with session_factory() as session:
        for kind in KINDS:
            ms_list = sorted(
                v
                for v in session.scalars(
                    select(GenerationAttempt.latency_ms)
                    .where(GenerationAttempt.kind == kind, GenerationAttempt.outcome == "ok")
                    .order_by(GenerationAttempt.id.desc())
                    .limit(20)
                )
                if v
            )
            if not ms_list:
                ms_list = sorted(
                    session.scalars(
                        select(StorySummary.latency_ms)
                        .where(StorySummary.kind == kind, StorySummary.latency_ms > 0)
                        .order_by(StorySummary.id.desc())
                        .limit(20)
                    )
                )
            seconds[kind] = round(ms_list[len(ms_list) // 2] / 1000, 1) if ms_list else None
    return seconds
