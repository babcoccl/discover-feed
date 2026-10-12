"""Generate, validate and store a story's brief (cards) and report (story page)."""

import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app import profiles as repo
from app.config import StructuredOutput, SummariesConfig
from app.llm import ChatResult, LLMBadResponse, LLMClient, LLMError, LLMHTTPError
from app.models import (
    GenerationAttempt,
    StorySummary,
    SummaryKind,
    SummaryStatus,
    TextStatus,
    utcnow,
)
from app.summarize.prompt import (
    PROMPT_VERSIONS,
    PROMPTS,
    VERBATIM_SHINGLE,
    SourceDoc,
    build_messages,
)
from app.summarize.sources import basis, input_hash, select_sources
from app.summarize.validate import (
    SummaryInvalid,
    ValidBrief,
    ValidReport,
    ValidSummary,
    VerbatimGuard,
    validate_brief,
    validate_report,
    validate_summary,
)
from app.web import snippet
from app.worker_status import SummaryOutcome

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
    attempt_log: list[dict[str, Any]] = field(default_factory=list)
    """One entry per request (``GenerationAttempt`` columns minus story/members)."""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)

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


RAW_CONTENT_CHARS = 2000
ATTEMPTS_KEPT = 500
PROMPT_OVERHEAD_TOKENS = 700
"""System prompt, schema and per-source headers, roughly."""


def estimate_tokens(words: int) -> int:
    return round(words * 1.35)


def error_reason(exc: LLMError) -> str:
    """``timeout`` | ``http_<status>`` | ``connection`` | ``invalid_json`` | ``error``."""
    if isinstance(exc, LLMHTTPError):
        return f"http_{exc.status_code}"
    if isinstance(exc, LLMBadResponse):
        return "invalid_json"
    return exc.kind


def content_reason(result: ChatResult, error: str) -> str:
    """Why an answer was rejected: ``empty_content``, ``truncated``,
    ``reasoning_consumed_budget``, ``invalid_json`` or ``validation:<rule>``."""
    if result.finish_reason == "length":
        if not result.content and result.reasoning_length:
            return "reasoning_consumed_budget"
        return "truncated"
    if not result.content:
        return "reasoning_consumed_budget" if result.reasoning_length else "empty_content"
    if "not a valid JSON object" in error:
        return "invalid_json"
    low = error.lower()
    for needle, rule in _VALIDATION_RULES:
        if needle in low:
            return f"validation:{rule}"
    rule = re.sub(r"[^a-z0-9]+", "_", low.split(":")[0]).strip("_")[:48]
    return f"validation:{rule or 'invalid'}"


_VALIDATION_RULES = (
    ("not found in its cited sources", "invented_number"),
    ("phrases appear in the sources", "verbatim_ratio"),
    ("word for word", "verbatim_overlap"),
    ("valid source indexes", "citation_range"),
    ("cite", "citation"),
    ("duplicate", "duplicate"),
    ("repeats", "duplicate"),
    ("words", "length"),
    ("bullet", "bullet_count"),
    ("paragraph", "paragraph_count"),
    ("sentence", "lead_sentence"),
)


def trim_sources(
    sources: Sequence[SourceDoc], *, context_tokens: int, max_tokens: int
) -> list[SourceDoc]:
    """Cut every source's text proportionally so prompt + answer fit the context window."""
    budget = context_tokens - max_tokens - PROMPT_OVERHEAD_TOKENS
    words = [len(s.text.split()) for s in sources]
    total = estimate_tokens(sum(words))
    if total <= budget or not total:
        return list(sources)
    ratio = max(budget, 0) / total
    return [
        replace(s, text=" ".join(s.text.split()[: max(20, int(n * ratio))]))
        for s, n in zip(sources, words, strict=True)
    ]


def _attempt_reason(errors: Sequence[str]) -> str:
    return "; ".join(f"attempt {i}: {e}" for i, e in enumerate(errors, 1))


def summarizer_options(o: SummariesConfig) -> dict[str, Any]:
    """:class:`Summarizer` keyword arguments from a profile's ``summaries:`` section."""
    return {
        "report_max_articles": o.report_max_articles,
        "report_total_words": o.report_total_words,
        "report_max_tokens": o.report_max_tokens,
        "report_timeout_seconds": o.report_timeout_seconds,
        "duplicate_threshold": o.duplicate_threshold,
        "verbatim": VerbatimGuard(
            max_run=o.verbatim_max_run,
            max_ratio=o.verbatim_max_ratio,
            shingle=VERBATIM_SHINGLE,
            allow=tuple(o.verbatim_allow),
        ),
        "retry_temperature_bump": o.retry_temperature_bump,
    }


