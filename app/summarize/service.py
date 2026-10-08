"""Generate, validate and store one story's summary."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import profiles as repo
from app.llm import LLMClient, LLMError
from app.models import StorySummary, SummaryStatus, utcnow
from app.summarize.prompt import PROMPT_VERSION, RESPONSE_SCHEMA, SourceDoc, build_messages
from app.summarize.sources import basis, input_hash, select_sources
from app.summarize.validate import SummaryInvalid, ValidSummary, validate_summary
from app.web import snippet

logger = logging.getLogger(__name__)

Status = Literal["ok", "fallback", "failed"]


@dataclass
class Generation:
    """Result of asking the model (no DB involved): used by the worker, smoke and compare."""

    status: Status
    summary: ValidSummary | None = None
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


class SummaryOutcome(BaseModel):
    story_id: int
    status: Literal["ok", "fallback", "failed", "cached", "reused", "skipped"]
    """``cached``: input unchanged, no model call. ``reused``: an identical input was already
    summarized for another story id (e.g. before a re-cluster), copied without a model call."""
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
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.client = client
        self.max_articles = max_articles
        self.max_words = max_words
        self.clock = clock

    async def generate(self, sources: Sequence[SourceDoc]) -> Generation:
        """Ask the model; on a validation error retry once with the error, then give up
        (``fallback``). Endpoint errors return ``failed`` right away."""
        gen = Generation(status="fallback", model=self.client.model)
        grounding = [s.grounding for s in sources]
        previous = error = None
        for attempt in (1, 2):
            gen.attempts = attempt
            try:
                result = await self.client.chat(
                    build_messages(sources, previous=previous, error=error),
                    json_schema=RESPONSE_SCHEMA,
                    schema_name="story_summary",
                )
            except LLMError as exc:
                gen.status, gen.reason = "failed", f"{exc.kind}: {exc}"
                return gen
            gen.model = result.model or gen.model
            gen.latency_ms += result.latency_ms
            gen.prompt_tokens += result.prompt_tokens or 0
            gen.completion_tokens += result.completion_tokens or 0
            gen.raw_responses.append(result.content)
            try:
                gen.summary = validate_summary(result.content, grounding)
            except SummaryInvalid as exc:
                error = str(exc)
                if not result.content and result.finish_reason:
                    error += f" (finish_reason={result.finish_reason})"
                gen.errors.append(error)
                previous = result.content
                continue
            gen.status, gen.reason = "ok", None
            return gen
        gen.reason = "; ".join(f"attempt {i}: {e}" for i, e in enumerate(gen.errors, 1))
        return gen

    async def summarize_story(
        self, session_factory: sessionmaker, story_id: int, *, force: bool = False
    ) -> SummaryOutcome:
        with session_factory() as session:
            item = repo.get_story(session, story_id)
            if item is None:
                return SummaryOutcome(story_id=story_id, status="skipped", reason="no members")
            names = repo.source_names(session)
            sources = select_sources(
                item.members, names, max_articles=self.max_articles, max_words=self.max_words
            )
            key = input_hash(
                [m.id for m in item.members], sources, PROMPT_VERSION, self.client.model
            )
            fallback_text = snippet(item.snippet_source) or item.title
            hit = None if force else self._cache_hit(session, key)
        if hit is not None:
            if hit.story_id == story_id:
                return SummaryOutcome(story_id=story_id, status="cached", summary_id=hit.id)
            copy = _copy(hit)
            self._save(session_factory, story_id, copy)
            return SummaryOutcome(story_id=story_id, status="reused", summary_id=copy.id)

        gen = await self.generate(sources)
        row = StorySummary(
            text=gen.summary.summary if gen.summary else fallback_text,
            citations_json=gen.summary.citations if gen.summary else [],
            article_ids=[s.article_id for s in sources],
            basis=basis(sources) if gen.summary else "feed_summary",
            model=gen.model,
            prompt_version=PROMPT_VERSION,
            input_hash=key,
            status=SummaryStatus(gen.status).value,
            reason=gen.reason,
            latency_ms=gen.latency_ms,
            prompt_tokens=gen.prompt_tokens or None,
            completion_tokens=gen.completion_tokens or None,
        )
        self._save(session_factory, story_id, row)
        if gen.status != "ok":
            logger.info("story %s summary %s: %s", story_id, gen.status, gen.reason)
        return SummaryOutcome(
            story_id=story_id,
            status=gen.status,
            reason=gen.reason,
            summary_id=row.id,
            attempts=gen.attempts,
            latency_ms=gen.latency_ms,
            completion_tokens=gen.completion_tokens,
        )

    def _cache_hit(self, session, key: str) -> StorySummary | None:
        stmt = (
            select(StorySummary)
            .where(StorySummary.input_hash == key, StorySummary.status != SummaryStatus.FAILED)
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


_COPIED = ("text", "citations_json", "article_ids", "basis", "model", "prompt_version")


def _copy(hit: StorySummary) -> StorySummary:
    note = f"reused summary #{hit.id}" + (f" ({hit.reason})" if hit.reason else "")
    return StorySummary(
        **{c: getattr(hit, c) for c in _COPIED},
        input_hash=hit.input_hash,
        status=hit.status,
        reason=note,
    )
