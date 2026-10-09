"""Generate, validate and store a story's brief (cards) and report (story page)."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import sessionmaker

from app import profiles as repo
from app.llm import ChatResult, LLMClient, LLMError
from app.models import StorySummary, SummaryKind, SummaryStatus, utcnow
from app.summarize.prompt import (
    PROMPT_VERSIONS,
    PROMPTS,
    SourceDoc,
    build_messages,
)
from app.summarize.sources import basis, input_hash, select_sources
from app.summarize.validate import (
    SummaryInvalid,
    ValidBrief,
    ValidReport,
    ValidSummary,
    validate_brief,
    validate_report,
    validate_summary,
)
from app.web import snippet

logger = logging.getLogger(__name__)

Status = Literal["ok", "fallback", "failed", "skipped"]
Kind = Literal["brief", "report"]
KINDS: tuple[Kind, ...] = ("brief", "report")

INSUFFICIENT_TEXT = "insufficient_text"
REPORT_MIN_SOURCE_WORDS = 150
"""A single-member story needs at least this much extracted text for a report."""
INVALID = "invalid: "
"""Reason prefix of a report that failed validation twice (cached: same input, same answer)."""


@dataclass
class Generation:
    """Result of asking the model (no DB involved): used by the worker, smoke and compare."""

    status: Status
    kind: str = "summary"
    summary: ValidSummary | None = None
    brief: ValidBrief | None = None
    report: ValidReport | None = None
    style: str | None = None
    """Brief fallbacks: ``summary`` (Phase 4 style answer) or ``snippet`` (feed snippet)."""
    reason: str | None = None
    attempts: int = 0
    model: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    raw_responses: list[str] = field(default_factory=list)

    @property
    def tokens_per_second(self) -> float | None:
        if not self.completion_tokens or self.latency_ms <= 0:
            return None
        return self.completion_tokens / (self.latency_ms / 1000)

    @property
    def rejected(self) -> bool:
        """A report that failed validation (not an endpoint failure)."""
        return self.status == "failed" and (self.reason or "").startswith(INVALID)

    def add(self, result: ChatResult) -> None:
        self.model = result.model or self.model
        self.latency_ms += result.latency_ms
        self.prompt_tokens += result.prompt_tokens or 0
        self.completion_tokens += result.completion_tokens or 0
        self.raw_responses.append(result.content)


def _attempt_reason(errors: Sequence[str]) -> str:
    return "; ".join(f"attempt {i}: {e}" for i, e in enumerate(errors, 1))


class SummaryOutcome(BaseModel):
    story_id: int
    kind: str = "brief"
    status: Literal["ok", "fallback", "failed", "rejected", "cached", "reused", "skipped"]
    """``cached``: input unchanged, no model call. ``reused``: an identical input was already
    summarized for another story id (e.g. before a re-cluster), copied without a model call.
    ``rejected``: a report failed validation (stored as failed, no endpoint problem)."""
    reason: str | None = None
    summary_id: int | None = None
    attempts: int = 0
    latency_ms: int | None = None
    completion_tokens: int | None = None


class Summarizer:
    def __init__(
        self,
        client: LLMClient,
        *,
        max_articles: int = 5,
        max_words: int = 1200,
        report_max_articles: int = 4,
        report_max_words: int = 600,
        report_max_tokens: int | None = None,
        report_timeout_seconds: float | None = None,
        duplicate_threshold: float = 0.6,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.client = client
        self.report_client = client
        update = {
            k: v
            for k, v in (
                ("max_tokens", report_max_tokens),
                ("timeout_seconds", report_timeout_seconds),
            )
            if v
        }
        if update:
            role = client.role.model_copy(update=update)
            self.report_client = LLMClient(role, transport=client.transport)
        self.max_articles = max_articles
        self.max_words = max_words
        self.report_max_articles = report_max_articles
        self.report_max_words = report_max_words
        self.duplicate_threshold = duplicate_threshold
        self.clock = clock

    # --- generation (no DB) ----------------------------------------------------------------

    async def _ask(
        self,
        client: LLMClient,
        kind: str,
        sources: Sequence[SourceDoc],
        check: Callable[[str], Any],
        gen: Generation,
    ) -> tuple[Any | None, list[str]]:
        """Two attempts; the retry gets the rejected answer and the validation error.
        Raises :class:`LLMError` on endpoint errors."""
        _system, schema, _ask = PROMPTS[kind]
        previous = error = None
        errors: list[str] = []
        for _ in (1, 2):
            gen.attempts += 1
            result = await client.chat(
                build_messages(sources, previous=previous, error=error, kind=kind),
                json_schema=schema,
                schema_name=f"story_{kind}",
            )
            gen.add(result)
            try:
                return check(result.content), errors
            except SummaryInvalid as exc:
                error = str(exc)
                if not result.content and result.finish_reason:
                    error += f" (finish_reason={result.finish_reason})"
                errors.append(error)
                previous = result.content
        return None, errors

    async def generate(self, sources: Sequence[SourceDoc]) -> Generation:
        """The Phase 4 summary: retry once with the error, then ``fallback``. Endpoint errors
        return ``failed`` right away."""
        gen = Generation(status="fallback", model=self.client.model)
        grounding = [s.grounding for s in sources]
        try:
            gen.summary, gen.errors = await self._ask(
                self.client, "summary", sources, lambda c: validate_summary(c, grounding), gen
            )
        except LLMError as exc:
            gen.status, gen.reason = "failed", f"{exc.kind}: {exc}"
            return gen
        if gen.summary is not None:
            gen.status = "ok"
        else:
            gen.reason = _attempt_reason(gen.errors)
        return gen

    async def generate_brief(self, sources: Sequence[SourceDoc]) -> Generation:
        """A brief; after two invalid answers, a Phase 4 summary (``fallback``, style
        ``summary``), then the feed snippet (``fallback``, style ``snippet``)."""
        gen = Generation(status="fallback", kind="brief", model=self.client.model)
        grounding = [s.grounding for s in sources]
        try:
            gen.brief, errors = await self._ask(
                self.client,
                "brief",
                sources,
                lambda c: validate_brief(
                    c, grounding, duplicate_threshold=self.duplicate_threshold
                ),
                gen,
            )
            gen.errors += errors
            if gen.brief is not None:
                gen.status = "ok"
                return gen
            reason = f"brief {_attempt_reason(errors)}"
            gen.summary, errors = await self._ask(
                self.client, "summary", sources, lambda c: validate_summary(c, grounding), gen
            )
            gen.errors += errors
        except LLMError as exc:
            gen.status, gen.reason = "failed", f"{exc.kind}: {exc}"
            return gen
        if gen.summary is not None:
            gen.style, gen.reason = "summary", reason
        else:
            gen.style, gen.reason = "snippet", f"{reason}; summary {_attempt_reason(errors)}"
        return gen

    async def generate_report(self, sources: Sequence[SourceDoc]) -> Generation:
        """A report; after two invalid answers ``failed`` with an ``invalid:`` reason (no
        lesser substitute is shown)."""
        gen = Generation(status="failed", kind="report", model=self.client.model)
        grounding = [s.grounding for s in sources]
        try:
            gen.report, gen.errors = await self._ask(
                self.report_client,
                "report",
                sources,
                lambda c: validate_report(
                    c, grounding, duplicate_threshold=self.duplicate_threshold
                ),
                gen,
            )
        except LLMError as exc:
            gen.reason = f"{exc.kind}: {exc}"
            return gen
        if gen.report is not None:
            gen.status = "ok"
        else:
            gen.reason = INVALID + _attempt_reason(gen.errors)
        return gen

    # --- inputs ----------------------------------------------------------------------------

    def sources_for(
        self, item: repo.StoryItem, names: dict[str, str], kind: str
    ) -> list[SourceDoc]:
        if kind == SummaryKind.REPORT:
            return select_sources(
                item.members,
                names,
                max_articles=self.report_max_articles,
                max_words=self.report_max_words,
                numbered_first=self.max_articles,
            )
        return select_sources(
            item.members, names, max_articles=self.max_articles, max_words=self.max_words
        )

    def key_for(self, item: repo.StoryItem, sources: Sequence[SourceDoc], kind: str) -> str:
        return input_hash(
            [m.id for m in item.members], sources, PROMPT_VERSIONS[kind], self.client.model, kind
        )

    def is_current(self, session, story_id: int, kind: str) -> bool:
        """True when ``kind`` is stored (or cached) for the story's current input."""
        item = repo.get_story(session, story_id)
        if item is None:
            return True
        sources = self.sources_for(item, repo.source_names(session), kind)
        return self._cache_hit(session, self.key_for(item, sources, kind)) is not None

    @staticmethod
    def insufficient_text(item: repo.StoryItem, sources: Sequence[SourceDoc]) -> bool:
        """``feed_summary`` basis: every member failed extraction, or a single member has
        under 150 words of text."""
        if basis(sources) == "feed_summary":
            return True
        return len(item.members) == 1 and len(sources[0].text.split()) < REPORT_MIN_SOURCE_WORDS

    # --- storing ---------------------------------------------------------------------------

    async def summarize_story(
        self,
        session_factory: sessionmaker,
        story_id: int,
        *,
        force: bool = False,
        kind: Kind = "brief",
    ) -> SummaryOutcome:
        with session_factory() as session:
            item = repo.get_story(session, story_id)
            if item is None:
                return SummaryOutcome(
                    story_id=story_id, kind=kind, status="skipped", reason="no members"
                )
            sources = self.sources_for(item, repo.source_names(session), kind)
            key = self.key_for(item, sources, kind)
            fallback_text = snippet(item.snippet_source) or item.title
            insufficient = kind == SummaryKind.REPORT and self.insufficient_text(item, sources)
            hit = None if force and not insufficient else self._cache_hit(session, key)
        if hit is not None:
            if hit.story_id == story_id:
                return SummaryOutcome(
                    story_id=story_id, kind=kind, status="cached", summary_id=hit.id
                )
            copy = _copy(hit)
            self._save(session_factory, story_id, copy)
            return SummaryOutcome(story_id=story_id, kind=kind, status="reused", summary_id=copy.id)

        row = StorySummary(
            kind=kind,
            article_ids=[s.article_id for s in sources],
            basis=basis(sources),
            model=self.client.model,
            prompt_version=PROMPT_VERSIONS[kind],
            input_hash=key,
        )
        if insufficient:
            row.status, row.reason = SummaryStatus.SKIPPED.value, INSUFFICIENT_TEXT
            self._save(session_factory, story_id, row)
            return SummaryOutcome(
                story_id=story_id,
                kind=kind,
                status="skipped",
                reason=INSUFFICIENT_TEXT,
                summary_id=row.id,
            )

        if kind == SummaryKind.REPORT:
            gen = await self.generate_report(sources)
        else:
            gen = await self.generate_brief(sources)
        _fill(row, gen, fallback_text)
        self._save(session_factory, story_id, row)
        if gen.status != "ok":
            logger.info("story %s %s %s: %s", story_id, kind, gen.status, gen.reason)
        return SummaryOutcome(
            story_id=story_id,
            kind=kind,
            status="rejected" if gen.rejected else gen.status,
            reason=gen.reason,
            summary_id=row.id,
            attempts=gen.attempts,
            latency_ms=gen.latency_ms,
            completion_tokens=gen.completion_tokens,
        )

    def _cache_hit(self, session, key: str) -> StorySummary | None:
        stmt = (
            select(StorySummary)
            .where(
                StorySummary.input_hash == key,
                or_(
                    StorySummary.status != SummaryStatus.FAILED,
                    StorySummary.reason.startswith(INVALID),
                ),
            )
            .order_by(StorySummary.id.desc())
            .limit(1)
        )
        return session.scalar(stmt)

    def _save(self, session_factory: sessionmaker, story_id: int, row: StorySummary) -> None:
        with session_factory() as session:
            latest = session.scalar(
                select(func.max(StorySummary.version)).where(StorySummary.story_id == story_id)
            )
            row.story_id = story_id
            row.version = (latest or 0) + 1
            row.created_at = self.clock()
            session.add(row)
            session.commit()