class Summarizer:
    def __init__(
        self,
        client: LLMClient,
        *,
        max_articles: int = 5,
        max_words: int = 1200,
        report_max_articles: int = 4,
        report_total_words: int = 2400,
        report_max_tokens: int | None = None,
        report_timeout_seconds: float | None = None,
        duplicate_threshold: float = 0.6,
        verbatim: VerbatimGuard | None = None,
        retry_temperature_bump: float = 0.2,
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
        self.report_total_words = report_total_words
        self.duplicate_threshold = duplicate_threshold
        self.verbatim = verbatim or VerbatimGuard()
        self.retry_temperature_bump = retry_temperature_bump
        self.raw_content_chars: int | None = RAW_CONTENT_CHARS
        """Raw answer kept per attempt (``None``: all of it, for ``diagnose --full``)."""
        self.clock = clock
        self.on_save: Callable[[Session, int], None] | None = None
        """Called with the saving session before commit (the worker rebuilds the view)."""

    # --- generation (no DB) ----------------------------------------------------------------

    def _retry_client(self, client: LLMClient) -> LLMClient:
        """``client`` with the temperature raised by ``retry_temperature_bump``."""
        if not self.retry_temperature_bump:
            return client
        temperature = min(2.0, client.role.temperature + self.retry_temperature_bump)
        role = client.role.model_copy(update={"temperature": round(temperature, 3)})
        return LLMClient(role, transport=client.transport)

    async def _ask(
        self,
        client: LLMClient,
        kind: str,
        sources: Sequence[SourceDoc],
        check: Callable[[str], Any],
        gen: Generation,
    ) -> tuple[Any | None, list[str]]:
        """Two attempts (the retry gets the rejected answer and the validation error), then
        for briefs and reports one last ``structured_output: json_object`` attempt (JSON is
        extracted leniently; every validation rule still applies). Retries quote the copied
        phrases and run ``retry_temperature_bump`` hotter. Raises :class:`LLMError` on endpoint
        errors."""
        _system, schema, _ask = PROMPTS[kind]
        previous = error = None
        phrases: tuple[str, ...] = ()
        errors: list[str] = []
        tries = (1, 2, 3) if kind in KINDS else (1, 2)
        retry_client = self._retry_client(client)
        for attempt in tries:
            final = attempt == 3
            gen.attempts += 1
            messages = build_messages(
                sources, previous=previous, error=error, kind=kind, phrases=phrases
            )
            words = sum(len(str(m["content"]).split()) for m in messages)
            entry: dict[str, Any] = {
                "kind": kind if kind in KINDS else "brief",
                "prompt": kind,
                "attempt": attempt,
                "run_id": gen.run_id,
                "model": client.model,
                "endpoint_host": client.endpoint_host,
                "input_words": words,
                "estimated_tokens": estimate_tokens(words),
                "structured_output": "json_object" if final else client.role.structured_output,
                "temperature": (client if attempt == 1 else retry_client).role.temperature,
            }
            gen.attempt_log.append(entry)
            try:
                result = await (client if attempt == 1 else retry_client).chat(
                    messages,
                    json_schema=schema,
                    schema_name=f"story_{kind}",
                    structured_output=StructuredOutput.JSON_OBJECT if final else None,
                )
            except LLMError as exc:
                entry.update(outcome="failed", reason=error_reason(exc))
                raise
            gen.add(result)
            entry.update(
                model=result.model or client.model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                finish_reason=result.finish_reason,
                latency_ms=result.latency_ms,
                content_length=len(result.content),
                reasoning_length=result.reasoning_length,
                raw_content=result.content[: self.raw_content_chars],
                tokens_per_second=result.tokens_per_second,
            )
            try:
                value = check(result.content)
            except SummaryInvalid as exc:
                error = str(exc)
                if not result.content and result.finish_reason:
                    error += f" (finish_reason={result.finish_reason})"
                errors.append(error)
                entry.update(
                    outcome="failed",
                    reason=content_reason(result, str(exc)),
                    validation_errors=[error],
                )
                previous, phrases = result.content, exc.phrases
                continue
            entry.update(
                outcome="ok",
                reason=None,
                validation_errors=[],
                warnings=list(getattr(value, "warnings", ())),
            )
            return value, errors
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
                    c,
                    grounding,
                    duplicate_threshold=self.duplicate_threshold,
                    allow=self.verbatim.allow,
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

    async def generate_report(self, sources: Sequence[SourceDoc], title: str = "") -> Generation:
        """A report; after three invalid answers ``failed`` with an ``invalid:`` reason (no
        lesser substitute is shown). ``title`` (the story's) words may be copied freely."""
        gen = Generation(status="failed", kind="report", model=self.client.model)
        grounding = [s.grounding for s in sources]
        try:
            gen.report, gen.errors = await self._ask(
                self.report_client,
                "report",
                sources,
                lambda c: validate_report(
                    c,
                    grounding,
                    duplicate_threshold=self.duplicate_threshold,
                    guard=self.verbatim,
                    title=title,
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
            role = self.report_client.role
            return trim_sources(
                select_sources(
                    item.members,
                    names,
                    max_articles=self.report_max_articles,
                    total_words=self.report_total_words,
                    numbered_first=self.max_articles,
                ),
                context_tokens=role.context_tokens,
                max_tokens=role.max_tokens,
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
        """No included member has extracted text (``text_status=ok``) of 150+ words."""
        return not report_eligibility(item, sources)

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
        members = [
            {
                "article_id": m.id,
                "source": next((s.source for s in sources if s.article_id == m.id), m.source_id),
                "text_status": m.text_status,
                "words": m.word_count or 0,
                "included": any(s.article_id == m.id for s in sources),
            }
            for m in item.members
        ]
        if insufficient:
            row.status, row.reason = SummaryStatus.SKIPPED.value, INSUFFICIENT_TEXT
            skipped = {
                "kind": kind,
                "model": self.client.model,
                "endpoint_host": self.client.endpoint_host,
                "outcome": "skipped",
                "reason": INSUFFICIENT_TEXT,
            }
            self._save(session_factory, story_id, row, [skipped], members)
            return SummaryOutcome(
                story_id=story_id,
                kind=kind,
                status="skipped",
                reason=INSUFFICIENT_TEXT,
                summary_id=row.id,
            )

        if kind == SummaryKind.REPORT:
            gen = await self.generate_report(sources, item.title)
        else:
            gen = await self.generate_brief(sources)
        _fill(row, gen, fallback_text)
        self._save(session_factory, story_id, row, gen.attempt_log, members)
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

    def _save(
        self,
        session_factory: sessionmaker,
        story_id: int,
        row: StorySummary,
        attempts: Sequence[dict[str, Any]] = (),
        members: Sequence[dict[str, Any]] = (),
    ) -> None:
        with session_factory() as session:
            if attempts:
                record_attempts(session, story_id, attempts, members)
            latest = session.scalar(
                select(func.max(StorySummary.version)).where(StorySummary.story_id == story_id)
            )
            row.story_id = story_id
            row.version = (latest or 0) + 1
            row.created_at = self.clock()
            session.add(row)
            if self.on_save is not None:  # the story's view, in the same transaction
                session.flush()
                self.on_save(session, story_id)
            session.commit()


_ATTEMPT_COLUMNS = {c.name for c in GenerationAttempt.__table__.columns} - {"id", "story_id"}


def record_attempts(
    session: Session,
    story_id: int | None,
    attempts: Sequence[dict[str, Any]],
    members: Sequence[dict[str, Any]] = (),
) -> None:
    """Store attempt rows and keep only the newest ``ATTEMPTS_KEPT``."""
    for entry in attempts:
        values = {k: v for k, v in entry.items() if k in _ATTEMPT_COLUMNS}
        session.add(GenerationAttempt(story_id=story_id, members=list(members), **values))
    session.flush()
    newest = session.scalar(select(func.max(GenerationAttempt.id))) or 0
    session.execute(delete(GenerationAttempt).where(GenerationAttempt.id <= newest - ATTEMPTS_KEPT))


def report_eligibility(item: repo.StoryItem, sources: Sequence[SourceDoc]) -> bool:
    """A report needs at least one included member with extracted text of 150+ words
    (decided without the LLM)."""
    included = {s.article_id for s in sources}
    return any(
        m.id in included
        and m.text_status == TextStatus.OK
        and (m.word_count or 0) >= REPORT_MIN_SOURCE_WORDS
        for m in item.members
    )


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
