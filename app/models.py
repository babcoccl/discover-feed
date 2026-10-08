from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import Source
from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Stores naive UTC (SQLite has no tz support) and always returns aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    url: Mapped[str] = mapped_column(String(2048), default="", server_default="")
    """The feed's permalink: what users see and click."""
    canonical_url: Mapped[str] = mapped_column(String(2048), unique=True)
    """Normalized URL used only for deduplication."""
    title: Mapped[str] = mapped_column(Text, default="")
    summary_raw: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)
    image_url: Mapped[str | None] = mapped_column(String(2048))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    text: Mapped[str | None] = mapped_column(Text)
    """Extracted article body. Internal only (clustering, later summaries); never displayed."""
    text_status: Mapped[str] = mapped_column(
        String(10), default="pending", server_default="pending", index=True
    )
    text_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    word_count: Mapped[int | None] = mapped_column(Integer)
    story_id: Mapped[int | None] = mapped_column(
        ForeignKey("stories.id", ondelete="SET NULL"), index=True
    )


class TextStatus(StrEnum):
    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class Story(Base):
    """A group of articles about the same event, from one or more sources (profile-agnostic)."""

    __tablename__ = "stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="")
    representative_article_id: Mapped[int | None] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    """Earliest member published_at."""
    last_updated_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    """Latest member published_at."""
    article_count: Mapped[int] = mapped_column(Integer, default=0)
    source_count: Mapped[int] = mapped_column(Integer, default=0)


class PipelineRun(Base):
    """Last run of each pipeline step (ingest, extract, cluster)."""

    __tablename__ = "pipeline_runs"

    name: Mapped[str] = mapped_column(String(20), primary_key=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SourceStatus(Base):
    __tablename__ = "source_status"

    source_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    etag: Mapped[str | None] = mapped_column(String(500))
    last_modified: Mapped[str | None] = mapped_column(String(100))


profile_sources = Table(
    "profile_sources",
    Base.metadata,
    Column("profile_id", ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True),
    Column("source_id", ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True),
)


class SourceRecord(Base):
    """A feed; shared by every profile it is assigned to."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(20), default="rss")
    url: Mapped[str] = mapped_column(String(2048))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    refresh_minutes: Mapped[int] = mapped_column(Integer, default=30)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    def to_config(self) -> Source:
        return Source(
            id=self.id,
            name=self.name,
            type=self.type,
            url=self.url,
            enabled=self.enabled,
            refresh_minutes=self.refresh_minutes,
            tags=list(self.tags or []),
        )


class ProfileRecord(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[list[SourceRecord]] = relationship(
        secondary=profile_sources, order_by=SourceRecord.name, lazy="selectin"
    )
    topics: Mapped[list["Topic"]] = relationship(
        back_populates="profile",
        order_by="Topic.position",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (UniqueConstraint("profile_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    include_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    exclude_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    position: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    profile: Mapped[ProfileRecord] = relationship(back_populates="topics")


class SummaryStatus(StrEnum):
    OK = "ok"
    FALLBACK = "fallback"
    """The model's answers failed validation; ``text`` is the feed summary."""
    FAILED = "failed"
    """The endpoint failed (HTTP error, timeout); ``text`` is the feed summary."""


class StorySummary(Base):
    """Every generated summary version of a story (prior versions are kept)."""

    __tablename__ = "story_summaries"
    __table_args__ = (UniqueConstraint("story_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[int | None] = mapped_column(Integer, index=True)
    """Set to NULL when the story is deleted (rebuild); rows stay reusable by ``input_hash``."""
    version: Mapped[int] = mapped_column(Integer, default=1)
    text: Mapped[str] = mapped_column(Text, default="")
    citations_json: Mapped[list[list[int]]] = mapped_column(JSON, default=list)
    """One list of source indexes (1-based) per sentence."""
    article_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    """Article id of each numbered source: ``article_ids[n - 1]`` is [n]."""
    basis: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(200), default="")
    prompt_version: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(10), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class SummaryJob(Base):
    """A story waiting to be (re-)summarized."""

    __tablename__ = "summary_jobs"

    story_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    force: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
