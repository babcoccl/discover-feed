"""Per-source digests and the story-level "how outlets differ" synthesis.

Digests are generated lazily (only for stories that qualify) and cached by the
article's content, so a re-run or a story re-version reuses them. The synthesis is
stored as a ``story_summaries`` row with ``kind="perspectives"``; its ``input_hash``
covers the member set, each member's text and the prompt/model, which gives "once
per story version" for free.

GET routes never generate; generation happens only from the summary worker's
perspectives pass or the diagnose/evaluate tools.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import telemetry
from app.labels import publisher_of, via_of
from app.llm.client import LLMClient
from app.models import (
    Article,
    ArticleDigest,
    ProfileRecord,
    Story,
    StoryState,
    StorySummary,
    SummaryJob,
    SummaryStatus,
    TextStatus,
    profile_sources,
    utcnow,
)
from app.perspectives.prompt import (
    DIGEST_PROMPT_VERSION,
    DIGEST_SCHEMA,
    PERSPECTIVES_PROMPT_VERSION,
    SYNTH_SCHEMA,
    digest_messages,
    synthesis_messages,
)
from app.perspectives.validate import title_digest, validate_digest, validate_synthesis
from app.summarize.prompt import SourceDoc
from app.summarize.validate import SummaryInvalid, VerbatimGuard

logger = logging.getLogger(__name__)

KIND = "perspectives"
"""``story_summaries.kind`` / ``summary_jobs.kind`` for perspectives artifacts."""

TRIES = 3


@dataclass
class PerspectiveOutcome:
    story_id: int
    status: str
    """``generated`` | ``cached`` | ``skipped`` | ``failed``."""
    reason: str | None = None
    digests: int = 0
    detail: dict[str, Any] | None = None
    """Dry runs only: the digests + synthesis for review (never written)."""


@dataclass
class _Prepared:
    """A member article with its digest, ready for the synthesis prompt."""

    article: Article
    digest: dict
    label: str
    text: str
    """The words quote/number checks run against (extracted text or feed summary)."""


def _sha(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _basis(article: Article) -> tuple[str, str]:
    """``(basis, text)``: headline-only members digest their title; others their
    extracted text (or feed summary when extraction failed)."""
    if article.text_status == TextStatus.OK.value and article.text:
        return "full_text", article.text
    if article.text_status == "none" or not (article.text or article.summary_raw):
        return "title", article.title or ""
    return "feed_summary", article.text or article.summary_raw or ""


def _digest_key(article: Article, claim: str, model: str) -> str:
    """The digest cache key: the article's content + the claim it is judged against +
    prompt/model."""
    basis, text = _basis(article)
    return _sha(
        {
            "kind": "digest",
            "basis": basis,
            "text": text,
            "claim": claim,
            "prompt_version": DIGEST_PROMPT_VERSION,
            "model": model,
        }
    )


class PerspectiveService:
    """Generates and stores digests + synthesis; never blocks the brief pipeline."""

    def __init__(
        self,
        session_factory: sessionmaker,
        client: LLMClient | None,
        options,
        *,
        profile_defaults: dict[str, bool] | None = None,
        max_age_hours: float = 168,
        clock=utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.client = client
        self.options = options
        self.profile_defaults = profile_defaults or {}
        self.max_age_hours = max_age_hours
        self.clock = clock

    @property
    def model(self) -> str:
        return self.client.model if self.client else ""

    # --- profile gating ---------------------------------------------------------------

    def enabled_profiles(self, session: Session) -> list[ProfileRecord]:
        return [
            record
            for record in session.scalars(select(ProfileRecord))
            if self.profile_enabled(record)
        ]

    def profile_enabled(self, record: ProfileRecord) -> bool:
        if record.perspectives_enabled is not None:
            return record.perspectives_enabled
        return self.profile_defaults.get(record.slug, self.options.enabled)

    @property
    def any_enabled(self) -> bool:
        with self.session_factory() as session:
            return bool(self.enabled_profiles(session))

    # --- eligibility --------------------------------------------------------------------

    def rate_limited(self, session: Session) -> bool:
        """True once ``max_per_hour`` perspectives runs happened in the last hour."""
        recent = session.scalar(
            select(func.count())
            .select_from(StorySummary)
            .where(
                StorySummary.kind == KIND,
                StorySummary.created_at > self.clock() - timedelta(hours=1),
            )
        )
        return (recent or 0) >= self.options.max_per_hour

    def candidates(self, session: Session, limit: int = 10) -> list[int]:
        """Published stories that could gain a perspectives section (newest first)."""
        if self.client is None or self.rate_limited(session):
            return []
        profiles = self.enabled_profiles(session)
        if not profiles:
            return []
        now = self.clock()
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
        backing_off = select(SummaryJob.story_id).where(
            SummaryJob.kind == KIND, SummaryJob.next_attempt_at > now
        )
        stmt = (
            select(Story.id)
            .where(
                Story.state == StoryState.PUBLISHED.value,
                Story.last_updated_at >= now - timedelta(hours=self.max_age_hours),
                member,
                ~Story.id.in_(backing_off),
            )
            .order_by(Story.last_updated_at.desc(), Story.id.desc())
            .limit(limit * 3)
        )
        ids = []
        for sid in session.scalars(stmt):
            if not self.is_current(session, sid):
                ids.append(sid)
            if len(ids) >= limit:
                break
        return ids

    def is_current(self, session: Session, story_id: int) -> bool:
        """True when the story's perspectives row covers its current member set."""
        members = self._members(session, story_id)
        if len(members) < self.options.min_sources:
            return True
        key = self._key(session, story_id, members)
        return (
            session.scalar(
                select(StorySummary.id).where(
                    StorySummary.story_id == story_id,
                    StorySummary.kind == KIND,
                    StorySummary.input_hash == key,
                )
            )
            is not None
        )

    # --- members / claim -----------------------------------------------------------------

    def _members(self, session: Session, story_id: int) -> list[Article]:
        """The story's coverage sources: feed + enrichment members (context links are
        background, never coverage)."""
        return list(
            session.scalars(
                select(Article)
                .where(Article.story_id == story_id, Article.origin != "context")
                .order_by(Article.published_at, Article.id)
            )
        )

    def _claim(self, session: Session, story_id: int, story: Story | None = None) -> str:
        """The story's central claim for stance judgements: the brief's lead, else the
        title."""
        row = session.scalar(
            select(StorySummary)
            .where(
                StorySummary.story_id == story_id,
                StorySummary.kind == "brief",
                StorySummary.status.in_([SummaryStatus.OK, SummaryStatus.FALLBACK]),
            )
            .order_by(StorySummary.id.desc())
            .limit(1)
        )
        if row is not None:
            content = row.content_json or {}
            if isinstance(content.get("lead"), str) and content["lead"].strip():
                return " ".join(content["lead"].split())
            if row.text.strip():
                return " ".join(row.text.split())[:300]
        story = story or session.get(Story, story_id)
        return (story.title if story else "") or ""

    def _key(self, session: Session, story_id: int, members: list[Article]) -> str:
        """The synthesis cache key without touching the model: member ids, the hash of
        each member's digest input, the claim, prompt version and model."""
        claim = self._claim(session, story_id)
        return _sha(
            {
                "kind": KIND,
                "members": [
                    [a.id, hashlib.sha256(_basis(a)[1].encode()).hexdigest()[:16]] for a in members
                ],
                "claim": claim,
                "prompt_version": PERSPECTIVES_PROMPT_VERSION,
                "model": self.model,
            }
        )

    # --- digests --------------------------------------------------------------------------

    def stored_digest(self, session: Session, article: Article, claim: str) -> ArticleDigest | None:
        key = _digest_key(article, claim, self.model)
        return session.scalar(
            select(ArticleDigest).where(
                ArticleDigest.article_id == article.id, ArticleDigest.input_hash == key
            )
        )

    async def _digest(
        self,
        session: Session,
        article: Article,
        claim: str,
        names: dict,
        *,
        dry_run: bool = False,
    ) -> tuple[dict | None, str | None]:
        """The digest for one member; ``(None, reason)`` when dropped. Cached by content
        hash: a stored ``ok`` row for the same input is reused."""
        basis, text = _basis(article)
        key = _digest_key(article, claim, self.model)
        row = session.scalar(
            select(ArticleDigest).where(
                ArticleDigest.article_id == article.id, ArticleDigest.input_hash == key
            )
        )
        if row is not None and row.status == "ok":
            return dict(row.digest_json), None
        if basis == "title":
            digest = title_digest(article.title or "")
            if not dry_run:
                self._store_digest(session, article, key, digest, basis, "ok")
            return digest, None
        if self.client is None:
            return None, "no perspectives LLM role"
        name = names.get(article.source_id, article.source_id)
        publisher = publisher_of(article.url, name)
        source = SourceDoc(
            index=1,
            article_id=article.id,
            source=name,
            headline=" ".join((article.title or "").split()),
            url=article.url,
            text=" ".join(text.split()[: self.options.digest_max_words]),
            full_text=basis == "full_text",
            publisher=publisher,
            via=via_of(article.source_id, name, publisher, getattr(names, "aggregators", ())),
        )
        previous, error = None, None
        for _try in range(1, TRIES + 1):
            messages = digest_messages(source, claim, previous=previous, error=error)
            result = await self.client.chat(
                messages,
                purpose="perspective_digest",
                article_id=article.id,
                attempt=_try,
                json_schema=DIGEST_SCHEMA,
                schema_name="digest",
            )
            try:
                digest = validate_digest(result.content, text)
            except SummaryInvalid as exc:
                telemetry.update_llm_call(
                    result.call_ref, status="invalid", validation_error=str(exc)
                )
                previous, error = result.content, str(exc)
                continue
            if not dry_run:
                self._store_digest(session, article, key, digest, basis, "ok")
            return digest, None
        if not dry_run:
            self._store_digest(session, article, key, {}, basis, "invalid", reason=error)
        return None, error

    def _store_digest(
        self,
        session: Session,
        article: Article,
        key: str,
        digest: dict,
        basis: str,
        status: str,
        *,
        reason: str | None = None,
    ) -> None:
        session.add(
            ArticleDigest(
                article_id=article.id,
                story_id=article.story_id,
                input_hash=key,
                digest_json=digest,
                status=status,
                reason=reason,
                basis=basis,
                model=self.model,
                prompt_version=DIGEST_PROMPT_VERSION,
            )
        )

    # --- synthesis -------------------------------------------------------------------------

    async def run_story(
        self,
        session_factory: sessionmaker | None = None,
        story_id: int = 0,
        *,
        dry_run: bool = False,
    ) -> PerspectiveOutcome:
        """Digests for every member, then the synthesis; writes nothing on ``dry_run``."""
        factory = session_factory or self.session_factory
        with factory() as session, telemetry.story_context(story_id):
            story = session.get(Story, story_id)
            if story is None or story.state != StoryState.PUBLISHED.value:
                return PerspectiveOutcome(story_id, "skipped", "not published")
            members = self._members(session, story_id)
            claim = self._claim(session, story_id, story)
            from app.profiles import source_names

            names = source_names(session)
            prepared: list[_Prepared] = []
            errors: dict[int, str] = {}
            for article in members[: self.options.max_articles]:
                _b, text = _basis(article)
                digest, error = await self._digest(session, article, claim, names, dry_run=dry_run)
                if digest is None:
                    if error:
                        errors[article.id] = error
                    continue
                name = names.get(article.source_id, article.source_id)
                publisher = publisher_of(article.url, name)
                label = f"{name} ({publisher})" if publisher and publisher != name else name
                prepared.append(_Prepared(article, digest, label, text))
            if len(prepared) < self.options.min_sources:
                if not dry_run:
                    self._store_summary(
                        session,
                        story,
                        {},
                        members,
                        prepared,
                        status=SummaryStatus.SKIPPED,
                        reason="few_digests",
                    )
                return PerspectiveOutcome(
                    story_id,
                    "skipped",
                    f"only {len(prepared)} digests (<{self.options.min_sources})",
                    digests=len(prepared),
                )
            key = self._key(session, story_id, members)
            hit = session.scalar(
                select(StorySummary).where(
                    StorySummary.story_id == story_id,
                    StorySummary.kind == KIND,
                    StorySummary.input_hash == key,
                    StorySummary.status == SummaryStatus.OK,
                )
            )
            if hit is not None:
                return PerspectiveOutcome(
                    story_id, "cached", digests=len(prepared), detail=hit.content_json
                )
            synthesis, error = await self._synthesize(story.title or "", claim, prepared)
            if synthesis is None:
                if not dry_run:
                    self._store_summary(
                        session,
                        story,
                        {},
                        members,
                        prepared,
                        status=SummaryStatus.FAILED,
                        reason=f"invalid: {error}" if error else "invalid",
                    )
                return PerspectiveOutcome(
                    story_id,
                    "failed",
                    f"invalid: {error}" if error else "invalid",
                    digests=len(prepared),
                )
            if not dry_run:
                self._store_summary(session, story, synthesis, members, prepared)
            detail = {
                "synthesis": synthesis,
                "digests": [
                    {"article_id": p.article.id, "label": p.label, **p.digest} for p in prepared
                ],
                "errors": {str(k): v for k, v in errors.items()},
            }
            return PerspectiveOutcome(story_id, "generated", digests=len(prepared), detail=detail)

    async def _synthesize(
        self, title: str, claim: str, prepared: list[_Prepared]
    ) -> tuple[dict | None, str | None]:
        digests = [{"digest": p.digest, "label": p.label} for p in prepared]
        texts = [p.text for p in prepared]
        guard = VerbatimGuard()
        previous, error = None, None
        for _try in range(1, TRIES + 1):
            messages = synthesis_messages(title, claim, digests, previous=previous, error=error)
            result = await self.client.chat(
                messages,
                purpose="perspective_synthesis",
                attempt=_try,
                json_schema=SYNTH_SCHEMA,
                schema_name="perspectives",
            )
            try:
                synthesis = validate_synthesis(
                    result.content, digests, texts, title=title, guard=guard
                )
            except SummaryInvalid as exc:
                telemetry.update_llm_call(
                    result.call_ref, status="invalid", validation_error=str(exc)
                )
                previous, error = result.content, str(exc)
                continue
            return synthesis, None
        return None, error

    def _store_summary(
        self,
        session: Session,
        story: Story,
        synthesis: dict,
        members: list[Article],
        prepared: list[_Prepared],
        *,
        status: str = SummaryStatus.OK,
        reason: str | None = None,
    ) -> None:
        version = (
            session.scalar(
                select(func.max(StorySummary.version)).where(StorySummary.story_id == story.id)
            )
            or 0
        ) + 1
        session.add(
            StorySummary(
                story_id=story.id,
                version=version,
                kind=KIND,
                text="",
                content_json=synthesis or {},
                article_ids=[p.article.id for p in prepared],
                basis="digests",
                model=self.model,
                prompt_version=PERSPECTIVES_PROMPT_VERSION,
                input_hash=self._key(session, story.id, members),
                status=status,
                reason=reason,
            )
        )
        session.commit()

    def latest(self, session: Session, story_id: int) -> StorySummary | None:
        """The newest perspectives row for the story (the route reads this)."""
        return session.scalar(
            select(StorySummary)
            .where(StorySummary.story_id == story_id, StorySummary.kind == KIND)
            .order_by(StorySummary.id.desc())
            .limit(1)
        )

    def stances(self, session: Session, story_id: int) -> dict[int, dict]:
        """``{article_id: {stance, kind, framing}}`` from stored digests, for the
        coverage badges."""
        story = session.get(Story, story_id)
        if story is None:
            return {}
        claim = self._claim(session, story_id, story)
        out = {}
        for article in self._members(session, story_id):
            key = _digest_key(article, claim, self.model)
            row = session.scalar(
                select(ArticleDigest).where(
                    ArticleDigest.article_id == article.id,
                    ArticleDigest.input_hash == key,
                    ArticleDigest.status == "ok",
                )
            )
            if row is not None:
                out[article.id] = {
                    "stance": row.digest_json.get("stance"),
                    "kind": row.digest_json.get("kind"),
                    "framing": row.digest_json.get("framing"),
                }
        return out


def perspectives_defaults(config) -> dict[str, bool]:
    """``{profile_id: perspectives.enabled}`` from the YAML."""
    return {p.id: p.perspectives.enabled for p in config.profiles}
