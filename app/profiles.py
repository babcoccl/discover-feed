"""DB-backed profiles, sources and topics: seeding, topic CRUD and the per-profile feed."""

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.config import AppConfig, Source
from app.models import (
    Article,
    ProfileRecord,
    SourceRecord,
    SourceStatus,
    Topic,
    profile_sources,
)
from app.topics import TopicRule, matches, normalize_keywords

TimeField = Literal["published", "fetched"]


class TopicError(ValueError):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


class InvalidCursor(ValueError):
    pass


# --- seeding -------------------------------------------------------------------------------


def seed_from_config(session: Session, config: AppConfig) -> bool:
    """Copy profiles, sources and topics from YAML into an empty DB. Returns True if seeded.

    Runs only while the profiles table is empty: afterwards the DB is the source of truth.
    """
    if session.scalar(select(func.count()).select_from(ProfileRecord)):
        return False
    sources: dict[str, SourceRecord] = {}
    for profile in config.profiles:
        record = ProfileRecord(slug=profile.id, name=profile.name, description=profile.description)
        for source in profile.sources:
            if source.id not in sources:
                sources[source.id] = SourceRecord(
                    id=source.id,
                    name=source.name,
                    type=source.type.value,
                    url=str(source.url),
                    enabled=source.enabled,
                    refresh_minutes=source.refresh_minutes,
                    tags=list(source.tags),
                )
            record.sources.append(sources[source.id])
        ids_by_name = {s.name: s.id for s in profile.sources}
        for position, topic in enumerate(profile.topics):
            record.topics.append(
                Topic(
                    name=topic.name.strip(),
                    include_keywords=normalize_keywords(topic.include),
                    exclude_keywords=normalize_keywords(topic.exclude),
                    source_ids=[ids_by_name[name] for name in topic.sources],
                    position=position,
                    enabled=topic.enabled,
                )
            )
        session.add(record)
    session.commit()
    return bool(config.profiles)


# --- queries -------------------------------------------------------------------------------


def list_profiles(session: Session) -> list[ProfileRecord]:
    return list(session.scalars(select(ProfileRecord).order_by(ProfileRecord.id)))


def get_profile(session: Session, slug: str) -> ProfileRecord | None:
    return session.scalar(select(ProfileRecord).where(ProfileRecord.slug == slug))


def active_sources(session: Session) -> list[Source]:
    """Enabled sources assigned to at least one profile, as config models for the Ingestor."""
    stmt = (
        select(SourceRecord)
        .where(
            SourceRecord.enabled.is_(True),
            SourceRecord.id.in_(select(profile_sources.c.source_id)),
        )
        .order_by(SourceRecord.id)
    )
    return [record.to_config() for record in session.scalars(stmt)]


def get_source(session: Session, source_id: str) -> Source | None:
    record = session.get(SourceRecord, source_id)
    return record.to_config() if record else None


def source_names(session: Session) -> dict[str, str]:
    return {sid: name for sid, name in session.execute(select(SourceRecord.id, SourceRecord.name))}


def source_statuses(session: Session, source_ids: Sequence[str]) -> dict[str, SourceStatus]:
    stmt = select(SourceStatus).where(SourceStatus.source_id.in_(source_ids))
    return {status.source_id: status for status in session.scalars(stmt)}


# --- topics --------------------------------------------------------------------------------


def get_topic(profile: ProfileRecord, topic_id: int) -> Topic | None:
    return next((t for t in profile.topics if t.id == topic_id), None)


def create_topic(
    session: Session,
    profile: ProfileRecord,
    *,
    name: str,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    source_ids: Sequence[str] = (),
    enabled: bool = True,
    position: int | None = None,
) -> Topic:
    name = _validate(profile, name, source_ids)
    topic = Topic(name=name, position=len(profile.topics))
    _apply(topic, include, exclude, source_ids, enabled)
    profile.topics.append(topic)
    _reorder(profile, topic, len(profile.topics) - 1 if position is None else position)
    session.commit()
    return topic


def update_topic(
    session: Session,
    profile: ProfileRecord,
    topic: Topic,
    *,
    name: str,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    source_ids: Sequence[str] = (),
    enabled: bool = True,
    position: int | None = None,
) -> Topic:
    topic.name = _validate(profile, name, source_ids, current=topic)
    _apply(topic, include, exclude, source_ids, enabled)
    if position is not None:
        _reorder(profile, topic, position)
    session.commit()
    return topic


def move_topic(session: Session, profile: ProfileRecord, topic: Topic, offset: int) -> None:
    ordered = sorted(profile.topics, key=lambda t: t.position)
    _reorder(profile, topic, ordered.index(topic) + offset)
    session.commit()


