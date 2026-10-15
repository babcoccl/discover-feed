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
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import AppConfig, EnrichmentConfig, LLMRole
from app.enrich.domains import registrable_domain
from app.enrich.fetch import CandidateFetcher, FetchedPage
from app.enrich.filters import StoryContext, pre_filter, snippet_check, text_check
from app.enrich.query import build_queries
from app.lifecycle import PRE_CLUSTER
from app.models import (
    ENRICHMENT,
    Article,
    EnrichmentCandidate,
    ProfileRecord,
    Story,
    StoryState,
    TextStatus,
    profile_sources,
    utcnow,
)
from app.normalize import canonicalize_url, content_hash
from app.search.base import SearchHit
from app.search.service import SearchService, budget_use

logger = logging.getLogger(__name__)

STALE_PENDING = timedelta(hours=1)
"""A ``pending`` mark older than this (the process died mid-run) is picked up again."""
BATCH = 20


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
    text_similarity: float | None = None
    page: FetchedPage | None = None
    article_id: int | None = None

    def as_dict(self) -> dict:
        return {
            "url": self.hit.url,
            "domain": self.hit.domain,
            "title": self.hit.title,
            "provider": self.provider,
            "decision": self.decision,
            "reason": self.reason,
            "similarity": self.similarity,
            "text_similarity": self.text_similarity,
        }


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

    @property
    def accepted(self) -> list[Candidate]:
        return [c for c in self.candidates if c.decision == "accepted"]


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
                Article.origin != ENRICHMENT,
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
        for story in session.scalars(stmt.order_by(Story.last_updated_at.desc())):
            if not self._due(story, now):
                continue
            if story.published_version is not None and (
                not cfg.resummarize_on_enrichment or story.enrich_regens >= 1
            ):
                continue  # late sources would re-generate a published story more than once
            if self.publishers(session, story.id) >= cfg.target_sources:
                continue
            out.append(story.id)
        return out

    def _due(self, story: Story, now: datetime) -> bool:
        if story.enrich_status is None:
            return True
        if story.enrich_status == "pending":
            return (story.enrich_started_at or now) < now - STALE_PENDING
        if not self.config.reenrich_hours:
            return False
        last = story.enriched_at or story.enrich_started_at
        return last is None or last < now - timedelta(hours=self.config.reenrich_hours)

    @staticmethod
    def publishers(session: Session, story_id: int) -> int:
        urls = session.scalars(select(Article.url).where(Article.story_id == story_id))
        return len({registrable_domain(u) for u in urls if u})

    # --- scheduling -------------------------------------------------------------------

    def schedule(self, story_ids: list[int] | None = None, *, force: bool = False) -> int:
        """Mark eligible stories ``pending`` (their briefs wait) and start the background
        run. ``force``: the admin "Enrich now" (ignores the re-enrich interval)."""
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
                ids = self.eligible(session, story_ids)
            for sid in ids:
                story = session.get(Story, sid)
                story.enrich_status, story.enrich_started_at = "pending", now
                story.enrich_reason = None
            session.commit()
        if ids:
            self.start()
        return len(ids)

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._again = True
                return False
            self._thread = threading.Thread(
                target=self.run_blocking, name="enrichment", daemon=True
            )
            self._thread.start()
            return True

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_blocking(self) -> list[StoryEnrichment]:
        results: list[StoryEnrichment] = []
        while True:
            results += asyncio.run(self.run_pending())
            with self._lock:
                if not self._again:
                    return results
                self._again = False

    def pending(self, session: Session) -> list[int]:
        stmt = (
            select(Story.id)
            .where(Story.enrich_status == "pending")
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

    def context(self, session: Session, story_id: int) -> tuple[StoryContext, Article] | None:
        story = session.get(Story, story_id)
        if story is None or story.state == StoryState.MERGED:
            return None
        members = list(
            session.scalars(
                select(Article)
                .where(Article.story_id == story_id)
                .order_by(Article.published_at, Article.id)
            )
        )
        feed = [m for m in members if m.origin != ENRICHMENT]
        if not feed:
            return None
        anchor = next((m for m in feed if m.id == story.representative_article_id), feed[0])
        headlines = [anchor.title or ""] + [m.title or "" for m in feed if m is not anchor]
        return (
            StoryContext(
                title=story.title or anchor.title or "",
                headlines=[h for h in headlines if h],
                texts=[
                    (m.text if m.text_status == "ok" else None) or m.summary_raw or ""
                    for m in members
                ],
                domains={registrable_domain(m.url) for m in members if m.url},
                canonical_urls={m.canonical_url for m in members},
                first_at=min(m.published_at for m in members),
                last_at=max(m.published_at for m in members),
                language=self.config.language,
            ),
            anchor,
        )

    async def enrich_story(self, story_id: int, *, dry_run: bool = False) -> StoryEnrichment:
        result = StoryEnrichment(story_id=story_id)
        try:
            await self._enrich(result, dry_run)
        except Exception as exc:
            logger.exception("enrichment of story %s failed", story_id)
            result.status, result.reason = "failed", f"{type(exc).__name__}: {exc}"[:500]
            if not dry_run:
                self._finish(result)
        return result

    async def _enrich(self, result: StoryEnrichment, dry_run: bool) -> None:
        cfg = self.config
        with self.session_factory() as session:
            found = self.context(session, result.story_id)
            result.publishers_before = self.publishers(session, result.story_id)
        if found is None:
            result.status, result.reason = "failed", "story gone or has no feed members"
            if not dry_run:
                self._finish(result)
            return
        story, anchor = found
        need = max(0, min(cfg.max_added_sources, cfg.target_sources - result.publishers_before))
        if dry_run:
            need = need or cfg.max_added_sources
        result.queries, result.query_mode = await build_queries(
            story.headlines,
            mode=cfg.query_mode.value,
            role=self.llm_role,
            transport=self.llm_transport,
        )
        if not result.queries or need == 0:
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
            fresh: list[Candidate] = []
            for query in result.queries:
                outcome = await self.search.search(
                    provider,
                    query,
                    since=since,
                    language=cfg.language,
                    cache_hours=cfg.cache_hours,
                )
                result.searches.append(
                    {
                        "provider": provider.name,
                        "query": query,
                        "hits": len(outcome.hits),
                        "cached": outcome.cached,
                        "error": outcome.error,
                        "raw": outcome.raw[:600],
                    }
                )
                for hit in outcome.hits:
                    key = canonicalize_url(hit.url) if hit.url.startswith("http") else hit.url
                    if key in seen:
                        continue
                    seen.add(key)
                    fresh.append(Candidate(hit=hit, provider=provider.name, query=query))
            await self._judge(fresh, story, need - len(result.accepted), accepted_texts)
            result.candidates += fresh
        if not dry_run:
            self._finish(result, anchor_id=anchor.id)

    async def _judge(
        self, candidates: list[Candidate], story: StoryContext, need: int, accepted_texts: list
    ) -> None:
        cfg = self.config
        gap = timedelta(hours=cfg.max_time_gap_hours)
        keep: list[Candidate] = []
        with self.session_factory() as session:
            for c in candidates:
                c.reason = (
                    pre_filter(
                        c.hit, story, deny=cfg.deny_domains, max_gap=gap, language=cfg.language
                    )
                    or ""
                )
                if not c.reason and session.scalar(
                    select(func.count())
                    .select_from(Article)
                    .where(Article.canonical_url == canonicalize_url(c.hit.url))
                ):
                    c.reason = "already_ingested"
                if c.reason:
                    continue
                reason, c.similarity = snippet_check(c.hit, story, cfg.min_snippet_similarity)
                if reason:
                    c.reason = reason
                    continue
                keep.append(c)
        keep.sort(key=lambda c: -(c.similarity or 0))
        for i, c in enumerate(keep):
            if need <= 0 or i >= cfg.max_candidates:
                c.reason = "not_needed" if need <= 0 else "over_candidate_limit"
                continue
            domain = registrable_domain(c.hit.url)
            if domain in story.domains:
                c.reason = "same_domain"
                continue
            c.page = await self.fetcher.fetch(c.hit.url)
            if c.page.status != "ok":
                c.reason = {
                    "blocked": "blocked_url",
                    "paywalled": "paywalled",
                    "skipped": "robots",
                }.get(c.page.status, "fetch_failed")
                continue
            reason, c.text_similarity = text_check(
                c.hit.title,
                c.page.text,
                story,
                threshold=cfg.min_text_similarity,
                min_entities=cfg.min_shared_entities,
                duplicate_overlap=cfg.duplicate_overlap,
                accepted_texts=accepted_texts,
            )
            if reason:
                c.reason = reason
                continue
            c.decision, c.reason = "accepted", "accepted"
            accepted_texts.append(c.page.text)
            story.domains.add(domain)
            need -= 1

    def _finish(self, result: StoryEnrichment, *, anchor_id: int | None = None) -> None:
        now = self.now()
        run_id = uuid.uuid4().hex
        with self.session_factory() as session:
            story = session.get(Story, result.story_id)
            if story is None or story.state == StoryState.MERGED:
                result.status, result.reason = "failed", "story gone"
                return
            accepted = result.accepted
            late = story.published_version is not None
            if (
                accepted
                and late
                and (not self.config.resummarize_on_enrichment or story.enrich_regens >= 1)
            ):
                for c in accepted:
                    c.decision, c.reason = "rejected", "late"
                accepted = []
            for c in accepted:
                c.article_id = self._add_member(session, story, c, anchor_id, now)
            result.added = len(accepted)
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
                        similarity=c.similarity,
                        text_similarity=c.text_similarity,
                        article_id=c.article_id,
                        created_at=now,
                    )
                )
            story.enrich_status = result.status if result.status == "failed" else "done"
            story.enriched_at = now
            story.enrich_reason = result.reason
            if result.added:
                from app.cluster.service import refresh_stories

                story.enrich_added = (story.enrich_added or 0) + result.added
                if late:
                    story.enrich_regens = (story.enrich_regens or 0) + 1
                if story.state not in PRE_CLUSTER:
                    story.state = StoryState.CLUSTERED.value  # regenerate; published stays
                session.flush()
                refresh_stories(session, {story.id})
            session.commit()

    def _add_member(
        self, session: Session, story: Story, c: Candidate, anchor_id: int | None, now
    ) -> int | None:
        page = c.page
        url = c.hit.url
        canonical = canonicalize_url(url)
        if session.scalar(select(Article.id).where(Article.canonical_url == canonical)):
            c.decision, c.reason = "rejected", "already_ingested"
            return None
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
            raw_json={"provider": c.provider, "query": c.query, "similarity": c.similarity},
            text=page.text if page else None,
            text_status=TextStatus.OK.value,
            text_fetched_at=now,
            word_count=page.words if page else None,
            story_id=story.id,
            origin=ENRICHMENT,
            discovered_for_story_id=story.id,
            anchor_article_id=anchor_id,
        )
        session.add(article)
        session.flush()
        return article.id

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
        return {
            "enabled": bool(self.enabled_profiles(session)),
            "running": self.running,
            "eligible": len(self.eligible(session)),
            "pending": pending or 0,
            "attempted": attempted or 0,
            "enriched": enriched or 0,
            "avg_added": round(float(added or 0), 2),
            "reject_reasons": reasons.most_common(8),
            "providers": [
                {
                    **row,
                    **self.search._health(row["provider"]).as_dict(),
                }
                for row in budget_use(session, self.config.providers, self.now())
            ],
            "days": days,
        }


def summarize_run(results: list[StoryEnrichment]) -> dict:
    reasons = Counter(c.reason for r in results for c in r.candidates if c.decision != "accepted")
    errors = [s["error"] for r in results for s in r.searches if s.get("error")]
    return {
        "stories": len(results),
        "enriched": sum(1 for r in results if r.added),
        "added": sum(r.added for r in results),
        "failed": sum(1 for r in results if r.status == "failed"),
        "candidates": sum(len(r.candidates) for r in results),
        "reject_reasons": dict(reasons.most_common(8)),
        "provider_errors": len(errors),
    }


def enrichment_config(config: AppConfig, profile_slug: str | None = None) -> EnrichmentConfig:
    """The thresholds/providers in use: the summarize profile's ``enrichment`` block."""
    for p in config.profiles:
        if profile_slug is None or p.id == profile_slug:
            return p.enrichment
    return EnrichmentConfig()
