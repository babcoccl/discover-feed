"""The enrichment stage (between clustering and the brief).

New or under-covered stories (fewer than ``target_sources`` publishers) are marked
``enrich_status = pending``; the summary worker holds their brief for up to ``wait_seconds``.
A background thread searches the configured providers with headline-only queries, filters
and fetches candidates, and pins accepted pages to the story as ``origin = enrichment``
members. Those never fire alerts, never become cards and never make a story visible in a
profile by themselves (see ``story_views.load`` / ``profiles.get_story``).
"""

import asyncio
import logging
import re
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import AppConfig, EnrichmentConfig, LLMRole
from app.enrich.domains import SOCIAL, canonical_domain, registrable_domain
from app.enrich.fetch import CandidateFetcher, FetchedPage
from app.enrich.filters import (
    StoryContext,
    pre_filter,
    shared_entities,
    similarity,
    snippet_check,
    text_check,
    title_check,
)
from app.enrich.quality import flag_reason
from app.enrich.query import (
    build_queries,
    deep_queries,
    drop_short_terms,
    provider_queries,
    signal_tokens,
    strip_boilerplate,
)
from app.lifecycle import PRE_CLUSTER
from app.models import (
    CONTEXT,
    ENRICHMENT,
    NON_FEED,
    AlertFiring,
    Article,
    BlockedDomain,
    EnrichmentCandidate,
    ProfileRecord,
    SearchProviderState,
    SourceUsage,
    Story,
    StoryState,
    TextStatus,
    profile_sources,
    utcnow,
)
from app.normalize import canonicalize_url, content_hash
from app.search.base import PROVIDERS, SearchHit
from app.search.service import SearchService, budget_use

logger = logging.getLogger(__name__)


def _provider_cls(name: str):
    """The provider class for ``name`` — the ``PROVIDERS`` map fills lazily inside
    ``create_provider``, so import the modules that register first (deep-tier checks
    like ``supports_pages`` must not silently miss before any provider ran)."""
    from app.search import exa, providers, tinyfish  # noqa: F401

    return PROVIDERS.get(name)


STALE_PENDING = timedelta(hours=1)
"""A ``pending`` mark older than this (the process died mid-run) is picked up again."""
BATCH = 20
LOOP_RETRY_SECONDS = 5.0
"""The worker loop sleeps this long after an unexpected error, then tries again."""
RETRY_FIRST = timedelta(minutes=2)
RETRY_MAX = timedelta(minutes=60)
UNAVAILABLE = "provider_unavailable"
SKIPPED = "skipped:"
"""``enrich_reason`` prefix of stories not searched: ``skipped:pattern`` (the headline
matches ``skip_title_patterns``) or ``skipped:no_signal`` (too few names / numbers)."""
QUERY_TOO_SHORT = "query_too_short"
RECOVERED = "recovered"
DEGRADED = "degraded"
"""``enrich_reason`` prefix when the primary provider was unavailable and only a fallback
answered: the story is retried (``retry``) until the primary answers again."""
DEGRADED_MAX_ATTEMPTS = 3
"""Degraded passes in a row before the story is marked done with what the fallback found."""
DEEP_USAGE = "enrich:deep"
"""``source_usage`` key the deep tier's request budget (``enrichment.depth``) is kept under."""


def retry_delay(attempts: int) -> timedelta:
    """Backoff after ``attempts`` failed passes in a row: 2 min doubling to 60 min."""
    return min(RETRY_MAX, RETRY_FIRST * 2 ** max(0, attempts - 1))


def web_source_id(url: str) -> str:
    return f"web:{registrable_domain(url)}"


@dataclass
class Candidate:
    hit: SearchHit
    provider: str
    query: str
    decision: str = "rejected"
    reason: str = ""
    similarity: float | None = None
    title_similarity: float | None = None
    shared_entities: int | None = None
    text_similarity: float | None = None
    page: FetchedPage | None = None
    article_id: int | None = None
    rule: str = ""
    """Which acceptance rule passed: ``""`` for rule A, ``b`` / ``c`` / ``d`` for the
    extra rules (the candidate's ``via``)."""
    flag: str = ""
    """Source-quality flag (``tld:``/``name:``) on accepted candidates whose domain
    looks like a content farm — flagged, never rejected, still added and cited."""
    fetch_detail: str = ""
    """Why the fetch failed (HTTP status, timeout, size cap, extraction, robots) —
    for the ``fetch_failed`` breakdown in evaluate."""

    def as_dict(self) -> dict:
        return {
            "url": self.hit.url,
            "domain": self.hit.domain,
            "title": self.hit.title,
            "provider": self.provider,
            "decision": self.decision,
            "reason": self.reason,
            "similarity": self.similarity,
            "title_similarity": self.title_similarity,
            "shared_entities": self.shared_entities,
            "text_similarity": self.text_similarity,
            "rule": self.rule,
            "flag": self.flag,
            "fetch_detail": self.fetch_detail,
        }

    @property
    def score(self) -> float:
        return self.similarity if self.similarity is not None else self.title_similarity or 0.0


@dataclass
class StoryEnrichment:
    story_id: int
    queries: list[str] = field(default_factory=list)
    query_mode: str = "heuristic"
    searches: list[dict] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    publishers_before: int = 0
    added: int = 0
    status: str = "done"
    reason: str | None = None
    tier: str = "standard"
    """``deep`` (``enrichment.depth`` admission: publishers / priority / alert) | standard."""
    answered: bool = False
    """At least one provider answered (hits or none); otherwise the story is retried."""
    deep_requests: int = 0
    """HTTP requests the deep tier sent for this story (recorded against the day budget)."""
    degraded: str | None = None
    """The primary provider's error when only a fallback answered (a degraded pass)."""
    retry_at: datetime | None = None

    @property
    def accepted(self) -> list[Candidate]:
        return [c for c in self.candidates if c.decision == "accepted"]

    @property
    def link_only(self) -> list[Candidate]:
        """Members stored without text (``enrichment.link_only``): never summarized."""
        return [c for c in self.candidates if c.decision == "link_only"]

    @property
    def headline_only(self) -> list[Candidate]:
        """Unfetchable members kept for the coverage panel (headline + time only)."""
        return [c for c in self.candidates if c.decision == "headline_only"]

    @property
    def untexted(self) -> list[Candidate]:
        """Members stored with ``text_status = none`` (link_only and headline_only)."""
        return [c for c in self.candidates if c.decision in ("link_only", "headline_only")]