def _fill(row: StorySummary, gen: Generation, fallback_text: str) -> None:
    row.model = gen.model
    row.status = SummaryStatus(gen.status).value
    row.reason = gen.reason
    row.latency_ms = gen.latency_ms
    row.prompt_tokens = gen.prompt_tokens or None
    row.completion_tokens = gen.completion_tokens or None
    if gen.brief is not None:
        row.text = gen.brief.text
        row.citations_json = [gen.brief.lead_citations, *(b.citations for b in gen.brief.bullets)]
        row.content_json = gen.brief.as_json()
    elif gen.report is not None:
        row.text = gen.report.text
        row.citations_json = [p.citations for p in gen.report.paragraphs]
        row.content_json = gen.report.as_json()
    elif gen.summary is not None:
        row.text, row.citations_json = gen.summary.summary, gen.summary.citations
        row.content_json = {"style": "summary"}
    elif gen.kind == SummaryKind.BRIEF:
        row.text, row.citations_json, row.basis = fallback_text, [], "feed_summary"
        row.content_json = {"style": "snippet"}
    else:
        row.text, row.citations_json = "", []


_COPIED = (
    "kind",
    "text",
    "citations_json",
    "content_json",
    "article_ids",
    "basis",
    "model",
    "prompt_version",
)


def _copy(hit: StorySummary) -> StorySummary:
    note = f"reused summary #{hit.id}" + (f" ({hit.reason})" if hit.reason else "")
    if (hit.reason or "").startswith(INVALID) or hit.reason == INSUFFICIENT_TEXT:
        note = hit.reason
    return StorySummary(
        **{c: getattr(hit, c) for c in _COPIED},
        input_hash=hit.input_hash,
        status=hit.status,
        reason=note,
    )