def delete_topic(session: Session, profile: ProfileRecord, topic: Topic) -> None:
    profile.topics.remove(topic)
    for index, remaining in enumerate(sorted(profile.topics, key=lambda t: t.position)):
        remaining.position = index
    session.commit()


def topic_rule(topic: Topic) -> TopicRule:
    return TopicRule(
        include=tuple(topic.include_keywords or ()),
        exclude=tuple(topic.exclude_keywords or ()),
        source_ids=tuple(topic.source_ids or ()),
    )


def _validate(
    profile: ProfileRecord, name: str, source_ids: Sequence[str], current: Topic | None = None
) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise TopicError("Topic name is required.")
    if len(name) > 100:
        raise TopicError("Topic name must be at most 100 characters.")
    for other in profile.topics:
        if other is not current and other.name.casefold() == name.casefold():
            raise TopicError(f"A topic named {name!r} already exists.", status_code=409)
    unknown = set(source_ids) - {s.id for s in profile.sources}
    if unknown:
        raise TopicError(f"Unknown sources for this profile: {', '.join(sorted(unknown))}.")
    return name


def _apply(
    topic: Topic,
    include: Sequence[str],
    exclude: Sequence[str],
    source_ids: Sequence[str],
    enabled: bool,
) -> None:
    topic.include_keywords = normalize_keywords(include)
    topic.exclude_keywords = normalize_keywords(exclude)
    topic.source_ids = list(dict.fromkeys(source_ids))
    topic.enabled = enabled


def _reorder(profile: ProfileRecord, topic: Topic, position: int) -> None:
    ordered = [t for t in sorted(profile.topics, key=lambda t: t.position) if t is not topic]
    ordered.insert(max(0, min(position, len(ordered))), topic)
    for index, item in enumerate(ordered):
        item.position = index


# --- feed ----------------------------------------------------------------------------------


@dataclass
class FeedPage:
    items: list[Article]
    next_cursor: str | None


def encode_cursor(at: datetime, article_id: int) -> str:
    raw = f"{at.isoformat()}|{article_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        at, article_id = raw.rsplit("|", 1)
        return datetime.fromisoformat(at), int(article_id)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise InvalidCursor("invalid cursor") from None


def feed_page(
    session: Session,
    profile: ProfileRecord,
    topic: Topic | None = None,
    *,
    limit: int = 24,
    cursor: str | None = None,
    time_field: TimeField = "published",
) -> FeedPage:
    """Newest-first articles from the profile's sources, filtered by ``topic`` (None = all).

    Topic matching runs in Python (whole-word regexes), so rows are scanned in keyset-ordered
    batches until ``limit + 1`` matches are found. A SQL LIKE pre-filter on the include
    keywords keeps the scan small.
    """
    column = Article.fetched_at if time_field == "fetched" else Article.published_at
    rule = topic_rule(topic) if topic else TopicRule()
    profile_source_ids = {s.id for s in profile.sources}
    source_ids = sorted(
        profile_source_ids & set(rule.source_ids) if rule.source_ids else profile_source_ids
    )
    if not source_ids:
        return FeedPage(items=[], next_cursor=None)

    base = select(Article).where(Article.source_id.in_(source_ids))
    # SQLite's LIKE only folds ASCII case, so the pre-filter is skipped for non-ASCII keywords.
    if rule.include and all(keyword.isascii() for keyword in rule.include):
        base = base.where(or_(*(_like(keyword) for keyword in rule.include)))
    base = base.order_by(column.desc(), Article.id.desc())

    position = decode_cursor(cursor) if cursor else None
    batch_size = max(limit * 3, 100)
    found: list[Article] = []
    while len(found) <= limit:
        stmt = base.limit(batch_size)
        if position is not None:
            at, article_id = position
            stmt = stmt.where(or_(column < at, and_(column == at, Article.id < article_id)))
        rows = list(session.scalars(stmt))
        for article in rows:
            if matches(
                rule, title=article.title, summary=article.summary_raw, source_id=article.source_id
            ):
                found.append(article)
                if len(found) > limit:
                    break
        if len(rows) < batch_size:
            break
        position = (getattr(rows[-1], column.key), rows[-1].id)

    items = found[:limit]
    next_cursor = None
    if len(found) > limit:
        last = items[-1]
        next_cursor = encode_cursor(getattr(last, column.key), last.id)
    return FeedPage(items=items, next_cursor=next_cursor)


def _like(keyword: str):
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = "%" + "%".join(escaped.split()) + "%"
    return or_(
        Article.title.ilike(pattern, escape="\\"),
        Article.summary_raw.ilike(pattern, escape="\\"),
    )


# --- story feed ----------------------------------------------------------------------------


