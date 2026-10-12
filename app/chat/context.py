"""The chat context pack: the story's numbered sources (``story_context``, so [n] matches
the brief's numbering), its published brief and report, and the last turns, trimmed to the
chat role's context window."""

from dataclasses import replace

from sqlalchemy.orm import Session

from app import story_views
from app.chat.prompt import CHAT_SYSTEM_PROMPT, ChatContext, estimate_tokens
from app.config import LLMRole, SummariesConfig
from app.summarize.context import story_context

MAX_ARTICLES = 5
PROMPT_OVERHEAD_TOKENS = 400
"""Tags, headers and chat template tokens around the context, roughly."""


def _brief_text(story: story_views.PublishedStory | None) -> str | None:
    brief = story.brief if story is not None else None
    if brief is None or brief.style == "snippet":
        return None
    return brief.marked_text


def _report_text(story: story_views.PublishedStory | None) -> str | None:
    report = story.report if story is not None else None
    if report is None or report.state != "ready" or not report.paragraphs:
        return None
    return "\n\n".join(p.marked for p in report.paragraphs)


def fit(ctx: ChatContext, question: str, role: LLMRole) -> ChatContext:
    """Drop the oldest turns (keeping at least one), then the report, then cut every source
    proportionally until prompt + answer fit ``role.context_tokens``."""
    budget = role.context_tokens - role.max_tokens - PROMPT_OVERHEAD_TOKENS
    budget -= estimate_tokens([CHAT_SYSTEM_PROMPT, question, ctx.title])

    def extra() -> int:
        texts = [ctx.brief or "", ctx.report or ""]
        texts += [t for turn in ctx.history for t in turn]
        texts += [f"{s.label} {s.headline} {s.url}" for s in ctx.sources]
        return estimate_tokens(texts)

    def total() -> int:
        return extra() + estimate_tokens([s.text for s in ctx.sources])

    while total() > budget and len(ctx.history) > 1:
        ctx.history.pop(0)
        ctx.dropped_turns += 1
    if total() > budget and ctx.report:
        ctx.report = None
    if total() > budget:
        room = max(budget - extra(), 0)
        words = [len(s.text.split()) for s in ctx.sources]
        ratio = room / max(estimate_tokens([s.text for s in ctx.sources]), 1)
        ctx.sources = [
            replace(s, text=" ".join(s.text.split()[: max(20, int(n * ratio))]))
            for s, n in zip(ctx.sources, words, strict=True)
        ]
    return ctx


def chat_context(
    session: Session,
    story_id: int,
    question: str,
    *,
    role: LLMRole,
    options: SummariesConfig,
    history: list[tuple[str, str]] | None = None,
) -> ChatContext | None:
    base = story_context(
        session, story_id, max_articles=MAX_ARTICLES, total_words=options.chat_context_words
    )
    if base is None:
        return None
    published = story_views.published_story(session, story_id)
    turns = list(history or [])
    turns = turns[-options.chat_history_turns :] if options.chat_history_turns else []
    ctx = ChatContext(
        story_id=story_id,
        title=base.title,
        sources=base.sources,
        brief=_brief_text(published),
        report=_report_text(published),
        history=turns,
    )
    return fit(ctx, question, role)