class Enricher:
    def __init__(
        self,
        session_factory: sessionmaker,
        config: EnrichmentConfig,
        *,
        profile_defaults: dict[str, bool] | None = None,
        search: SearchService | None = None,
        fetcher: CandidateFetcher | None = None,
        llm_role: LLMRole | None = None,
        llm_transport=None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.config = config
        self.profile_defaults = profile_defaults or {}
        """YAML ``enrichment.enabled`` per profile slug (a Settings toggle overrides it)."""
        self.search = search or SearchService(session_factory)
        self.fetcher = fetcher or CandidateFetcher(session_factory)
        self.llm_role = llm_role
        self.llm_transport = llm_transport
        self.now = now
        self.on_done: Callable[[list[int]], None] | None = None
        """Called with the story ids that finished enrichment (the pipeline kicks briefs)."""
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._again = False
        self.last_run: dict | None = None
        self.backfill: timedelta | None = None
        """The summarizer's backfill window: older never-enriched stories are skipped."""
        self.sleep: Callable[[float], None] = time.sleep
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.last_pass_at: datetime | None = None
        """When the worker last finished a pass over the pending queue."""
        self.last_sweep_at: datetime | None = None
        """When the scheduled ``enrich`` job last ran (``sweep``)."""
        self.last_watchdog_at: datetime | None = None
        self.watchdog_restarts = 0

    # --- eligibility ------------------------------------------------------------------

    def enabled_profiles(self, session: Session) -> list[ProfileRecord]:
        out = []
        for record in session.scalars(select(ProfileRecord)):
            default = self.profile_defaults.get(record.slug, self.config.enabled)
            on = record.enrichment_enabled if record.enrichment_enabled is not None else default
            if on:
                out.append(record)
        return out

    def profile_enabled(self, record: ProfileRecord) -> bool:
        if record.enrichment_enabled is not None:
            return record.enrichment_enabled
        return self.profile_defaults.get(record.slug, self.config.enabled)

    @property
    def any_enabled(self) -> bool:
        with self.session_factory() as session:
            return bool(self.enabled_profiles(session))

    def eligible(self, session: Session, story_ids: list[int] | None = None) -> list[int]:
        """Stories to enrich now: clustered, recent, under-covered, followed by a profile with
        enrichment on, and not enriched within ``reenrich_hours``."""
        profiles = self.enabled_profiles(session)
        if not profiles:
            return []
        now = self.now()
        cfg = self.config
        followed = select(profile_sources.c.source_id).where(
            profile_sources.c.profile_id.in_([p.id for p in profiles])
        )
        member = (
            select(Article.id)
            .where(
                Article.story_id == Story.id,
                Article.origin.not_in(NON_FEED),
                Article.source_id.in_(followed),
            )
            .exists()
        )
        stmt = select(Story).where(
            Story.state.not_in([s.value for s in PRE_CLUSTER] + [StoryState.MERGED.value]),
            Story.last_updated_at >= now - timedelta(hours=cfg.max_age_hours),
            member,
        )
        if story_ids is not None:
            stmt = stmt.where(Story.id.in_(story_ids))
        out = []
        for story in session.scalars(
            stmt.order_by(Story.priority.desc(), Story.last_updated_at.desc())
        ):
            if not self._due(story, now):
                continue
            if (
                not story.enrich_passes
                and story.enrich_status is None
                and self.backfill is not None
                and story.last_updated_at < now - self.backfill
            ):
                continue  # backlog catch-up: too old to hold a brief for
            if story.published_version is not None and (
                not cfg.resummarize_on_enrichment or story.enrich_regens >= 1
            ):
                continue  # late sources would re-generate a published story more than once
            publishers = self.publishers(session, story.id)
            if publishers >= cfg.target_sources and not (
                cfg.depth.enabled
                and publishers < cfg.depth.target_sources
                and self._deep_admitted(session, story)
            ):
                continue
            out.append(story.id)
        return out

    def _due(self, story: Story, now: datetime) -> bool:
        if story.enrich_status is None:
            return True
        if story.enrich_status == "pending":
            return (story.enrich_started_at or now) < now - STALE_PENDING
        if story.enrich_status == "retry":
            return False  # run_pending picks it up at enrich_next_attempt_at
        second = self.config.second_pass_hours
        if (
            second
            and story.enrich_status == "done"
            and story.enrich_passes == 1
            and story.enriched_at is not None
            and story.enriched_at <= now - timedelta(hours=second)
        ):
            return True  # one more search for stories still under target_sources
        if not self.config.reenrich_hours:
            return False
        last = story.enriched_at or story.enrich_started_at
        return last is None or last < now - timedelta(hours=self.config.reenrich_hours)

    def skip_reason(self, headlines: list[str], anchor_url: str | None = None) -> str | None:
        """``skipped:pattern`` / ``skipped:no_signal`` / ``skipped:url_pattern`` for
        stories other outlets are unlikely to cover (Show HN posts, opinion columns,
        features, vague headlines); None to search."""
        if not headlines:
            return None
        cfg = self.config
        main = headlines[0].strip()
        if any(re.search(p, main, re.IGNORECASE) for p in cfg.skip_title_patterns):
            return f"{SKIPPED}pattern"
        if anchor_url and any(
            p in urlsplit(anchor_url).path.lower() for p in cfg.skip_url_patterns
        ):
            return f"{SKIPPED}url_pattern"
        if cfg.min_signal_tokens and max(len(signal_tokens(h)) for h in headlines) < (
            cfg.min_signal_tokens
        ):
            return f"{SKIPPED}no_signal"
        return None

    def _skip_unsearchable(self, session: Session, ids: list[int], now: datetime) -> list[int]:
        """Mark stories not worth a search ``done`` (with the skip reason) right away, so
        their briefs never wait; the rest are returned."""
        keep = []
        for sid in ids:
            found = self.context(session, sid)
            reason = self.skip_reason(found[0].headlines, found[1].url) if found else None
            if reason is None:
                keep.append(sid)
                continue
            story = session.get(Story, sid)
            story.enrich_status, story.enrich_reason = "done", reason
            story.enriched_at, story.enrich_next_attempt_at = now, None
            story.enrich_passes = (story.enrich_passes or 0) + 1
        return keep

    def _feed_publishers(self, session: Session, story_id: int) -> int:
        """Distinct publishers among feed members only (canonical domains; enrichment
        members do not count — ``deep_min_publishers`` is about real coverage)."""
        urls = session.scalars(
            select(Article.url).where(Article.story_id == story_id, Article.origin.not_in(NON_FEED))
        )
        return len({canonical_domain(u, self.config.publisher_aliases) for u in urls if u})

    def _deep_admitted(self, session: Session, story: Story) -> bool:
        """The deep tier (``enrichment.depth``): stories with ``deep_min_publishers``
        feed publishers, a priority above ``deep_min_priority``, or an alert that
        already fired on this story."""
        cfg = self.config.depth
        if not cfg.enabled:
            return False
        if self._feed_publishers(session, story.id) >= cfg.deep_min_publishers:
            return True
        if cfg.deep_min_priority is not None and (story.priority or 0) > cfg.deep_min_priority:
            return True
        return bool(
            cfg.deep_on_alert
            and session.scalar(
                select(func.count())
                .select_from(AlertFiring)
                .where(AlertFiring.lineage_id == story.id)
            )
        )

    def _deep_budget_left(self) -> float:
        """Requests the deep tier may still send today (inf when unbounded)."""
        budget = self.config.depth.daily_request_budget
        if budget is None:
            return float("inf")
        with self.session_factory() as session:
            from app.ingest import usage_periods

            day, _ = usage_periods(self.now())
            row = session.get(SourceUsage, (DEEP_USAGE, day))
            return budget - (row.requests if row else 0)

    def _record_deep_requests(self, count: int) -> None:
        """Add ``count`` requests to today's deep-tier usage (``enrich:deep``)."""
        if count <= 0:
            return
        from app.ingest import usage_periods

        with self.session_factory() as session:
            day, _ = usage_periods(self.now())
            row = session.get(SourceUsage, (DEEP_USAGE, day))
            if row is None:
                row = SourceUsage(source_id=DEEP_USAGE, period=day, requests=0)
                session.add(row)
            row.requests = (row.requests or 0) + count
            session.commit()

    def publishers(self, session: Session, story_id: int) -> int:
        """Distinct publishers on the story: registrable domains, with editions of the
        same publisher (``bbc.com`` / ``bbc.co.uk``) counted once via
        ``publisher_aliases``."""
        aliases = self.config.publisher_aliases
        urls = session.scalars(
            select(Article.url).where(Article.story_id == story_id, Article.origin != CONTEXT)
        )
        return len({canonical_domain(u, aliases) for u in urls if u})

    # --- scheduling -------------------------------------------------------------------

    def schedule(self, story_ids: list[int] | None = None, *, force: bool = False) -> int:
        """Mark eligible stories ``pending`` (their briefs wait) and start the background
        run. Only as many as the providers can serve within ``wait_seconds`` are admitted
        (newest first); the rest are admitted on a later sweep. ``force``: the admin
        "Enrich now" (ignores the re-enrich interval and the admission limit).

        The worker is started whenever anything is waiting — newly admitted, retries
        due, or stories still ``pending`` from an earlier sweep (after a restart they
        fill ``capacity()``, so nothing new is admitted, yet they still need a worker)."""
        logger.info(
            "enrichment schedule: story_ids=%s force=%s worker_running=%s",
            story_ids if story_ids is None or len(story_ids) <= 10 else f"{len(story_ids)} ids",
            force,
            self.running,
        )
        now = self.now()
        with self.session_factory() as session:
            if force and story_ids:
                ids = [
                    s
                    for s in story_ids
                    if (story := session.get(Story, s)) is not None
                    and story.state != StoryState.MERGED
                ]
            else:
                ids = self._skip_unsearchable(session, self.eligible(session, story_ids), now)
                ids = ids[: self.capacity(session)]
            wait = timedelta(seconds=self.config.wait_seconds)
            for sid in ids:
                story = session.get(Story, sid)
                story.enrich_status, story.enrich_started_at = "pending", now
                story.enrich_reason, story.enrich_next_attempt_at = None, None
                if story.enrich_wait_until is None:  # only the first pass holds the brief
                    first = not story.enrich_passes and story.published_version is None
                    story.enrich_wait_until = now + wait if first else now
            session.commit()
            due = self.retries_due(session)
            waiting = self.pending_count(session)
        logger.info(
            "enrichment schedule: admitted=%d pending=%d retries_due=%d",
            len(ids),
            waiting,
            due,
        )
        if waiting or due:
            self.start()
        return len(ids)

    @staticmethod
    def pending_count(session: Session) -> int:
        return (
            session.scalar(
                select(func.count()).select_from(Story).where(Story.enrich_status == "pending")
            )
            or 0
        )

    def has_work(self) -> bool:
        with self.session_factory() as session:
            return bool(self.pending_count(session) or self.retries_due(session))

    def sweep(self) -> int:
        """The scheduled ``enrich`` job: admit newly eligible stories and make sure the
        worker runs for anything waiting — independent of the pipeline run."""
        self.last_sweep_at = self.now()
        try:
            return self.schedule()
        except Exception as exc:
            self._record_error(exc)
            logger.exception("scheduled enrichment sweep failed")
            return 0

    def watchdog(self) -> bool:
        """Restart the worker thread when it is not alive while work is waiting (the
        thread exits on its own once the queue is empty, which is not a failure)."""
        self.last_watchdog_at = self.now()
        if self.running:
            return False
        try:
            work = self.has_work()
        except Exception as exc:
            self._record_error(exc)
            logger.exception("enrichment watchdog could not read the queue")
            work = True
        if not work:
            logger.debug("enrichment watchdog: worker idle, nothing waiting")
            return False
        logger.warning("enrichment watchdog: worker thread not alive with work waiting; restarting")
        self.watchdog_restarts += 1
        return self.start()

    def _record_error(self, exc: BaseException) -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"[:500]
        self.last_error_at = self.now()

    def status(self) -> dict:
        """The admin status endpoint / Workflow panel."""
        from app.models import StageEvent

        with self.session_factory() as session:
            pending = self.pending_count(session)
            due = self.retries_due(session)
            last_event = session.scalar(
                select(func.max(StageEvent.started_at)).where(StageEvent.stage == "enrich")
            )
        primary = self.config.providers[0].name if self.config.providers else None
        health = self.search.health.get(primary) if primary else None
        return {
            "thread_alive": self.running,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_stage_event_at": last_event,
            "last_pass_at": self.last_pass_at,
            "pending": pending,
            "retries_due": due,
            "provider": primary,
            "provider_requests_since_start": health.requests if health else 0,
            "tinyfish_requests_since_start": (
                self.search.health["tinyfish"].requests if "tinyfish" in self.search.health else 0
            ),
            "last_sweep_at": self.last_sweep_at,
            "last_watchdog_at": self.last_watchdog_at,
            "watchdog_restarts": self.watchdog_restarts,
        }

    def capacity(self, session: Session) -> int:
        """Stories the first provider not cooling down can serve within ``wait_seconds``
        (its ``min_interval_seconds`` x ``max_queries_per_story``, minus a cooldown if all
        are cooling), less those already waiting."""
        cfg = self.config
        if not cfg.providers:
            return 0
        cooling = {p.name: self.search.cooldown_remaining(p.name) for p in cfg.providers}
        primary = next((p for p in cfg.providers if cooling[p.name] <= 0), cfg.providers[0])
        per_story = (primary.min_interval_seconds or 0.0) * cfg.max_queries_per_story
        if per_story <= 0:
            return 10**6
        available = cfg.wait_seconds - min(cooling.values())
        queued = session.scalar(
            select(func.count()).select_from(Story).where(Story.enrich_status == "pending")
        )
        return max(0, int(available // per_story) - (queued or 0))

    def retries_due(self, session: Session) -> int:
        return (
            session.scalar(
                select(func.count()).select_from(Story).where(self._retry_due(self.now()))
            )
            or 0
        )

    @staticmethod
    def _retry_due(now: datetime):
        return and_(
            Story.enrich_status.is_not(None),
            Story.enrich_status == "retry",
            or_(Story.enrich_next_attempt_at.is_(None), Story.enrich_next_attempt_at <= now),
        )

    def requeue_provider_errors(self) -> int:
        """One-time admin action: stories whose enrichment ended with provider errors
        (``failed``, waiting to retry, or ``done`` by the old code with nothing found and no
        candidate recorded) are retried now, at the providers' pace. Briefs do not wait."""
        now = self.now()
        cutoff = now - timedelta(hours=self.config.max_age_hours)
        has_candidates = (
            select(EnrichmentCandidate.id).where(EnrichmentCandidate.story_id == Story.id).exists()
        )
        with self.session_factory() as session:
            stories = session.scalars(
                select(Story).where(
                    Story.enrich_status.is_not(None),
                    Story.state != StoryState.MERGED.value,
                    Story.last_updated_at >= cutoff,
                    or_(
                        Story.enrich_status.in_(["failed", "retry"]),
                        and_(
                            Story.enrich_status == "done",
                            func.coalesce(Story.enrich_added, 0) == 0,
                            ~has_candidates,
                        ),
                    ),
                )
            ).all()
            for story in stories:
                story.enrich_status, story.enrich_next_attempt_at = "retry", now
                story.enrich_attempts = 0
                story.enrich_reason = f"{UNAVAILABLE}: re-enrich requested"
                if story.enrich_wait_until is None:
                    story.enrich_wait_until = now
            session.commit()
        if stories:
            self.start()
        return len(stories)

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.info("enrichment start: worker already running; another pass queued")
                self._again = True
                return False
            logger.info("enrichment start: starting the worker thread")
            self._thread = threading.Thread(
                target=self.run_blocking, name="enrichment", daemon=True
            )
            self._thread.start()
            return True

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_blocking(self) -> list[StoryEnrichment]:
        """The worker thread: passes over the pending queue until it is empty. An
        unexpected error never ends the thread — it is logged, then retried."""
        results: list[StoryEnrichment] = []
        while True:
            try:
                results += asyncio.run(self.run_pending())
            except Exception as exc:
                self._record_error(exc)
                logger.exception(
                    "enrichment worker loop failed; retrying in %.0f s", LOOP_RETRY_SECONDS
                )
                self.sleep(LOOP_RETRY_SECONDS)
                continue
            self.last_pass_at = self.now()
            with self._lock:
                if not self._again:
                    return results
                self._again = False

    def pending(self, session: Session) -> list[int]:
        """Admitted stories, then retries that came due (newest first)."""
        stmt = (
            select(Story.id)
            .where(
                or_(
                    and_(Story.enrich_status.is_not(None), Story.enrich_status == "pending"),
                    self._retry_due(self.now()),
                )
            )
            .order_by(Story.priority.desc(), Story.last_updated_at.desc())
            .limit(BATCH)
        )
        return list(session.scalars(stmt))

    async def run_pending(self) -> list[StoryEnrichment]:
        results: list[StoryEnrichment] = []
        while True:
            with self.session_factory() as session:
                ids = self.pending(session)
            if not ids:
                break
            semaphore = asyncio.Semaphore(self.config.concurrency)

            async def one(sid: int, semaphore=semaphore) -> StoryEnrichment:
                async with semaphore:
                    return await self.enrich_story(sid)

            batch = await asyncio.gather(*(one(s) for s in ids))
            results += batch
            self._notify([r.story_id for r in batch])
        if results:
            self.last_run = summarize_run(results)
            try:
                from app.runs import record_run

                record_run(self.session_factory, "enrich", self.last_run)
            except Exception:
                logger.exception("could not record the enrichment run")
        return results

    def _notify(self, ids: list[int]) -> None:
        if self.on_done is not None and ids:
            try:
                self.on_done(ids)
            except Exception:
                logger.exception("enrichment completion hook failed")

    # --- one story --------------------------------------------------------------------

    def context(
        self, session: Session, story_id: int, *, anchor_only: bool = False
    ) -> tuple[StoryContext, Article] | None:
        story = session.get(Story, story_id)
        if story is None or story.state == StoryState.MERGED:
            return None
        members = list(
            session.scalars(
                select(Article)
                .where(Article.story_id == story_id, Article.origin != CONTEXT)
                .order_by(Article.published_at, Article.id)
            )
        )
        feed = [m for m in members if m.origin != ENRICHMENT]
        if not feed:
            return None
        anchor = next((m for m in feed if m.id == story.representative_article_id), feed[0])
        if anchor_only:
            # The backtest's pretend single-sourced story: every member but the anchor
            # is hidden from queries, domain checks and similarity.
            feed = [anchor]
            members = [anchor]
        headlines = [anchor.title or ""] + [m.title or "" for m in feed if m is not anchor]
        return (
            StoryContext(
                title=story.title or anchor.title or "",
                headlines=[h for h in headlines if h],
                texts=[
                    (m.text if m.text_status == "ok" else None)
                    or ("" if m.text_status == "none" else m.summary_raw or "")
                    for m in members
                ],
                domains={
                    canonical_domain(m.url, self.config.publisher_aliases) for m in members if m.url
                },
                canonical_urls={m.canonical_url for m in members},
                first_at=min(m.published_at for m in members),
                last_at=max(m.published_at for m in members),
                language=self.config.language,
            ),
            anchor,
        )

    async def enrich_story(
        self,
        story_id: int,
        *,
        dry_run: bool = False,
        anchor_only: bool = False,
        force_deep: bool = False,
    ) -> StoryEnrichment:
        """``anchor_only`` (evaluate ``--backtest``): pretend the story only has its
        anchor member, so a multi-publisher story exercises the whole pipeline.
        ``force_deep`` (evaluate ``--deep``): run the deep tier even when the story
        would not be admitted on its own."""
        result = StoryEnrichment(story_id=story_id)
        try:
            from app import telemetry

            with (
                telemetry.story_context(story_id),
                telemetry.stage("enrich", story_id=story_id) as ev,
            ):
                await self._enrich(result, dry_run, anchor_only=anchor_only, force_deep=force_deep)
                ev.detail.update(
                    status=result.status,
                    reason=result.reason,
                    tier=result.tier,
                    added=result.added,
                    dry_run=dry_run,
                )
        except Exception as exc:
            logger.exception("enrichment of story %s failed", story_id)
            result.status, result.reason = "failed", f"{type(exc).__name__}: {exc}"[:500]
            if not dry_run:
                self._finish(result)
        return result

    async def _enrich(
        self,
        result: StoryEnrichment,
        dry_run: bool,
        *,
        anchor_only: bool = False,
        force_deep: bool = False,
    ) -> None:
        cfg = self.config
        with self.session_factory() as session:
            found = self.context(session, result.story_id, anchor_only=anchor_only)
            result.publishers_before = (
                len(found[0].domains)
                if anchor_only and found is not None
                else self.publishers(session, result.story_id)
            )
        if found is None:
            result.status, result.reason = "failed", "story gone or has no feed members"
            if not dry_run:
                self._finish(result)
            return
        story, anchor = found
        skip = self.skip_reason(story.headlines, anchor.url)
        if skip:
            result.answered, result.reason = True, skip
            if not dry_run:
                self._finish(result)
            return
        with self.session_factory() as session:
            result.tier = self._tier(session, result.story_id, force_deep=force_deep)
        depth = cfg.depth
        deep = result.tier == "deep" and depth.enabled
        headlines = [strip_boilerplate(h, cfg.strip_title_prefixes) for h in story.headlines]
        cap_added = depth.max_added_sources if deep else cfg.max_added_sources
        target = depth.target_sources if deep else cfg.target_sources
        need = max(0, min(cap_added, target - result.publishers_before))
        if dry_run:
            need = need or cap_added
        base, result.query_mode = await build_queries(
            headlines,
            mode=cfg.query_mode.value,
            role=self.llm_role,
            transport=self.llm_transport,
        )
        llm = base if result.query_mode == "llm" else None
        plans = {
            p.name: (
                deep_queries(
                    p.name,
                    headlines,
                    llm=llm,
                    max_queries=depth.max_queries_per_story,
                    strip_prefixes=cfg.strip_title_prefixes,
                )
                if deep and getattr(_provider_cls(p.name), "supports_pages", False)
                else provider_queries(
                    p.name,
                    headlines,
                    llm=llm,
                    max_queries=cfg.max_queries_per_story,
                    strip_prefixes=cfg.strip_title_prefixes,
                )
            )
            for p in cfg.providers
        }
        result.queries = list(dict.fromkeys(q for qs in plans.values() for q in qs))
        if not result.queries or need == 0:
            result.answered = True
            result.reason = "no query" if not result.queries else "enough sources"
            if not dry_run:
                self._finish(result)
            return
        since = story.first_at - timedelta(hours=cfg.max_time_gap_hours)
        seen: set[str] = set()
        accepted_texts: list[str] = []
        for provider in cfg.providers:
            if len(result.accepted) >= need:
                break
            exclude = self._excluded(provider.name, story)
            if deep and getattr(_provider_cls(provider.name), "supports_pages", False):
                await self._deep_pass(
                    provider,
                    plans[provider.name],
                    story,
                    result,
                    since,
                    need,
                    dry_run,
                    seen,
                    accepted_texts,
                    exclude,
                )
                continue
            fresh: list[Candidate] = []
            for query in plans[provider.name]:
                outcome = await self._search(
                    provider, query, since, dry_run, result, exclude=exclude
                )
                if outcome.kind == "query_rejected":
                    rebuilt = drop_short_terms(provider.name, query)
                    if rebuilt:  # rebuilt once without the short keywords
                        query = rebuilt
                        outcome = await self._search(
                            provider, query, since, dry_run, result, exclude=exclude
                        )
                    if outcome.kind == "query_rejected":
                        result.searches[-1]["skipped"] = QUERY_TOO_SHORT
                        continue  # the looser query may be valid; never a cooldown
                if not outcome.ok:
                    break
                result.answered = True
                for hit in outcome.hits:
                    key = canonicalize_url(hit.url) if hit.url.startswith("http") else hit.url
                    if key in seen:
                        continue
                    seen.add(key)
                    fresh.append(Candidate(hit=hit, provider=provider.name, query=query))
                # a looser query only when this one left too few usable candidates
                if self._prescreen(fresh, story) >= need - len(result.accepted):
                    break
            await self._judge(fresh, story, need - len(result.accepted), accepted_texts)
            result.candidates += fresh
        failures = [s for s in result.searches if s.get("error")]
        if (
            not result.answered
            and failures
            and all(s.get("kind") == "query_rejected" for s in failures)
        ):
            result.answered, result.reason = True, QUERY_TOO_SHORT  # retrying cannot help
        if not result.answered:
            last = failures[-1]["error"] if failures else "no provider answered"
            result.status, result.reason = "retry", f"{UNAVAILABLE}: {last}"[:500]
        else:
            result.degraded = self._primary_down(result)
            if result.degraded:
                primary = cfg.providers[0].name
                result.reason = f"{DEGRADED}: {primary} unavailable: {result.degraded}"[:500]
                if dry_run and len(result.accepted) < need:
                    result.status = "retry"
        if not dry_run:
            self._finish(result, anchor_id=anchor.id)

    def _tier(self, session: Session, story_id: int, *, force_deep: bool = False) -> str:
        """``deep`` for stories the depth config admits (feed publishers / priority /
        fired alert) — or evaluate's ``--deep``; ``standard`` otherwise."""
        if force_deep:
            return "deep"
        if not self.config.depth.enabled:
            return "standard"
        story = session.get(Story, story_id)
        if story is None or not self._deep_admitted(session, story):
            return "standard"
        return "deep"

    async def _deep_pass(
        self,
        provider,
        queries: list[str],
        story: StoryContext,
        result: StoryEnrichment,
        since: datetime,
        need: int,
        dry_run: bool,
        seen: set[str],
        accepted_texts: list,
        exclude: list[str],
    ) -> None:
        """Deep-tier search loop (``enrichment.depth``): each query variant on pages
        0..pages-1 (a page with no usable candidates stops it early, as does reaching
        ``need``), then the strongest query once per configured ``location``, then one
        ``include_domains`` query per missing ``ensure_groups`` group. Every request
        counts against ``daily_request_budget``; when it runs out the story keeps what
        it has (``deep_budget`` in ``searches``)."""
        cfg = self.config.depth
        seen_domains: set[str] = set()

        def coverage() -> int:
            return len(result.accepted) + len(result.untexted)

        async def collect(query: str, **kw) -> int | None:
            """One search + prescreen + judge on a fresh batch: its usable-candidate
            count, or None when the provider failed (stops the whole deep pass)."""
            if not dry_run and self._deep_budget_left() <= 0:
                result.searches.append(
                    {"provider": provider.name, "query": query, "stopped": "deep_budget"}
                )
                return 0  # budget is not a provider failure — stop quietly
            outcome = await self._search(
                provider, query, since, dry_run, result, exclude=exclude, **kw
            )
            result.deep_requests += outcome.requests
            if not dry_run:
                self._record_deep_requests(outcome.requests)
            if outcome.kind == "query_rejected":
                rebuilt = drop_short_terms(provider.name, query)
                if rebuilt:
                    outcome = await self._search(
                        provider, rebuilt, since, dry_run, result, exclude=exclude, **kw
                    )
                    result.deep_requests += outcome.requests
                    if not dry_run:
                        self._record_deep_requests(outcome.requests)
                if outcome.kind == "query_rejected":
                    result.searches[-1]["skipped"] = QUERY_TOO_SHORT
                    return 0
            if not outcome.ok:
                return None
            result.answered = True
            fresh = []
            for hit in outcome.hits:
                key = canonicalize_url(hit.url) if hit.url.startswith("http") else hit.url
                domain = canonical_domain(hit.url, self.config.publisher_aliases)
                if key in seen or domain in seen_domains or domain in story.domains:
                    continue
                seen.add(key)
                seen_domains.add(domain)  # one candidate per publisher across all calls
                fresh.append(Candidate(hit=hit, provider=provider.name, query=query))
            usable = self._prescreen(fresh, story)
            if usable:
                await self._judge(fresh, story, need - coverage(), accepted_texts)
            result.candidates += fresh
            return usable

        usable_by_query: dict[str, int] = {}
        for query in queries:
            for page in range(cfg.pages):
                if coverage() >= need:
                    break
                got = await collect(query, page=page)
                if got is None:
                    return
                usable_by_query[query] = usable_by_query.get(query, 0) + got
                if not got:  # a page with no usable candidates ends this variant
                    break
            if coverage() >= need:
                break
        strong = max(
            usable_by_query, key=usable_by_query.get, default=queries[0] if queries else ""
        )
        if strong and coverage() < need:
            for location in cfg.locations:
                got = await collect(strong, location=location)
                if got is None:
                    return
                if coverage() >= need:
                    break
        if cfg.ensure_groups and cfg.max_group_queries and coverage() < need:
            from app.outlets import outlets

            registry = outlets()
            covered = registry.groups_covered(
                [
                    registry.for_url(c.hit.url)
                    for c in result.candidates
                    if c.decision != "rejected"
                ],
                cfg.ensure_groups,
            )
            domains_by_group = registry.group_domains(cfg.ensure_groups)
            for group in cfg.ensure_groups:
                if group in covered or not domains_by_group.get(group):
                    continue
                if (
                    coverage() >= need
                    or sum(1 for s in result.searches if s.get("group")) >= cfg.max_group_queries
                ):
                    break
                got = await collect(strong or queries[0], include=domains_by_group[group])
                if got is None:
                    return
                if result.searches:
                    result.searches[-1]["group"] = group

    def _primary_down(self, result: StoryEnrichment) -> str | None:
        """The first provider's error when it never answered for this story (auth, rate
        limit, cooldown, timeout, budget) while a fallback did; None otherwise."""
        providers = self.config.providers
        if len(providers) < 2:
            return None
        mine = [s for s in result.searches if s["provider"] == providers[0].name]
        if not mine or any(not s.get("error") for s in mine):
            return None
        down = [s for s in mine if s.get("kind") != "query_rejected"]
        return down[-1]["error"] if down else None

    def _excluded(self, provider: str, story: StoryContext) -> list[str]:
        """The story's own publisher domains plus the social networks, when the provider
        can exclude them server-side (TinyFish ``exclude_domains``); same-domain and
        social hits are always rejected, so keeping them out of the results frees slots
        for real coverage. Fediverse hosts on arbitrary domains (``mastodon.`` /
        ``mstdn.`` substrings) cannot be enumerated and stay post-filtered."""
        cls = _provider_cls(provider)
        if cls is None or not cls.supports_exclude:
            return []
        return sorted({*story.domains, *SOCIAL, *self.config.social_domains})

    async def _search(
        self,
        provider,
        query: str,
        since: datetime,
        dry_run: bool,
        result,
        exclude: list[str] | None = None,
        page: int = 0,
        location: str | None = None,
        include: Sequence[str] = (),
    ):
        cfg = self.config
        outcome = await self.search.search(
            provider,
            query,
            since=since,
            language=cfg.language,
            cache_hours=cfg.cache_hours,
            empty_cache_minutes=cfg.empty_cache_minutes,
            store=not dry_run,
            exclude=exclude or (),
            page=page,
            location=location,
            include=include,
        )
        result.searches.append(
            {
                "provider": provider.name,
                "query": query,
                "page": page,
                "location": location,
                "include": list(include) or None,
                "hits": len(outcome.hits),
                "cached": outcome.cached,
                "error": outcome.error,
                "kind": outcome.kind,
                "requests": outcome.requests,
                "skipped": outcome.skipped,
                "cooldown_seconds": round(outcome.cooldown_seconds, 1),
                "fields": outcome.fields,
                "raw": outcome.raw[:600],
            }
        )
        return outcome

    def _denied(self, session: Session) -> list[str]:
        """The effective deny list: ``enrichment.deny_domains``, confirmed content farms
        (``enrichment.quality.deny_domains``) plus every domain the admin blocked
        (the database wins — a YAML entry cannot unblock one)."""
        return [
            *self.config.deny_domains,
            *self.config.quality.deny_domains,
            *session.scalars(select(BlockedDomain.domain)),
            *session.scalars(select(BlockedDomain.canonical)),
        ]

    def _flag(self, c: Candidate) -> str:
        """The source-quality flag for an accepted/link-only candidate, ``""`` when its
        domain looks fine (``enrichment.quality``)."""
        q = self.config.quality
        return flag_reason(c.hit.url, tlds=q.suspicious_tlds, patterns=q.suspicious_patterns) or ""

    def _prescreen(self, candidates: list[Candidate], story: StoryContext) -> int:
        """The cheap rejections, before any fetch: same domain, deny / social / aggregator
        lists, an already ingested URL, the time gap, the language. Returns how many
        candidates are still usable (drives whether the looser query runs)."""
        cfg = self.config
        gap = timedelta(hours=cfg.max_time_gap_hours)
        with self.session_factory() as session:
            deny = self._denied(session)
            for c in candidates:
                if c.reason:
                    continue
                c.reason = (
                    pre_filter(
                        c.hit,
                        story,
                        deny=deny,
                        max_gap=gap,
                        language=cfg.language,
                        aliases=cfg.publisher_aliases,
                        social=cfg.social_domains,
                        aggregators=cfg.aggregator_domains,
                    )
                    or ""
                )
                if not c.reason and session.scalar(
                    select(func.count())
                    .select_from(Article)
                    .where(Article.canonical_url == canonicalize_url(c.hit.url))
                ):
                    c.reason = "already_ingested"
        return sum(1 for c in candidates if not c.reason)

    async def _judge(
        self, candidates: list[Candidate], story: StoryContext, need: int, accepted_texts: list
    ) -> None:
        cfg = self.config
        keep: list[Candidate] = []
        for c in candidates:
            if c.reason:
                continue  # rejected by the cheap prescreen
            if c.hit.snippet.strip():
                reason, c.similarity = snippet_check(c.hit, story, cfg.min_snippet_similarity)
                c.shared_entities = shared_entities(f"{c.hit.title} {c.hit.snippet}", story)
            else:
                reason, c.title_similarity, c.shared_entities = title_check(
                    c.hit, story, cfg.min_title_similarity
                )
            if reason:
                c.reason = reason
                continue
            keep.append(c)
        keep.sort(key=lambda c: -c.score)
        for i, c in enumerate(keep):
            if need <= 0 or i >= cfg.max_candidates:
                c.reason = "not_needed" if need <= 0 else "over_candidate_limit"
                continue
            domain = canonical_domain(c.hit.url, cfg.publisher_aliases)
            if domain in story.domains:
                c.reason = "same_domain"
                continue
            c.page = await self.fetcher.fetch(c.hit.url)
            if c.page.status != "ok":
                c.fetch_detail = f"{c.page.status}: {c.page.reason or ''}".strip(": ")
                reason = {
                    "blocked": "blocked_url",
                    "paywalled": "paywalled",
                    "skipped": "robots",
                }.get(c.page.status, "fetch_failed")
                if c.page.status in ("failed", "blocked", "paywalled"):  # never robots
                    if (
                        cfg.link_only
                        and (c.similarity or 0.0) >= cfg.min_snippet_strong
                        and (c.shared_entities or 0) >= cfg.min_shared_entities
                    ):
                        c.decision, c.reason = "link_only", reason
                        c.flag = self._flag(c)
                        continue
                    if self._headline_only(c, story):
                        c.decision, c.reason = "headline_only", reason
                        c.flag = self._flag(c)
                        continue
                c.reason = reason
                continue
            verdict = text_check(
                c.hit.title,
                c.page.text,
                story,
                threshold=cfg.min_text_similarity,
                min_entities=cfg.min_shared_entities,
                duplicate_overlap=cfg.duplicate_overlap,
                accepted_texts=accepted_texts,
                snippet_similarity=c.similarity,
                min_snippet_strong=cfg.min_snippet_strong,
                min_text_floor=cfg.min_text_floor,
                extra_rules=cfg.extra_rules,
            )
            c.text_similarity, c.shared_entities, c.rule = (
                verdict.score,
                verdict.entities,
                verdict.rule,
            )
            if verdict.reason:
                c.reason = verdict.reason
                continue
            c.decision = "accepted"
            c.reason = f"accepted_rule_{c.rule}" if c.rule else "accepted"
            c.flag = self._flag(c)
            accepted_texts.append(c.page.text)
            story.domains.add(domain)
            need -= 1

    def _headline_only(self, c: Candidate, story: StoryContext) -> bool:
        """Headline-only member (``enrichment.headline_only_*``): the hit's title is
        clearly the same event — inside the time window (the pre-filter's), at least
        ``headline_only_title_similarity`` similar to the headlines and sharing
        ``headline_only_min_entities`` of them — but its page cannot be fetched. Kept
        for the coverage panel only: never summarized or cited."""
        cfg = self.config
        c.title_similarity = (
            c.title_similarity
            if c.title_similarity is not None
            else round(similarity(c.hit.title, " ".join(story.headlines)), 4)
        )
        entities = shared_entities(c.hit.title, story)
        if c.shared_entities is None or entities > c.shared_entities:
            c.shared_entities = entities
        return (
            c.title_similarity >= cfg.headline_only_title_similarity
            and entities >= cfg.headline_only_min_entities
        )

    def _finish(self, result: StoryEnrichment, *, anchor_id: int | None = None) -> None:
        now = self.now()
        run_id = uuid.uuid4().hex
        with self.session_factory() as session:
            story = session.get(Story, result.story_id)
            if story is None or story.state == StoryState.MERGED:
                result.status, result.reason = "failed", "story gone"
                return
            if result.status == "retry":
                self._schedule_retry(story, result, now)
                session.commit()
                return
            accepted = result.accepted
            untexted = result.untexted
            late = story.published_version is not None
            if (
                accepted
                and late
                and (not self.config.resummarize_on_enrichment or story.enrich_regens >= 1)
            ):
                for c in accepted:
                    c.decision, c.reason = "rejected", "late"
                accepted = []
            regenerate = late and accepted and self._regen_allowed(session, now)
            for c in accepted + untexted:
                c.article_id = self._add_member(session, story, c, anchor_id, now)
            result.added = sum(1 for c in accepted if c.article_id is not None)
            untexted_added = sum(1 for c in untexted if c.article_id is not None)
            for c in result.candidates:
                session.add(
                    EnrichmentCandidate(
                        story_id=story.id,
                        run_id=run_id,
                        provider=c.provider,
                        query=c.query,
                        url=c.hit.url[:2048],
                        domain=c.hit.domain[:300],
                        title=c.hit.title,
                        decision=c.decision,
                        reason=c.reason[:40],
                        flagged=c.flag[:40],
                        similarity=c.similarity,
                        title_similarity=c.title_similarity,
                        shared_entities=c.shared_entities,
                        text_similarity=c.text_similarity,
                        article_id=c.article_id,
                        created_at=now,
                    )
                )
            if result.degraded and self._degraded_retry(session, story, now):
                self._schedule_degraded(story, result, now)
            else:
                if result.status != "failed" and story.enrich_attempts and not result.reason:
                    result.reason = f"{RECOVERED} after {story.enrich_attempts} failed attempt(s)"
                result.status = "failed" if result.status == "failed" else "done"
                story.enrich_status = result.status
                story.enrich_depth = result.tier
                story.enriched_at = now
                story.enrich_passes = (story.enrich_passes or 0) + 1
                story.enrich_attempts, story.enrich_next_attempt_at = 0, None
                if result.added and late and not regenerate:
                    result.reason = "late_regen_deferred"  # kept for the next regeneration
                story.enrich_reason = result.reason
            if result.added or untexted_added:
                from app.cluster.service import refresh_stories

                story.enrich_added = (story.enrich_added or 0) + result.added
                if late and regenerate:
                    story.enrich_regens = (story.enrich_regens or 0) + 1
                    story.enrich_regen_at = now
                if story.state not in PRE_CLUSTER and (not late or regenerate):
                    story.state = StoryState.CLUSTERED.value  # regenerate; published stays
                session.flush()
                refresh_stories(session, {story.id})
            session.commit()

    def _schedule_retry(self, story: Story, result: StoryEnrichment, now: datetime) -> None:
        """No provider answered: not done (``enriched_at`` stays unset); retry after a
        backoff, or when the first provider's cooldown ends if that was the only reason."""
        tried = [s for s in result.searches if s.get("error")]
        cooling = [c for s in tried if (c := s.get("cooldown_seconds") or 0.0) > 0]
        wait_out = timedelta(seconds=min(cooling) + 1) if cooling else timedelta(0)
        if tried and all(s.get("skipped") == "cooldown" for s in tried):
            delay = wait_out or RETRY_FIRST  # waiting for a cooldown is not a failed attempt
        else:
            story.enrich_attempts = (story.enrich_attempts or 0) + 1
            delay = max(retry_delay(story.enrich_attempts), wait_out)
        result.retry_at = now + delay
        story.enrich_status, story.enrich_next_attempt_at = "retry", result.retry_at
        story.enrich_reason = result.reason

    def _degraded_retry(self, session: Session, story: Story, now: datetime) -> bool:
        """A degraded pass is retried while the story is still under ``target_sources``,
        within ``max_age_hours`` and short of ``DEGRADED_MAX_ATTEMPTS`` passes in a row."""
        cfg = self.config
        session.flush()
        return (
            (story.enrich_attempts or 0) < DEGRADED_MAX_ATTEMPTS
            and story.last_updated_at >= now - timedelta(hours=cfg.max_age_hours)
            and self.publishers(session, story.id) < cfg.target_sources
        )

    def _schedule_degraded(self, story: Story, result: StoryEnrichment, now: datetime) -> None:
        """Keep what the fallback found, but not done: retry once the primary's cooldown
        (if any) is over, after the usual backoff at least."""
        story.enrich_attempts = (story.enrich_attempts or 0) + 1
        cooling = self.search.cooldown_remaining(self.config.providers[0].name)
        delay = retry_delay(story.enrich_attempts)
        if cooling > 0:
            delay = max(delay, timedelta(seconds=cooling + 1))
        result.status, result.retry_at = "retry", now + delay
        story.enrich_status, story.enrich_next_attempt_at = "retry", result.retry_at
        story.enrich_reason = result.reason

    def _regen_allowed(self, session: Session, now: datetime) -> bool:
        """Late sources may re-generate at most ``late_regen_max_per_hour`` stories."""
        cfg = self.config
        if not cfg.resummarize_on_enrichment:
            return False
        recent = session.scalar(
            select(func.count())
            .select_from(Story)
            .where(
                Story.enrich_regen_at.is_not(None),
                Story.enrich_regen_at >= now - timedelta(hours=1),
            )
        )
        return (recent or 0) < cfg.late_regen_max_per_hour

    def _add_member(
        self, session: Session, story: Story, c: Candidate, anchor_id: int | None, now
    ) -> int | None:
        page = c.page
        url = c.hit.url
        canonical = canonicalize_url(url)
        if session.scalar(select(Article.id).where(Article.canonical_url == canonical)):
            c.decision, c.reason = "rejected", "already_ingested"
            return None
        untexted = c.decision in ("link_only", "headline_only")
        article = Article(
            source_id=web_source_id(url),
            url=url,
            canonical_url=canonical,
            title=c.hit.title or url,
            summary_raw=c.hit.snippet or "",
            published_at=c.hit.published_at or now,
            fetched_at=now,
            image_url=c.hit.image_url or (page.image_url if page else None),
            content_hash=content_hash(c.hit.title or "", url),
            raw_json={
                "provider": c.provider,
                "query": c.query,
                "similarity": c.similarity,
                "flag": c.flag,
                "headline_only": untexted,
            },
            text=None if untexted else (page.text if page else None),
            text_status=TextStatus.NONE.value if untexted else TextStatus.OK.value,
            text_fetched_at=now,
            word_count=None if untexted else (page.words if page else None),
            story_id=story.id,
            origin=ENRICHMENT,
            discovered_for_story_id=story.id,
            anchor_article_id=anchor_id,
        )
        session.add(article)
        session.flush()
        return article.id

    # --- domain blocking --------------------------------------------------------------

    def block_domain(self, url_or_domain: str) -> dict:
        """Persist ``url_or_domain`` in the blocked-domains table, remove every enrichment
        member and context link on it (canonical domain, so publisher aliases block
        editions too), and queue the affected stories for one regeneration — context
        links never feed briefs, so removing one alone does not requeue. Returns what
        happened."""
        domain = registrable_domain(url_or_domain)
        canonical = canonical_domain(url_or_domain, self.config.publisher_aliases)
        with self.session_factory() as session:
            if session.get(BlockedDomain, domain) is None:
                session.add(BlockedDomain(domain=domain, canonical=canonical))
            blocked = {domain, canonical}
            members = session.scalars(select(Article).where(Article.origin.in_(NON_FEED))).all()
            story_ids: set[int] = set()
            removed = 0
            removed_context = 0
            for m in members:
                if canonical_domain(m.url, self.config.publisher_aliases) not in blocked:
                    continue
                if m.origin == CONTEXT:
                    removed_context += 1
                elif m.story_id:
                    story_ids.add(m.story_id)
                session.delete(m)
                removed += 1
            queued = 0
            for story in session.scalars(select(Story).where(Story.id.in_(story_ids))):
                if story.state == StoryState.MERGED:
                    continue
                if story.state not in PRE_CLUSTER:
                    story.state = StoryState.CLUSTERED.value  # regenerate once
                    queued += 1
            if story_ids:
                from app.cluster.service import refresh_stories

                session.flush()
                refresh_stories(session, story_ids)
            session.commit()
        return {
            "domain": domain,
            "canonical": canonical,
            "removed": removed,
            "removed_context": removed_context,
            "stories": sorted(story_ids),
            "queued": queued,
        }

    # --- stats ------------------------------------------------------------------------

    def stats(self, session: Session, days: int = 7) -> dict:
        since = self.now() - timedelta(days=days)
        reasons = Counter(
            dict(
                session.execute(
                    select(EnrichmentCandidate.reason, func.count())
                    .where(
                        EnrichmentCandidate.created_at >= since,
                        EnrichmentCandidate.decision == "rejected",
                    )
                    .group_by(EnrichmentCandidate.reason)
                ).all()
            )
        )
        enriched = session.scalar(
            select(func.count())
            .select_from(Story)
            .where(Story.enriched_at >= since, Story.enrich_added > 0)
        )
        attempted = session.scalar(
            select(func.count()).select_from(Story).where(Story.enriched_at >= since)
        )
        added = session.scalar(
            select(func.avg(Story.enrich_added)).where(Story.enriched_at >= since)
        )
        pending = session.scalar(
            select(func.count()).select_from(Story).where(Story.enrich_status == "pending")
        )
        retry = session.scalar(
            select(func.count()).select_from(Story).where(Story.enrich_status == "retry")
        )
        next_retry = session.scalar(
            select(func.min(Story.enrich_next_attempt_at)).where(Story.enrich_status == "retry")
        )
        recovered = session.scalar(
            select(func.count())
            .select_from(Story)
            .where(Story.enriched_at >= since, Story.enrich_reason.like(f"{RECOVERED}%"))
        )
        skipped = dict(
            session.execute(
                select(Story.enrich_reason, func.count())
                .where(Story.enriched_at >= since, Story.enrich_reason.like(f"{SKIPPED}%"))
                .group_by(Story.enrich_reason)
            ).all()
        )
        top_domains = Counter(
            canonical_domain(url, self.config.publisher_aliases)
            for (url,) in session.execute(
                select(EnrichmentCandidate.url).where(
                    EnrichmentCandidate.created_at >= since,
                    EnrichmentCandidate.decision.in_(["accepted", "link_only"]),
                )
            )
            if url
        )
        flagged_rows = session.execute(
            select(EnrichmentCandidate.url, EnrichmentCandidate.flagged).where(
                EnrichmentCandidate.created_at >= since,
                EnrichmentCandidate.flagged != "",
            )
        ).all()
        flagged_domains = Counter(
            canonical_domain(url, self.config.publisher_aliases) for url, _ in flagged_rows
        )
        # Deep tier (``enrichment.depth``): stories, the sources-per-story
        # distribution, distinct outlets/countries, and today's request use.
        deep_rows = session.execute(
            select(Story.id, Article.url)
            .join(Article, Article.story_id == Story.id)
            .where(
                Story.enrich_depth == "deep",
                Story.enriched_at >= since,
                Article.origin != CONTEXT,
            )
        ).all()
        deep_total = session.scalar(
            select(func.count())
            .select_from(Story)
            .where(Story.enrich_depth == "deep", Story.enriched_at >= since)
        )
        per_story: dict[int, set[str]] = {sid: set() for sid, _ in deep_rows}
        deep_domains: set[str] = set()
        for sid, url in deep_rows:
            if not url:
                continue
            domain = canonical_domain(url, self.config.publisher_aliases)
            per_story.setdefault(sid, set()).add(domain)
            deep_domains.add(domain)
        dist = {"0": 0, "1-3": 0, "4-7": 0, "8+": 0}
        for sid in {sid for sid, _ in deep_rows} | set(per_story):
            n = len(per_story.get(sid, ()))
            dist["0" if n == 0 else "1-3" if n <= 3 else "4-7" if n <= 7 else "8+"] += 1
        from app.ingest import usage_periods
        from app.outlets import outlets

        registry = outlets()
        deep_countries = {c for d in deep_domains if (c := registry.for_domain(d).country)}
        day, _ = usage_periods(self.now())
        deep_used = session.get(SourceUsage, (DEEP_USAGE, day))
        deep = {
            "stories": deep_total or 0,
            "distribution": dist,
            "outlets": len(deep_domains),
            "countries": len(deep_countries),
            "requests_today": deep_used.requests if deep_used else 0,
            "daily_budget": self.config.depth.daily_request_budget,
        }
        waiting = (pending or 0) + (retry or 0)
        providers = []
        for row in budget_use(session, self.config.providers, self.now()):
            state = session.get(SearchProviderState, row["provider"])
            cooling = self.search.cooldown_remaining(row["provider"])
            providers.append(
                {
                    **row,
                    **self.search._health(row["provider"]).as_dict(),
                    "cooldown_seconds": int(cooling + 0.999) if cooling > 0 else 0,
                    "consecutive_errors": state.consecutive_errors if state else 0,
                    "state_error": state.last_error if state else None,
                }
            )
        return {
            "enabled": bool(self.enabled_profiles(session)),
            "running": self.running,
            "eligible": len(self.eligible(session)),
            "pending": pending or 0,
            "attempted": attempted or 0,
            "enriched": enriched or 0,
            "avg_added": round(float(added or 0), 2),
            "retry": retry or 0,
            "next_retry_at": next_retry.isoformat() if next_retry else None,
            "next_retry_seconds": (
                max(0, int((next_retry - self.now()).total_seconds())) if next_retry else None
            ),
            "waiting": waiting,
            "recovered": recovered or 0,
            "skipped": {k.removeprefix(SKIPPED): v for k, v in sorted(skipped.items())},
            "cooling": [
                {"provider": p["provider"], "seconds": p["cooldown_seconds"], "waiting": waiting}
                for p in providers
                if p["cooldown_seconds"]
            ],
            "reject_reasons": reasons.most_common(8),
            "top_domains": top_domains.most_common(10),
            "flagged": len(flagged_rows),
            "flagged_domains": flagged_domains.most_common(10),
            "blocked_domains": [d for (d,) in session.execute(select(BlockedDomain.domain)).all()],
            "providers": providers,
            "deep": deep if (deep_total or 0) > 0 else None,
            "days": days,
        }


def summarize_run(results: list[StoryEnrichment]) -> dict:
    reasons = Counter(c.reason for r in results for c in r.candidates if c.decision == "rejected")
    errors = [s["error"] for r in results for s in r.searches if s.get("error")]
    return {
        "stories": len(results),
        "enriched": sum(1 for r in results if r.added),
        "added": sum(r.added for r in results),
        "link_only": sum(len(r.untexted) for r in results),
        "flagged": sum(1 for r in results for c in r.candidates if c.flag),
        "failed": sum(1 for r in results if r.status == "failed"),
        "retry": sum(1 for r in results if r.status == "retry"),
        "candidates": sum(len(r.candidates) for r in results),
        "reject_reasons": dict(reasons.most_common(8)),
        "provider_errors": len(errors),
        "skipped": dict(
            Counter(
                r.reason.removeprefix(SKIPPED)
                for r in results
                if r.reason and r.reason.startswith(SKIPPED)
            )
        ),
    }


def enrichment_config(config: AppConfig, profile_slug: str | None = None) -> EnrichmentConfig:
    """The thresholds/providers in use: the summarize profile's ``enrichment`` block."""
    for p in config.profiles:
        if profile_slug is None or p.id == profile_slug:
            return p.enrichment
    return EnrichmentConfig()
