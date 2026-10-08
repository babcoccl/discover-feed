"""Read side: a story's latest ``ok`` summary with its numbered sources, for the API and UI."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, StorySummary, SummaryStatus
from app.summarize.validate import split_sentences


@dataclass(frozen=True)
class Citation:
    index: int
    article_id: int
    source: str
    headline: str
    url: str
    """The article's raw feed permalink (``Article.url``), never ``canonical_url``."""


@dataclass(frozen=True)
class Sentence:
    text: str
    citations: list[Citation]


@dataclass(frozen=True)
class SummaryView:
    id: int
    text: str
    sentences: list[Sentence]
    citations: list[Citation]
    """Every numbered source, [1] first."""
    basis: str
    model: str
    generated_at: datetime

    @property
    def marked_text(self) -> str:
        """``Sentence one. [1] Sentence two. [1][2]``"""
        return " ".join(
            s.text + " " + "".join(f"[{c.index}]" for c in s.citations) for s in self.sentences
        )


def latest_ok(session: Session, story_ids: Iterable[int]) -> dict[int, StorySummary]:
    ids = sorted({i for i in story_ids if i})
    if not ids:
        return {}
    latest = (
        select(func.max(StorySummary.id))
        .where(StorySummary.story_id.in_(ids), StorySummary.status == SummaryStatus.OK)
        .group_by(StorySummary.story_id)
    )
    rows = session.scalars(select(StorySummary).where(StorySummary.id.in_(latest)))
    return {row.story_id: row for row in rows}


def build_view(
    row: StorySummary, articles: dict[int, Article], names: dict[str, str]
) -> SummaryView | None:
    citations = []
    for index, article_id in enumerate(row.article_ids or [], 1):
        article = articles.get(article_id)
        if article is None:
            return None
        citations.append(
            Citation(
                index=index,
                article_id=article.id,
                source=names.get(article.source_id, article.source_id),
                headline=article.title or article.url,
                url=article.url,
            )
        )
    texts = split_sentences(row.text)
    cited = row.citations_json or []
    if len(texts) != len(cited):  # unreachable for validated rows; show it as one sentence
        texts, cited = [row.text], [sorted({i for c in cited for i in c})]
    sentences = [
        Sentence(text=t, citations=[citations[i - 1] for i in c if 1 <= i <= len(citations)])
        for t, c in zip(texts, cited, strict=True)
    ]
    return SummaryView(
        id=row.id,
        text=row.text,
        sentences=sentences,
        citations=citations,
        basis=row.basis,
        model=row.model,
        generated_at=row.created_at,
    )


def summary_views(
    session: Session, story_ids: Iterable[int], names: dict[str, str]
) -> dict[int, SummaryView]:
    """Latest ``ok`` summary per story (stories without one are missing from the result)."""
    rows = latest_ok(session, story_ids)
    article_ids = {i for row in rows.values() for i in row.article_ids or []}
    articles = (
        {a.id: a for a in session.scalars(select(Article).where(Article.id.in_(article_ids)))}
        if article_ids
        else {}
    )
    views = {}
    for story_id, row in rows.items():
        view = build_view(row, articles, names)
        if view is not None:
            views[story_id] = view
    return views