@dataclass
class StoryItem:
    """A story as seen from one profile: only members from sources the profile follows.

    Articles not clustered yet are single-member items with ``story_id=None``.
    """

    story_id: int | None
    members: list[Article]
    """Oldest first."""
    representative: Article

    @property
    def title(self) -> str:
        return self.representative.title

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
    def image_url(self) -> str | None:
        if self.representative.image_url:
            return self.representative.image_url
        return next((m.image_url for m in self.members if m.image_url), None)

    @property
    def snippet_source(self) -> str:
        return self.representative.summary_raw or next(
            (m.summary_raw for m in self.members if m.summary_raw), ""
        )

    @property
    def sources(self) -> list[Article]:
        """First (earliest) article from each source, in order of first coverage."""
        seen: dict[str, Article] = {}
        for member in self.members:
            seen.setdefault(member.source_id, member)
        return list(seen.values())


@dataclass
class StoryPage:
    items: list[StoryItem]
    next_cursor: str | None


def story_item(story_id: int | None, members: Sequence[Article]) -> StoryItem:
    from app.cluster import representative
    from app.cluster.service import doc

    ordered = sorted(members, key=lambda m: (m.published_at, m.id))
    rep_id = representative([doc(m) for m in ordered]).id
    return StoryItem(
        story_id=story_id,
        members=ordered,
        representative=next(m for m in ordered if m.id == rep_id),
    )


def get_story(session: Session, story_id: int, profile: ProfileRecord | None = None):
    """A story with all its members, or only the profile's members (None if none are left)."""
    stmt = select(Article).where(Article.story_id == story_id)
    if profile is not None:
        stmt = stmt.where(Article.source_id.in_([s.id for s in profile.sources]))
    members = list(session.scalars(stmt))
    return story_item(story_id, members) if members else None


def story_feed_page(
    session: Session,
    profile: ProfileRecord,
    topic: Topic | None = None,
    *,
    limit: int = 24,
    cursor: str | None = None,
) -> StoryPage:
    """Newest-first stories (by latest visible member) from the profile's sources.

    A story matches ``topic`` when any of its visible members (the representative included)
    matches. Unclustered articles are listed as single-article items.
    """
    rule = topic_rule(topic) if topic else TopicRule()
    profile_source_ids = {s.id for s in profile.sources}
    source_ids = sorted(
        profile_source_ids & set(rule.source_ids) if rule.source_ids else profile_source_ids
    )
    if not source_ids:
        return StoryPage(items=[], next_cursor=None)

    # Group key: story id, or -article id for an article not clustered yet.
    key = case((Article.story_id.is_(None), -Article.id), else_=Article.story_id)
    latest = func.max(Article.published_at)
    base = select(key.label("key"), latest.label("latest")).where(Article.source_id.in_(source_ids))
    base = base.group_by(key)
    if rule.include and all(keyword.isascii() for keyword in rule.include):
        hit = or_(*(_like(keyword) for keyword in rule.include))
        base = base.having(func.max(case((hit, 1), else_=0)) == 1)
    base = base.order_by(latest.desc(), key.desc())

    position = decode_cursor(cursor) if cursor else None
    batch_size = max(limit * 3, 100)
    found: list[tuple[datetime, int, StoryItem]] = []
    while len(found) <= limit:
        stmt = base.limit(batch_size)
        if position is not None:
            at, last_key = position
            stmt = stmt.having(or_(latest < at, and_(latest == at, key < last_key)))
        rows = session.execute(stmt).all()
        members = _group_members(session, [row.key for row in rows], source_ids)
        for row in rows:
            group = members.get(row.key, [])
            if group and any(
                matches(rule, title=m.title, summary=m.summary_raw, source_id=m.source_id)
                for m in group
            ):
                found.append(
                    (row.latest, row.key, story_item(row.key if row.key > 0 else None, group))
                )
                if len(found) > limit:
                    break
        if len(rows) < batch_size:
            break
        position = (rows[-1].latest, rows[-1].key)

    items = found[:limit]
    next_cursor = encode_cursor(items[-1][0], items[-1][1]) if len(found) > limit else None
    return StoryPage(items=[item for _, _, item in items], next_cursor=next_cursor)


def _group_members(
    session: Session, keys: Sequence[int], source_ids: Sequence[str]
) -> dict[int, list[Article]]:
    story_ids = [k for k in keys if k > 0]
    article_ids = [-k for k in keys if k < 0]
    if not keys:
        return {}
    stmt = select(Article).where(
        Article.source_id.in_(source_ids),
        or_(Article.story_id.in_(story_ids), Article.id.in_(article_ids)),
    )
    groups: dict[int, list[Article]] = {}
    for article in session.scalars(stmt):
        groups.setdefault(article.story_id or -article.id, []).append(article)
    return groups
