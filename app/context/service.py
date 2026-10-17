"""The context finder: a background thread that searches the web for background /
context pages for published stories ("related, not the same event").

Eligible stories are picked once (``context_status`` NULL -> ``pending`` -> ``done``),
scheduled after the brief is published (``published_version`` set) — it never blocks the
brief. Provider failures retry with the same backoff as enrichment and are never marked
``done``. Accepted pages become ``origin == "context"`` articles pinned to the story;
every considered hit is recorded as a ``ContextCandidate`` (stats + ``diagnose``).
"""

import asyncio
import json
import logging
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import ContextConfig, EnrichmentConfig, LLMRole
from app.enrich.domains import canonical_domain
from app.enrich.filters import StoryContext
from app.enrich.query import story_terms
from app.enrich.service import retry_delay
from app.ingest import usage_periods
from app.llm.client import LLMClient
from app.models import (
    CONTEXT,
    Article,
    BlockedDomain,
    ContextCandidate,
    ProfileRecord,
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
from app.story_views import published_story

logger = logging.getLogger(__name__)

BATCH = 6
"""Stories per pending pass (the providers pace requests anyway)."""
STALE_PENDING = timedelta(minutes=30)
"""A ``pending`` story this old is re-picked (crashed run)."""
UNAVAILABLE = "unavailable:"
JUDGE_CHARS = 1500
"""Only the first N characters of a page go to the LLM judge (slow local LLMs)."""
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "adds_context": {"type": "integer", "enum": [0, 1, 2]},
        "note": {"type": "string"},
    },
    "required": ["adds_context", "note"],
    "additionalProperties": False,
}


def _similar_angle():
    from app.context.angles import Angle

    return Angle(label="related", query="")


@dataclass
class Candidate:
    hit: SearchHit
    provider: str
    query: str
    angle: str = ""
    heuristic: bool = False
    decision: str = "rejected"
    reason: str = ""
    shared_entities: int | None = None
    text_similarity: float | None = None
    judge: int | None = None
    note: str = ""
    page: object | None = None
    article_id: int | None = None
    fetch_detail: str = ""


@dataclass
class ContextResult:
    story_id: int
    angles: list = field(default_factory=list)
    """``app.context.angles.Angle`` objects used for this run."""
    searches: list[dict] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    added: int = 0
    status: str = ""
    reason: str | None = None
    answered: bool = False
    """At least one provider answered (hits or none); otherwise the story is retried."""
    retry_at: datetime | None = None

    @property
    def accepted(self) -> list[Candidate]:
        return [c for c in self.candidates if c.decision == "accepted"]


def _web_source_id(url: str) -> str:
    from app.enrich.service import web_source_id

    return web_source_id(url)


