"""Read side of the publish gate: published ``story_views`` documents, rendered as-is.

The feed, the story page and the story APIs only read here. Nothing in this module (or in
the GET routes using it) generates, queues or waits for anything; it imports no LLM or
summarization code (``tests/test_architecture.py`` checks that).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.labels import label, publisher_of
from app.models import Story, StoryState, StoryView
from app.topics import TopicRule, matches

SCHEMA_VERSION = 1

# Outlet kinds in coverage order — mirrors ``app.outlets.KINDS`` (kept literal here: this
# module stays free of non-model app imports for ``tests/test_architecture.py``).
_KINDS = (
    "wire",
    "international",
    "national",
    "local",
    "trade",
    "state",
    "blog",
    "unknown",
)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class Citation:
    index: int
    article_id: int
    source: str
    headline: str
    url: str
    publisher: str = ""
    """The URL's domain, the primary label (documents from before it: derived on load)."""
    via: str | None = None
    """``Hacker News`` when the source is an aggregator (secondary label)."""

    @property
    def label(self) -> str:
        return label(self.publisher, self.via)


@dataclass(frozen=True)
class Point:
    text: str
    citations: list[Citation]

    @property
    def marked(self) -> str:
        return self.text + (" " + "".join(f"[{c.index}]" for c in self.citations)).rstrip()


@dataclass(frozen=True)
class Brief:
    lead: list[Point]
    bullets: list[Point]
    citations: list[Citation]
    basis: str
    model: str
    generated_at: datetime
    status: str
    style: str
    """``brief`` | ``summary`` | ``snippet`` (the feed snippet: no valid brief)."""
    label: str | None = None
    """``Summary unavailable: <reason>`` on a snippet fallback."""

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
        return " ".join(p.marked for p in self.sentences)


@dataclass(frozen=True)
class Report:
    state: str
    """``ready`` | ``pending`` | ``skipped`` | ``failed``."""
    reason: str | None
    paragraphs: list[Point]
    citations: list[Citation]
    model: str
    generated_at: datetime | None
    label: str | None = None
    details: list[str] = field(default_factory=list)
    """Per-member text status and word count (why a report was skipped)."""

    @property
    def status(self) -> str:
        """The pre-publish-gate name (``ok`` for ``ready``)."""
        return "ok" if self.state == "ready" else self.state

    @property
    def word_count(self) -> int:
        return sum(len(p.text.split()) for p in self.paragraphs)


@dataclass(frozen=True)
class Member:
    id: int
    source_id: str
    source: str
    title: str
    url: str
    published_at: datetime
    image_url: str | None
    summary_raw: str
    text_status: str
    words: int
    publisher: str = ""
    via: str | None = None
    origin: str = "feed"
    """``feed`` | ``enrichment`` (found by web search; shown with a badge)."""
    flag: str = ""
    """Source-quality flag (``tld:``/``name:``): enrichment member whose domain looks
    like a content farm — still shown and cited, with a "quality uncertain" badge."""
    outlet: str = ""
    """Outlet name from ``config/outlets.yaml`` (the feed's display name / the domain
    when not listed)."""
    country: str | None = None
    """ISO country code of the outlet's home market (from the registry or the TLD)."""
    kind: str = "unknown"
    """``wire`` | ``international`` | ``national`` | ``local`` | ``trade`` | ``state`` |
    ``blog`` | ``unknown``."""
    groups: list[str] = field(default_factory=list)
    """User-defined outlet tags (``config/outlets.yaml``)."""
    headline_only: bool = False
    """Member stored without text: shown in the coverage panel only — never
    numbered, summarized or cited."""

    @property
    def link_only(self) -> bool:
        """Enrichment member stored without its text: shown as \"also reported by\",
        never numbered, summarized or cited (``enrichment.link_only`` /
        ``headline_only``)."""
        return self.text_status == "none"

    @property
    def label(self) -> str:
        return label(self.publisher, self.via)

    @property
    def found_by_search(self) -> bool:
        return self.origin == "enrichment"


