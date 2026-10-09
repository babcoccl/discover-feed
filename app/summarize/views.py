"""Read side: a story's brief and report with their numbered sources, for the API and UI."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Article, StorySummary, SummaryKind, SummaryStatus
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
class Point:
    """A sentence, bullet or paragraph with the sources it cites."""

    text: str
    citations: list[Citation]

    @property
    def marked(self) -> str:
        return self.text + (" " + "".join(f"[{c.index}]" for c in self.citations)).rstrip()


Sentence = Point


@dataclass(frozen=True)
class BriefView:
    id: int
    lead: list[Point]
    """One sentence for briefs; the sentences of a Phase 4 style summary; or the snippet."""
    bullets: list[Point]
    citations: list[Citation]
    """Every numbered source, [1] first."""
    basis: str
    model: str
    generated_at: datetime
    status: str
    """``ok`` or ``fallback``."""
    style: str
    """``brief``; ``summary`` (Phase 4 style text: a fallback, or a pre-brief row); or
    ``snippet`` (the feed snippet, no citations)."""

    @property
    def lead_text(self) -> str:
        return " ".join(p.text for p in self.lead)

    @property
    def lead_citations(self) -> list[Citation]:
        seen: dict[int, Citation] = {}
        for point in self.lead:
            for c in point.citations:
                seen.setdefault(c.index, c)
        return list(seen.values())

    @property
    def sentences(self) -> list[Point]:
        return self.lead + self.bullets

    @property
    def text(self) -> str:
        return " ".join(p.text for p in self.sentences)

    @property
    def marked_text(self) -> str:
        """``Lead. [1] Bullet one. [1][2] ...``"""
        return " ".join(p.marked for p in self.sentences)


SummaryView = BriefView


@dataclass(frozen=True)
class ReportView:
    id: int
    paragraphs: list[Point]
    """Empty unless ``status`` is ``ok``."""
    citations: list[Citation]
    model: str
    generated_at: datetime
    status: str
    """``ok``, ``failed`` (no report is shown) or ``skipped`` (not enough source text)."""
    reason: str | None

    @property
    def word_count(self) -> int:
        return sum(len(p.text.split()) for p in self.paragraphs)


def _latest(session: Session, story_ids: Iterable[int], kind: str, rank) -> dict:
    ids = sorted({i for i in story_ids if i})
    if not ids:
        return {}
    stmt = select(StorySummary).where(StorySummary.story_id.in_(ids), StorySummary.kind == kind)
    if kind == SummaryKind.BRIEF:
        stmt = stmt.where(StorySummary.status.in_([SummaryStatus.OK, SummaryStatus.FALLBACK]))
    best: dict[int, StorySummary] = {}
    for row in session.scalars(stmt):
        current = best.get(row.story_id)
        if current is None or rank(row) > rank(current):
            best[row.story_id] = row
    return best


def _style(row: StorySummary) -> str:
    content = row.content_json or {}
    if "lead" in content:
        return "brief"
    if content.get("style") == "snippet" or (not content and row.status == SummaryStatus.FALLBACK):
        return "snippet"
    return "summary"


def latest_ok(session: Session, story_ids: Iterable[int]) -> dict[int, StorySummary]:
    """The brief row shown per story: ``ok`` first, then a summary-style fallback, then a
    snippet fallback; newest first within each."""
    return _latest(
        session,
        story_ids,
        SummaryKind.BRIEF,
        lambda r: (r.status == SummaryStatus.OK, _style(r) != "snippet", r.id),
    )


def latest_reports(session: Session, story_ids: Iterable[int]) -> dict[int, StorySummary]:
    """The report row shown per story: the newest ``ok``, else the newest of any status."""
    return _latest(
        session, story_ids, SummaryKind.REPORT, lambda r: (r.status == SummaryStatus.OK, r.id)
    )


def _citations(
    row: StorySummary, articles: dict[int, Article], names: dict[str, str]
) -> list[Citation] | None:
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
    return citations


def _point(text: str, indexes: Iterable[int], citations: list[Citation]) -> Point:
    return Point(text, [citations[i - 1] for i in indexes if 1 <= i <= len(citations)])


def build_view(
    row: StorySummary, articles: dict[int, Article], names: dict[str, str]
) -> BriefView | None:
    citations = _citations(row, articles, names)
    if citations is None:
        return None
    style, content = _style(row), row.content_json or {}
    bullets: list[Point] = []
    if style == "brief":
        lead = [_point(content["lead"], content.get("lead_citations") or [], citations)]
        bullets = [
            _point(b["text"], b.get("citations") or [], citations)
            for b in content.get("bullets") or []
        ]
    elif style == "snippet":
        lead = [Point(row.text, [])]
    else:
        texts = split_sentences(row.text)
        cited = row.citations_json or []
        if len(texts) != len(cited):  # unreachable for validated rows; show it as one sentence
            texts, cited = [row.text], [sorted({i for c in cited for i in c})]
        lead = [_point(t, c, citations) for t, c in zip(texts, cited, strict=True)]
    return BriefView(
        id=row.id,
        lead=lead,
        bullets=bullets,
        citations=citations,
        basis=row.basis,
        model=row.model,
        generated_at=row.created_at,
        status=row.status,
        style=style,
    )


def build_report(
    row: StorySummary, articles: dict[int, Article], names: dict[str, str]
) -> ReportView | None:
    citations = _citations(row, articles, names)
    if citations is None:
        return None
    paragraphs = []
    if row.status == SummaryStatus.OK:
        paragraphs = [
            _point(p["text"], p.get("citations") or [], citations)
            for p in (row.content_json or {}).get("paragraphs") or []
        ]
    return ReportView(
        id=row.id,
        paragraphs=paragraphs,
        citations=citations,
        model=row.model,
        generated_at=row.created_at,
        status=row.status,
        reason=row.reason,
    )


def _views(session: Session, rows: dict[int, StorySummary], names: dict[str, str], build):
    article_ids = {i for row in rows.values() for i in row.article_ids or []}
    articles = (
        {a.id: a for a in session.scalars(select(Article).where(Article.id.in_(article_ids)))}
        if article_ids
        else {}
    )
    views = {}
    for story_id, row in rows.items():
        view = build(row, articles, names)
        if view is not None:
            views[story_id] = view
    return views


def brief_views(
    session: Session, story_ids: Iterable[int], names: dict[str, str]
) -> dict[int, BriefView]:
    """The brief shown per story (stories without one are missing from the result)."""
    return _views(session, latest_ok(session, story_ids), names, build_view)


summary_views = brief_views


def report_views(
    session: Session, story_ids: Iterable[int], names: dict[str, str]
) -> dict[int, ReportView]:
    """The report shown per story (``ok``, ``failed`` or ``skipped``); missing when none."""
    return _views(session, latest_reports(session, story_ids), names, build_report)