class ContextFinder:
    def __init__(
        self,
        session_factory: sessionmaker,
        config: ContextConfig,
        *,
        enrichment: EnrichmentConfig | None = None,
        profile_defaults: dict[str, bool] | None = None,
        search: SearchService | None = None,
        fetcher=None,
        llm_role: LLMRole | None = None,
        llm_transport=None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.config = config
        self.enrichment = enrichment or EnrichmentConfig()
        """Domain lists (deny/social/aggregator/aliases) are shared with enrichment."""
        self.profile_defaults = profile_defaults or {}
        """YAML ``context.enabled`` per profile slug (a Settings toggle overrides it)."""
        self.search = search or SearchService(session_factory)
        from app.enrich.fetch import CandidateFetcher

        self.fetcher = fetcher or CandidateFetcher(session_factory)
        self.llm_role = llm_role
        self.llm_transport = llm_transport
        self.now = now
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._again = False
        self.last_run: dict | None = None

    # --- eligibility ------------------------------------------------------------------

    def enabled_profiles(self, session: Session) -> list[ProfileRecord]:
        out = []
        for record in session.scalars(select(ProfileRecord)):
            default = self.profile_defaults.get(record.slug, self.config.enabled)
            on = record.context_enabled if record.context_enabled is not None else default
            if on:
                out.append(record)
        return out

    def profile_enabled(self, record: ProfileRecord) -> bool:
        if record.context_enabled is not None:
            return record.context_enabled
        return self.profile_defaults.get(record.slug, self.config.enabled)

    @property
    def any_enabled(self) -> bool:
        with self.session_factory() as session:
            return bool(self.enabled_profiles(session))

    def publishers(self, session: Session, story_id: int) -> int:
        """Distinct publishers covering the event (feed + enrichment members only)."""
        aliases = self.enrichment.publisher_aliases
        urls = session.scalars(
            select(Article.url).where(Article.story_id == story_id, Article.origin != CONTEXT)
        )
        return len({canonical_domain(u, aliases) for u in urls if u})

    def eligible(self, session: Session, story_ids: list[int] | None = None) -> list[int]:
        """Published stories never context-searched (or due to retry), with a member from
        a followed source of a context-enabled profile, priority above
        ``context.min_priority`` or at least ``context.min_publishers`` publishers."""
        profiles = self.enabled_profiles(session)
        if not profiles or not self.config.providers:
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
                Article.origin == "feed",
                Article.source_id.in_(followed),
            )
            .exists()
        )
        never = Story.context_status.is_(None)
        retry_due = and_(
            Story.context_status == "retry",
            or_(
                Story.context_next_attempt_at.is_(None),
                Story.context_next_attempt_at <= now,
            ),
        )
        stale_pending = and_(
            Story.context_status == "pending",
            Story.context_at.is_(None),
        )
        stmt = select(Story).where(
            Story.published_version.is_not(None),
            Story.state == StoryState.PUBLISHED.value,
            Story.last_updated_at >= now - timedelta(hours=cfg.max_age_hours),
            member,
            or_(never, retry_due, stale_pending),
        )
        if story_ids is not None:
            stmt = stmt.where(Story.id.in_(story_ids))
        out = []
        for story in session.scalars(stmt.order_by(Story.last_updated_at.desc())):
            if story.priority <= cfg.min_priority and (
                self.publishers(session, story.id) < cfg.min_publishers
            ):
                continue
            out.append(story.id)
        return out

    # --- scheduling -------------------------------------------------------------------

    def schedule(self, story_ids: list[int] | None = None, *, force: bool = False) -> int:
        """Mark eligible stories ``pending`` and start the background run. ``force`` (the
        admin "Find context now") ignores eligibility, but a ``done`` story still needs
        an explicit story_ids override."""
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
                story.context_status = "pending"
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
                target=self.run_blocking, name="context-finder", daemon=True
            )
            self._thread.start()
            return True

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_blocking(self) -> list[ContextResult]:
        results: list[ContextResult] = []
        while True:
            results += asyncio.run(self.run_pending())
            with self._lock:
                if not self._again:
                    return results
                self._again = False

    def pending(self, session: Session) -> list[int]:
        stmt = (
            select(Story.id)
            .where(
                or_(
                    Story.context_status == "pending",
                    and_(
                        Story.context_status == "retry",
                        or_(
                            Story.context_next_attempt_at.is_(None),
                            Story.context_next_attempt_at <= self.now(),
                        ),
                    ),
                )
            )
            .order_by(Story.last_updated_at.desc())
            .limit(BATCH)
        )
        return list(session.scalars(stmt))

    async def run_pending(self) -> list[ContextResult]:
        results: list[ContextResult] = []
        while True:
            with self.session_factory() as session:
                ids = self.pending(session)
            if not ids:
                break
            for sid in ids:
                results.append(await self.find_story(sid))
        if results:
            self.last_run = summarize_run(results)
            try:
                from app.runs import record_run

                record_run(self.session_factory, "context", self.last_run)
            except Exception:
                logger.exception("could not record the context run")
        return results

    # --- one story --------------------------------------------------------------------

    def story_context(self, session: Session, story_id: int) -> tuple[StoryContext, Article] | None:
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
        if not members:
            return None
        anchor = next((m for m in members if m.id == story.representative_article_id), members[0])
        domains = {
            canonical_domain(m.url, self.enrichment.publisher_aliases) for m in members if m.url
        }
        # already-listed context links also block the domain (one link per domain)
        for m in session.scalars(
            select(Article).where(Article.story_id == story_id, Article.origin == CONTEXT)
        ):
            if m.url:
                domains.add(canonical_domain(m.url, self.enrichment.publisher_aliases))
        canonical_urls = {m.canonical_url for m in members}
        return (
            StoryContext(
                title=story.title or anchor.title or "",
                headlines=[story.title or anchor.title or ""]
                + [m.title or "" for m in members[1:]],
                texts=[
                    (m.text if m.text_status == TextStatus.OK.value else None)
                    or ("" if m.text_status == "none" else m.summary_raw or "")
                    for m in members
                ],
                domains=domains,
                canonical_urls=canonical_urls,
                first_at=min(m.published_at for m in members),
                last_at=max(m.published_at for m in members),
                language=self.config.language,
            ),
            anchor,
        )

    def _brief_text(self, session: Session, story_id: int) -> str:
        story = published_story(session, story_id)
        if story is not None and story.brief is not None:
            return story.brief.text
        return ""

    async def find_story(self, story_id: int, *, dry_run: bool = False) -> ContextResult:
        result = ContextResult(story_id=story_id)
        try:
            await self._find(result, dry_run)
        except Exception as exc:
            logger.exception("context search of story %s failed", story_id)
            result.status, result.reason = "failed", f"{type(exc).__name__}: {exc}"[:500]
            if not dry_run:
                self._finish(result)
        return result

    async def _find(self, result: ContextResult, dry_run: bool) -> None:
        from app.context.angles import make_angles

        cfg = self.config
        with self.session_factory() as session:
            found = self.story_context(session, result.story_id)
            if found is None:
                result.status, result.reason = "done", "story gone"
                return
            story, anchor = found
            brief = self._brief_text(session, result.story_id)
            deny = [
                *self.enrichment.deny_domains,
                *self.enrichment.quality.deny_domains,
                *session.scalars(select(BlockedDomain.domain)),
                *session.scalars(select(BlockedDomain.canonical)),
            ]
        terms = story_terms(story.headlines)
        result.angles = await make_angles(
            story.title,
            brief,
            terms,
            max_angles=cfg.max_angles,
            providers=[p.name for p in cfg.providers],
            llm_role=self.llm_role,
            llm_transport=self.llm_transport,
        )
        if not result.angles:
            result.status, result.reason = "done", "no_angles"
            return

        hits = await self._search_all(result, anchor, dry_run)
        candidates = [
            Candidate(hit=h, provider=p, query=q, angle=a.label, heuristic=a.heuristic)
            for h, p, q, a in hits
        ]
        result.candidates = candidates
        self._prescreen(candidates, story, deny)
        await self._fetch_judge(candidates, story, brief, result)
        if not dry_run:
            self._finish(result, anchor_id=anchor.id)

    async def _search_all(
        self, result: ContextResult, anchor: Article, dry_run: bool
    ) -> list[tuple]:
        """``(hit, provider, query, angle)`` tuples: each provider x angle query, plus a
        ``find_similar`` call on the anchor URL for providers that support it (exa)."""
        cfg = self.config
        out = []
        since = self.now() - timedelta(hours=cfg.max_age_hours * 4)
        for provider in cfg.providers:
            for angle in result.angles:
                if not self._budget_ok():
                    result.searches.append(
                        {
                            "provider": provider.name,
                            "query": angle.query,
                            "hits": 0,
                            "skipped": "context_budget",
                        }
                    )
                    continue
                outcome = await self.search.search(
                    provider,
                    angle.query,
                    since=since,
                    language=cfg.language,
                    cache_hours=cfg.cache_hours,
                    empty_cache_minutes=cfg.empty_cache_minutes,
                    store=not dry_run,
                )
                result.searches.append(
                    {
                        "provider": provider.name,
                        "query": angle.query,
                        "angle": angle.label,
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
                self._count_requests(provider.name, outcome.requests)
                if outcome.error is None or outcome.hits:
                    result.answered = True
                for h in outcome.hits:
                    out.append((h, provider.name, angle.query, angle))
            outcome = await self._find_similar(provider, anchor)
            if outcome is not None and outcome.hits:
                similar = _similar_angle()
                for h in outcome.hits:
                    out.append((h, provider.name, outcome.query, similar))
                result.answered = True
            elif outcome is not None and outcome.error is None:
                result.answered = True
        # dedupe by canonical url (first provider/angle wins)
        seen, unique = set(), []
        for h, p, q, a in out:
            key = canonicalize_url(h.url)
            if key in seen:
                continue
            seen.add(key)
            unique.append((h, p, q, a))
        return unique

    async def _find_similar(self, provider, anchor: Article):
        """``provider.find_similar(anchor.url)`` for providers with
        ``supports_find_similar`` (exa): one extra shot at pages covering the same
        subject — the filters still reject same-event coverage afterwards."""
        import httpx

        from app.search.base import create_provider
        from app.search.service import SearchOutcome
        from app.sources.base import FetchError

        if provider.name not in PROVIDERS:
            return None
        outcome = SearchOutcome(provider=provider.name, query=f"similar:{anchor.url}")
        try:
            async with httpx.AsyncClient(
                timeout=provider.timeout_seconds,
                transport=self.search.transport,
            ) as client:
                instance = create_provider(provider, client)
                if not getattr(instance, "supports_find_similar", False):
                    return None
                outcome.hits = await instance.find_similar(
                    anchor.url,
                    self.now() - timedelta(hours=self.config.max_age_hours * 4),
                    self.config.language,
                    provider.max_results,
                    self.search._quota(provider),
                )
                outcome.requests = 1
        except FetchError as exc:
            outcome.error, outcome.kind = str(exc), "error"
        except Exception:
            logger.exception("find_similar failed for %s", provider.name)
            outcome.error, outcome.kind = "find_similar failed", "error"
        return outcome

    # --- filters ----------------------------------------------------------------------

    def _prescreen(self, candidates: list[Candidate], story: StoryContext, deny: list[str]) -> int:
        from app.context.filters import context_pre_filter

        cfg = self.config
        with self.session_factory() as session:
            for c in candidates:
                if c.reason:
                    continue
                c.reason = (
                    context_pre_filter(
                        c.hit,
                        story,
                        deny=deny,
                        language=cfg.language,
                        aliases=self.enrichment.publisher_aliases,
                        social=self.enrichment.social_domains,
                        aggregators=self.enrichment.aggregator_domains,
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

    async def _fetch_judge(
        self, candidates: list[Candidate], story: StoryContext, brief: str, result
    ) -> None:
        from app.context.filters import context_text_check

        cfg = self.config
        accepted_texts: list[str] = []
        kept = 0
        for c in candidates:
            if c.reason or kept >= cfg.max_items:
                if not c.reason:
                    c.reason = "not_needed"
                continue
            c.page = await self.fetcher.fetch(c.hit.url)
            if c.page.status != "ok":
                c.fetch_detail = f"{c.page.status}: {c.page.reason or ''}".strip(": ")
                c.reason = {
                    "blocked": "blocked_url",
                    "paywalled": "paywalled",
                    "skipped": "robots",
                }.get(c.page.status, "fetch_failed")
                continue
            if c.page.words < cfg.min_words:
                c.reason = "too_short"
                continue
            verdict = context_text_check(
                c.hit.title,
                c.page.text,
                story,
                floor=cfg.similarity_floor,
                ceiling=cfg.similarity_ceiling,
                min_entities=(
                    cfg.min_shared_entities_heuristic if c.heuristic else cfg.min_shared_entities
                ),
                duplicate_overlap=0.6,
                accepted_texts=accepted_texts,
            )
            c.shared_entities, c.text_similarity = verdict.entities, verdict.score
            if verdict.reason:
                c.reason = verdict.reason
                continue
            judge = await self._judge(story.title, brief, c)
            if judge is None and cfg.llm_judge and self.llm_role is not None:
                c.reason = "judge_failed"  # never fabricate a note
                continue
            if judge is not None:
                c.judge, c.note = judge
                if c.judge != 2:
                    c.reason = "judge_rejected"
                    continue
            c.decision = "accepted"
            accepted_texts.append(c.page.text)
            kept += 1
        result.answered = result.answered or bool(candidates)

    async def _judge(self, title: str, brief: str, c: Candidate) -> tuple[int, str] | None:
        """0 = off topic, 1 = same event (coverage), 2 = adds background. Returns None on
        failure/unparsable output — the caller then rejects (never fabricates a note)."""
        if self.llm_role is None or not self.config.llm_judge:
            return None
        role = self.llm_role.model_copy(update={"disable_thinking": True, "max_tokens": 200})
        text = (c.page.text or "")[:JUDGE_CHARS] if c.page else ""
        messages = [
            {
                "role": "system",
                "content": (
                    "Decide whether a web page adds useful background/context to a news "
                    "story: history, context, data, or an opposing view. It must NOT be "
                    'coverage of the same event. Answer JSON {"adds_context": 0|1|2, '
                    '"note": "<=25 words for a reader explaining what it adds"}: '
                    "0 off-topic, 1 same event, 2 good background."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Story: {title}\n\nBrief: {brief[:1500]}\n\n"
                    f"Page title: {c.hit.title}\nPage text: {text}"
                ),
            },
        ]
        try:
            client = LLMClient(role, transport=self.llm_transport)
            result = await client.chat(
                messages, json_schema=JUDGE_SCHEMA, schema_name="context_judge"
            )
            data = json.loads(result.content)
            score = int(data["adds_context"])
            note = str(data.get("note") or "")[:280]
            return (score, note) if score in (0, 1, 2) else None
        except Exception as exc:
            logger.info("context judge failed for %s: %s", c.hit.url, exc)
            return None

    # --- budget ------------------------------------------------------------------------

    def _context_usage(self, name: str) -> int:
        """Context searches today for one provider (its own ``context:<name>`` usage row;
        the shared ``search:<name>`` budget is charged by SearchService separately)."""
        day, _month = usage_periods(self.now())
        with self.session_factory() as session:
            row = session.get(SourceUsage, (f"context:{name}", day))
            return row.requests if row else 0

    def _budget_ok(self) -> bool:
        """``context.daily_request_budget``: a cap on this finder's requests/day (the
        providers' own budgets under ``search:<name>`` still apply inside SearchService)."""
        budget = self.config.daily_request_budget
        if budget is None:
            return True
        return sum(self._context_usage(p.name) for p in self.config.providers) < budget

    def _count_requests(self, name: str, requests: int) -> None:
        if not requests:
            return
        with self.session_factory() as session:
            for period in usage_periods(self.now()):
                row = session.get(SourceUsage, (f"context:{name}", period)) or SourceUsage(
                    source_id=f"context:{name}", period=period, requests=0, reads=0, spend_usd=0.0
                )
                row.requests = (row.requests or 0) + requests
                session.add(row)
            session.commit()

    # --- finish / stats -----------------------------------------------------------------

    def _add_link(
        self, session: Session, story: Story, c: Candidate, anchor_id: int | None, now
    ) -> int | None:
        canonical = canonicalize_url(c.hit.url)
        if session.scalar(select(Article.id).where(Article.canonical_url == canonical)):
            c.decision, c.reason = "rejected", "already_ingested"
            return None
        article = Article(
            source_id=_web_source_id(c.hit.url),
            url=c.hit.url,
            canonical_url=canonical,
            title=c.hit.title or c.hit.url,
            summary_raw=c.hit.snippet or "",
            published_at=c.hit.published_at or now,
            fetched_at=now,
            image_url=c.hit.image_url or (c.page.image_url if c.page else None),
            content_hash=content_hash(c.hit.title or "", c.hit.url),
            raw_json={"provider": c.provider, "query": c.query, "angle": c.angle},
            text=c.page.text if c.page else None,
            text_status=TextStatus.OK.value,
            text_fetched_at=now,
            word_count=c.page.words if c.page else None,
            story_id=story.id,
            origin=CONTEXT,
            discovered_for_story_id=story.id,
            anchor_article_id=anchor_id,
            context_note=c.note or None,
            context_angle=c.angle or None,
        )
        session.add(article)
        session.flush()
        return article.id

    def _finish(self, result: ContextResult, *, anchor_id: int | None = None) -> None:
        now = self.now()
        run_id = uuid.uuid4().hex
        with self.session_factory() as session:
            story = session.get(Story, result.story_id)
            if story is None or story.state == StoryState.MERGED:
                result.status, result.reason = "failed", "story gone"
                return
            if not result.answered and result.status != "done":
                # no provider answered: retry with backoff, never "done"
                story.context_attempts = (story.context_attempts or 0) + 1
                delay = retry_delay(story.context_attempts)
                result.retry_at = now + delay
                story.context_status = "retry"
                story.context_next_attempt_at = result.retry_at
                story.context_reason = result.reason or f"{UNAVAILABLE} no provider answered"
                session.commit()
                return
            for c in result.accepted:
                c.article_id = self._add_link(session, story, c, anchor_id, now)
            result.added = sum(1 for c in result.accepted if c.article_id is not None)
            for c in result.candidates:
                session.add(
                    ContextCandidate(
                        story_id=story.id,
                        run_id=run_id,
                        provider=c.provider,
                        query=c.query,
                        angle=c.angle,
                        url=c.hit.url[:2048],
                        domain=c.hit.domain[:300],
                        title=c.hit.title,
                        decision=c.decision,
                        reason=c.reason[:40],
                        shared_entities=c.shared_entities,
                        text_similarity=c.text_similarity,
                        judge=c.judge,
                        note=c.note,
                        article_id=c.article_id,
                        created_at=now,
                    )
                )
            result.status = "failed" if result.status == "failed" else "done"
            story.context_status = result.status
            story.context_at = now
            story.context_attempts, story.context_next_attempt_at = 0, None
            story.context_reason = result.reason
            session.commit()

    def stats(self, session: Session, days: int = 7) -> dict:
        cfg = self.config
        since = self.now() - timedelta(days=days)
        reasons = Counter(
            dict(
                session.execute(
                    select(ContextCandidate.reason, func.count())
                    .where(
                        ContextCandidate.created_at >= since,
                        ContextCandidate.decision == "rejected",
                    )
                    .group_by(ContextCandidate.reason)
                ).all()
            )
        )
        added_links = session.scalar(
            select(func.count())
            .select_from(Article)
            .where(Article.origin == CONTEXT, Article.fetched_at >= since)
        )
        done = session.scalar(
            select(func.count())
            .select_from(Story)
            .where(Story.context_at >= since, Story.context_status == "done")
        )
        with_links = session.scalar(
            select(func.count(func.distinct(ContextCandidate.story_id))).where(
                ContextCandidate.created_at >= since,
                ContextCandidate.decision == "accepted",
            )
        )
        pending = session.scalar(
            select(func.count()).select_from(Story).where(Story.context_status == "pending")
        )
        retry = session.scalar(
            select(func.count()).select_from(Story).where(Story.context_status == "retry")
        )
        judged = session.scalar(
            select(func.count())
            .select_from(ContextCandidate)
            .where(ContextCandidate.created_at >= since, ContextCandidate.judge.is_not(None))
        )
        judge_ok = session.scalar(
            select(func.count())
            .select_from(ContextCandidate)
            .where(ContextCandidate.created_at >= since, ContextCandidate.judge == 2)
        )
        providers = []
        for row in budget_use(session, cfg.providers, self.now()):
            cooling = self.search.cooldown_remaining(row["provider"])
            providers.append(
                {
                    **row,
                    "context_requests_today": self._context_usage(row["provider"]),
                    "cooldown_seconds": int(cooling + 0.999) if cooling > 0 else 0,
                }
            )
        return {
            "enabled": bool(self.enabled_profiles(session)),
            "running": self.running,
            "eligible": len(self.eligible(session)),
            "pending": pending or 0,
            "retry": retry or 0,
            "waiting": (pending or 0) + (retry or 0),
            "done": done or 0,
            "with_links": with_links or 0,
            "added_links": added_links or 0,
            "judge_rate": round(judge_ok / judged, 2) if judged else None,
            "reject_reasons": reasons.most_common(8),
            "providers": providers,
            "days": days,
        }


def summarize_run(results: list[ContextResult]) -> dict:
    reasons = Counter(c.reason for r in results for c in r.candidates if c.decision == "rejected")
    errors = [s["error"] for r in results for s in r.searches if s.get("error")]
    return {
        "stories": len(results),
        "with_links": sum(1 for r in results if r.added),
        "added": sum(r.added for r in results),
        "failed": sum(1 for r in results if r.status == "failed"),
        "retry": sum(1 for r in results if r.status == "retry"),
        "candidates": sum(len(r.candidates) for r in results),
        "angles": sum(len(r.angles) for r in results),
        "reject_reasons": dict(reasons.most_common(8)),
        "provider_errors": len(errors),
    }


def context_config(config, profile_slug: str | None = None) -> ContextConfig:
    """The thresholds/providers in use: the summarize profile's ``context`` block."""
    for p in config.profiles:
        if profile_slug is None or p.id == profile_slug:
            return p.context
    return ContextConfig()