@dataclass
class PublishedStory:
    """A published story as one profile sees it (``members`` from its sources only)."""

    story_id: int
    version: int
    title: str
    members: list[Member]
    representative: Member
    image_url: str | None
    brief: Brief | None
    report: Report | None
    model: str
    generated_at: datetime
    published_at: datetime | None
    all_source_count: int
    image: dict | None = None
    """``{url, key, source, width, height, generic}`` (``app.images.select``)."""
    logo: dict | None = None
    """``{url, key, tint, tint_dark}``: publisher icon for the no-image card."""

    @property
    def first_seen_at(self) -> datetime:
        return self.members[0].published_at

    @property
    def last_updated_at(self) -> datetime:
        return max(m.published_at for m in self.members)

    @property
    def source_ids(self) -> list[str]:
        return list(dict.fromkeys(m.source_id for m in self.members))

    @property
    def source_count(self) -> int:
        return len(self.source_ids)

    @property
    def snippet_source(self) -> str:
        return self.representative.summary_raw or next(
            (m.summary_raw for m in self.members if m.summary_raw), ""
        )

    @property
    def sources(self) -> list[Member]:
        seen: dict[str, Member] = {}
        for member in self.members:
            seen.setdefault(member.source_id, member)
        return list(seen.values())

    @property
    def coverage(self) -> dict | None:
        """Coverage-panel data: distinct outlets and countries, the headline-only
        count, and members grouped by outlet ``kind`` (registry order)."""
        if not self.members:
            return None
        order = {k: i for i, k in enumerate(_KINDS)}
        by_kind: dict[str, list[Member]] = {}
        for m in self.members:
            by_kind.setdefault(m.kind or "unknown", []).append(m)
        return {
            "outlets": len({m.outlet or m.source for m in self.members}),
            "countries": sorted({m.country for m in self.members if m.country}),
            "headline_only": sum(1 for m in self.members if m.headline_only),
            "by_kind": sorted(
                by_kind.items(), key=lambda kv: (order.get(kv[0], len(order)), kv[0])
            ),
        }


def _citations(items: Sequence[dict]) -> list[Citation]:
    out = []
    for c in items:
        c = {**c, "publisher": c.get("publisher") or publisher_of(c["url"], c["source"])}
        out.append(Citation(**c))
    return out


def _points(items: Sequence[dict], citations: list[Citation]) -> list[Point]:
    by_index = {c.index: c for c in citations}
    return [
        Point(p["text"], [by_index[i] for i in p.get("citations", []) if i in by_index])
        for p in items
    ]


def _brief(doc: dict | None) -> Brief | None:
    if not doc:
        return None
    citations = _citations(doc.get("citations", []))
    return Brief(
        lead=_points(doc.get("lead", []), citations),
        bullets=_points(doc.get("bullets", []), citations),
        citations=citations,
        basis=doc.get("basis", ""),
        model=doc.get("model", ""),
        generated_at=_dt(doc.get("generated_at")),
        status=doc.get("status", ""),
        style=doc.get("style", "brief"),
        label=doc.get("label"),
    )


def _report(doc: dict | None) -> Report | None:
    if not doc:
        return None
    citations = _citations(doc.get("citations", []))
    return Report(
        state=doc["state"],
        reason=doc.get("reason"),
        paragraphs=_points(doc.get("paragraphs", []), citations),
        citations=citations,
        model=doc.get("model", ""),
        generated_at=_dt(doc.get("generated_at")),
        label=doc.get("label"),
        details=list(doc.get("details", [])),
    )


def load(view: StoryView, source_ids: Sequence[str] | None = None) -> PublishedStory | None:
    """The view as seen from a profile following ``source_ids`` (None: every member)."""
    doc = view.doc
    members = [
        Member(
            id=m["id"],
            source_id=m["source_id"],
            source=m["source"],
            title=m["title"],
            url=m["url"],
            published_at=_dt(m["published_at"]),
            image_url=m.get("image_url"),
            summary_raw=m.get("summary", ""),
            text_status=m.get("text_status", ""),
            words=m.get("words", 0),
            publisher=m.get("publisher") or publisher_of(m["url"], m["source"]),
            via=m.get("via"),
            origin=m.get("origin") or "feed",
            flag=m.get("flag") or "",
            outlet=m.get("outlet") or m["source"],
            country=m.get("country"),
            kind=m.get("kind") or "unknown",
            groups=m.get("groups") or [],
            headline_only=bool(m.get("headline_only")) or m.get("text_status") == "none",
        )
        for m in doc.get("members", [])
    ]
    followed = set(source_ids) if source_ids is not None else None
    visible = [
        m
        for m in members
        if followed is None or (not m.found_by_search and m.source_id in followed)
    ]
    if visible and followed is not None:  # web-search members ride along, never alone
        visible = [m for m in members if m in visible or m.found_by_search]
    if not visible:
        return None
    lead = next((m for m in visible if not m.found_by_search), visible[0])
    rep = next((m for m in visible if m.id == doc.get("representative_id")), lead)
    image = doc.get("image")
    if image is None:  # views written before image selection
        url = rep.image_url or next((m.image_url for m in visible if m.image_url), None)
        image = {"url": url, "source": "feed" if url else "none"}
    elif image.get("article_id") and image["article_id"] not in {m.id for m in visible}:
        url = next((m.image_url for m in visible if m.image_url), None)
        image = {"url": url, "source": "member" if url else "none"}
    return PublishedStory(
        story_id=doc["story_id"],
        version=view.version,
        title=doc.get("title") or rep.title,
        members=visible,
        representative=rep,
        image_url=image.get("url"),
        image=image,
        logo=doc.get("logo"),
        brief=_brief(doc.get("brief")),
        report=_report(doc.get("report")),
        model=doc.get("model", ""),
        generated_at=_dt(doc.get("generated_at")),
        published_at=view.published_at,
        all_source_count=doc.get("source_count", len({m.source_id for m in members})),
    )


