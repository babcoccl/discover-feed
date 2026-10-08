"""Prompt-ready bundle of one story for grounded Q&A (next phase: POST /api/stories/{id}/ask).

Sources keep the numbering of the story's latest summary, so an answer's [n] means the same
article as the summary's [n]; members added since then are numbered after them.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import profiles as repo
from app.models import Article
from app.summarize.prompt import SourceDoc, format_sources
from app.summarize.sources import select_sources, source_doc
from app.summarize.views import SummaryView, summary_views


@dataclass(frozen=True)
class StoryContext:
    story_id: int
    title: str
    sources: list[SourceDoc]
    summary: SummaryView | None

    def prompt(self) -> str:
        parts = [f"Story: {self.title}", f"Sources:\n\n{format_sources(self.sources)}"]
        if self.summary is not None:
            parts.append(f"Current summary: {self.summary.marked_text}")
        return "\n\n".join(parts)


def story_context(
    session: Session, story_id: int, *, max_articles: int = 5, max_words: int = 1200
) -> StoryContext | None:
    item = repo.get_story(session, story_id)
    if item is None:
        return None
    names = repo.source_names(session)
    summary = summary_views(session, [story_id], names).get(story_id)
    if summary is None:
        sources = select_sources(
            item.members, names, max_articles=max_articles, max_words=max_words
        )
        return StoryContext(story_id, item.title, sources, None)
    numbered = [c.article_id for c in summary.citations]
    by_id = {a.id: a for a in session.scalars(select(Article).where(Article.id.in_(numbered)))}
    ordered = [by_id[i] for i in numbered]
    ordered += [m for m in item.members if m.id not in set(numbered)]
    sources = [source_doc(i, a, names, max_words) for i, a in enumerate(ordered, 1)]
    return StoryContext(story_id, item.title, sources, summary)