def _published(session: Session):
    return select(Story, StoryView).join(
        StoryView,
        and_(StoryView.story_id == Story.id, StoryView.version == Story.published_version),
    )


def published_story(
    session: Session, story_id: int, source_ids: Sequence[str] | None = None
) -> PublishedStory | None:
    row = session.execute(_published(session).where(Story.id == story_id)).first()
    return load(row.StoryView, source_ids) if row else None


def resolve(session: Session, story_id: int) -> int | None:
    """The story a merged id now lives in (follows chains), or None when unknown."""
    seen: set[int] = set()
    story = session.get(Story, story_id)
    while story is not None and story.merged_into and story.id not in seen:
        seen.add(story.id)
        story = session.get(Story, story.merged_into)
    return story.id if story is not None else None


@dataclass
class Page:
    items: list[PublishedStory]
    next_cursor: str | None


def published_page(
    session: Session,
    source_ids: Sequence[str],
    rule: TopicRule | None = None,
    *,
    limit: int = 24,
    cursor: tuple[datetime, int] | None = None,
    since: datetime | None = None,
    published_after: datetime | None = None,
    updated_after: datetime | None = None,
) -> tuple[list[tuple[datetime, int, PublishedStory]], bool]:
    """Published stories with a member from ``source_ids``, newest ``last_updated_at`` first.
    ``published_after``: first published after; ``updated_after``: the served version was
    published after. Returns ``(items, has_more)``; items are ``(sort_at, story_id, story)``."""
    from app.models import Article

    rule = rule or TopicRule()
    member = (
        select(Article.id)
        .where(Article.story_id == Story.id, Article.source_id.in_(list(source_ids)))
        .exists()
    )
    base = _published(session).where(Story.published_version.is_not(None), member)
    if since is not None:
        base = base.where(Story.published_sort_at > since)
    if published_after is not None:
        first = func.coalesce(Story.published_at, Story.published_sort_at)
        base = base.where(first > published_after)
    if updated_after is not None:
        base = base.where(
            func.coalesce(StoryView.published_at, StoryView.updated_at) > updated_after
        )
    base = base.order_by(Story.published_sort_at.desc(), Story.id.desc())
    found: list[tuple[datetime, int, PublishedStory]] = []
    position = cursor
    batch = max(limit * 3, 60)
    while len(found) <= limit:
        stmt = base
        if position is not None:
            at, last_id = position
            stmt = stmt.where(
                (Story.published_sort_at < at)
                | ((Story.published_sort_at == at) & (Story.id < last_id))
            )
        rows = session.execute(stmt.limit(batch)).all()
        for row in rows:
            story = load(row.StoryView, source_ids)
            if story is None:
                continue
            filtered = rule.include or rule.exclude or rule.source_ids
            if filtered and not any(
                matches(rule, title=m.title, summary=m.summary_raw, source_id=m.source_id)
                for m in story.members
            ):
                continue
            found.append((row.Story.published_sort_at, row.Story.id, story))
            if len(found) > limit:
                break
        if len(rows) < batch:
            break
        position = (rows[-1].Story.published_sort_at, rows[-1].Story.id)
    return found[:limit], len(found) > limit


def stage_counts(session: Session) -> dict[str, int]:
    """Stories per state (for "Preparing your first stories...")."""
    from sqlalchemy import func

    counts = dict(session.execute(select(Story.state, func.count()).group_by(Story.state)).all())
    return {s.value: counts.get(s.value, 0) for s in StoryState}


def published_since(
    session: Session, source_ids: Sequence[str], since: datetime
) -> list[tuple[int, int]]:
    """``(story_id, version)`` of stories first published after ``since`` (the feed's
    "N new stories" polling fallback when SSE is unavailable)."""
    from app.models import Article

    member = (
        select(Article.id)
        .where(Article.story_id == Story.id, Article.source_id.in_(list(source_ids)))
        .exists()
    )
    stmt = (
        select(Story.id, Story.published_version)
        .where(Story.published_version.is_not(None), Story.published_at > since, member)
        .order_by(Story.published_at.desc())
        .limit(200)
    )
    return [(row[0], row[1]) for row in session.execute(stmt).all()]
