from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
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
    state: Mapped[str] = mapped_column(String(20), default="clustered", index=True)
    """``StoryState``: where the story is in ingest -> ... -> published (``merged``: its
    articles joined ``merged_into``, which old links redirect to)."""
    state_reason: Mapped[str | None] = mapped_column(Text)
    """Why the story is waiting (e.g. ``timeout``, ``insufficient_text``)."""
    priority: Mapped[int] = mapped_column(Integer, default=0)
    """Pipeline > Prioritize: higher goes first; ties go newest first."""
    merged_into: Mapped[int | None] = mapped_column(Integer)
    ingested_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    clustered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    brief_ready_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    published_version: Mapped[int | None] = mapped_column(Integer, index=True)
    """``story_views.version`` the feed and story page serve; NULL until first published."""
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    """When the story was first published."""
    published_sort_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    """``last_updated_at`` of the served version (feed order)."""


class StoryState(StrEnum):
    INGESTED = "ingested"
    TEXT_PENDING = "text_pending"
    TEXT_FINAL = "text_final"
    CLUSTERED = "clustered"
    BRIEF_READY = "brief_ready"
    REPORT_FINAL = "report_final"
    PUBLISHED = "published"
    MERGED = "merged"


class StoryView(Base):
    """A ready-to-render story (everything the feed card and story page show). Versions are
    append-only; ``stories.published_version`` points at the one being served."""

    __tablename__ = "story_views"
    __table_args__ = (UniqueConstraint("story_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20))
    """``complete`` | ``report_pending`` | ``brief_fallback`` | ``draft`` (not publishable)."""
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    doc: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class GenerationAttempt(Base):
    """One LLM request for a brief or report (the last 500 are kept)."""

    __tablename__ = "generation_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[int | None] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(10))
    model: Mapped[str] = mapped_column(String(200), default="")
    endpoint_host: Mapped[str] = mapped_column(String(300), default="")
    members: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    """``[{article_id, source, text_status, words}]`` of the prompt's sources."""
    input_words: Mapped[int] = mapped_column(Integer, default=0)
    estimated_tokens: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    finish_reason: Mapped[str | None] = mapped_column(String(40))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    content_length: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_length: Mapped[int] = mapped_column(Integer, default=0)
    raw_content: Mapped[str] = mapped_column(Text, default="")
    """First 2,000 characters of the answer."""
    validation_errors: Mapped[list[str]] = mapped_column(JSON, default=list)
    outcome: Mapped[str] = mapped_column(String(10))
    """``ok`` | ``failed``."""
    reason: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(32))
    """Shared by the attempts of one generation (quality metrics)."""
    attempt: Mapped[int | None] = mapped_column(Integer)
    """1-based position within its prompt's attempts."""
    prompt: Mapped[str | None] = mapped_column(String(10))
    """What was asked: ``brief``, ``report`` or ``summary`` (a brief's fallback)."""
    temperature: Mapped[float | None] = mapped_column(Float)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    """Non-fatal validator findings (suspected glued words)."""
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class Conversation(Base):
    """A story Q&A thread. No accounts: the unguessable id is the only key (the story page
    keeps it in localStorage); idle ones are deleted after ``chat_retention_days``."""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    story_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_active_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class ChatMessage(Base):
    """A question (``user``) or an answer (``assistant``) in a conversation."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text, default="")
    citations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    """The sources the answer's [n] markers resolve to."""
    verification_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(200), default="")
    prompt_version: Mapped[str] = mapped_column(String(40), default="")
    tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    time_to_first_token_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="complete")
    """``complete`` | ``streaming`` | ``incomplete`` (stopped or failed mid-answer; the
    partial text is kept) | ``error`` (no answer)."""
    error: Mapped[str | None] = mapped_column(Text)
    """Why an answer is incomplete or failed (``client_disconnected``, ``timeout``, ...)."""
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class StoryEventRow(Base):
    """A published/updated/retracted story, kept ``summaries.event_retention_days`` so stream
    clients can resume (``Last-Event-ID``)."""

    __tablename__ = "story_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(20))
    """``story.published`` | ``story.updated`` | ``story.retracted``."""
    story_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[int | None] = mapped_column(Integer)
    profiles: Mapped[list[str]] = mapped_column(JSON, default=list)
    """Slugs of the profiles following one of the story's sources."""
    data: Mapped[dict | None] = mapped_column(JSON)
    """Extra payload (``alert.fired``: rule, article_id, matched)."""
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


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
    items_last_run: Mapped[int | None] = mapped_column(Integer)


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
    aggregator: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    first_party: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    api_key_env: Mapped[str | None] = mapped_column(String(100))
    """Environment variable *name* of the API key; the value is never stored."""
    limits: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    """daily_request_budget, max_posts_per_run, monthly_budget_usd, cost_per_read_usd."""
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    """Deleted in the UI/API: no longer fetched or listed; its articles and stories stay."""
    auto_disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=utcnow)

    def to_config(self) -> Source:
        limits = {k: v for k, v in (self.limits or {}).items() if v is not None}
        return Source(
            id=self.id,
            name=self.name,
            type=self.type,
            url=self.url or None,
            enabled=self.enabled,
            refresh_minutes=self.refresh_minutes,
            tags=list(self.tags or []),
            aggregator=bool(self.aggregator),
            first_party=bool(self.first_party),
            params=dict(self.params or {}),
            api_key=self.api_key_env or None,
            **limits,
        )

    def apply(self, source: Source) -> None:
        """Copy editable fields from a validated config model."""
        self.name = source.name
        self.type = source.type.value
        self.url = str(source.url)
        self.enabled = source.enabled
        self.refresh_minutes = source.refresh_minutes
        self.tags = list(source.tags)
        self.aggregator = source.aggregator
        self.first_party = source.first_party
        self.params = dict(source.params)
        self.api_key_env = source.api_key
        self.limits = {
            key: getattr(source, key)
            for key in LIMIT_FIELDS
            if getattr(source, key) is not None and key in source.model_fields_set
        }

    @classmethod
    def from_config(cls, source: Source) -> "SourceRecord":
        record = cls(id=source.id)
        record.apply(source)
        return record


LIMIT_FIELDS = (
    "daily_request_budget",
    "max_posts_per_run",
    "monthly_budget_usd",
    "cost_per_read_usd",
)


class SourceUsage(Base):
    """API calls and paid reads per source and period (``day:2026-10-12`` / ``month:2026-10``)
    so budgets survive restarts."""

    __tablename__ = "source_usage"

    source_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    period: Mapped[str] = mapped_column(String(20), primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    reads: Mapped[int] = mapped_column(Integer, default=0)
    spend_usd: Mapped[float] = mapped_column(Float, default=0.0)


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
    alerts_seeded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    """Set once the YAML alert rules/channels were copied in (the DB owns them afterwards)."""


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
    """The endpoint failed (HTTP error, timeout), or a report failed validation."""
    SKIPPED = "skipped"
    """Reports only: not enough source text (``reason`` says why)."""


class SummaryKind(StrEnum):
    BRIEF = "brief"
    """Lead sentence + three cited bullets (feed cards)."""
    REPORT = "report"
    """Multi-paragraph cited report (story page)."""


class StorySummary(Base):
    """Every generated brief/report version of a story (prior versions are kept)."""

    __tablename__ = "story_summaries"
    __table_args__ = (UniqueConstraint("story_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[int | None] = mapped_column(Integer, index=True)
    """Set to NULL when the story is deleted (rebuild); rows stay reusable by ``input_hash``."""
    version: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(
        String(10), default="brief", server_default="brief", index=True
    )
    text: Mapped[str] = mapped_column(Text, default="")
    """Plain text: the brief's lead + bullets, the report's paragraphs, or the fallback text."""
    content_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    """Structured answer: brief ``{lead, lead_citations, bullets}``, report ``{paragraphs}``, or
    ``{style: summary|snippet}`` for a brief fallback. NULL for pre-brief (Phase 4) rows."""
    citations_json: Mapped[list[list[int]]] = mapped_column(JSON, default=list)
    """One list of source indexes (1-based) per sentence (Phase 4 style rows)."""
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
    """A story's brief or report waiting to be (re-)generated."""

    __tablename__ = "summary_jobs"

    story_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), primary_key=True, default="brief")
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    force: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)


# --- alerts (app/alerts) -----------------------------------------------------------------


class AlertChannelRecord(Base):
    """A notification target. ``secret_env`` maps secret fields to environment variable
    *names*; secret values are never stored."""

    __tablename__ = "alert_channels"
    __table_args__ = (UniqueConstraint("profile_id", "key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(10))
    """``webhook`` | ``ntfy`` | ``email``."""
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    secret_env: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AlertRuleRecord(Base):
    __tablename__ = "alert_rules"
    __table_args__ = (UniqueConstraint("profile_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    include: Mapped[list[dict]] = mapped_column(JSON, default=list)
    """``[{"keyword": "AAPL", "case_sensitive": true}, ...]``."""
    exclude: Mapped[list[dict]] = mapped_column(JSON, default=list)
    match: Mapped[str] = mapped_column(String(5), default="any")
    match_in: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["title", "summary"])
    source_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    priority: Mapped[str] = mapped_column(String(10), default="default")
    channels: Mapped[list[str]] = mapped_column(JSON, default=list)
    """Channel keys."""
    quiet_start: Mapped[str | None] = mapped_column(String(5))
    quiet_end: Mapped[str | None] = mapped_column(String(5))
    max_per_hour: Mapped[int] = mapped_column(Integer, default=10)
    notify_on_update: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AlertFiring(Base):
    """One decision to notify: the exactly-once ledger. ``lineage_id`` is the story the
    article lived in when it fired; merges are handled by checking every member of the
    article's current story. ``slot`` is 0 for alerts and the hour bucket for updates."""

    __tablename__ = "alert_firings"
    __table_args__ = (UniqueConstraint("rule_id", "lineage_id", "kind", "slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rules.id", ondelete="CASCADE"))
    lineage_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(10))
    """``alert`` | ``update``."""
    slot: Mapped[int] = mapped_column(Integer, default=0)
    article_id: Mapped[int] = mapped_column(Integer, index=True)
    source_id: Mapped[str] = mapped_column(String(200))
    matched: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class NotificationKind(StrEnum):
    ALERT = "alert"
    UPDATE = "update"
    DIGEST = "digest"
    TEST = "test"


class NotificationStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    HELD = "held"
    """Quiet hours or over ``max_per_hour``: waits for the next digest."""
    DIGESTED = "digested"
    """A held notification that went out inside a digest."""


class Notification(Base):
    """One message to one channel (the persistent send queue and the notification log).
    ``payload`` is what gets rendered per channel; it never contains secrets."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="SET NULL"), index=True
    )
    rule_name: Mapped[str] = mapped_column(String(100), default="")
    firing_id: Mapped[int | None] = mapped_column(Integer)
    lineage_id: Mapped[int | None] = mapped_column(Integer)
    article_id: Mapped[int | None] = mapped_column(Integer)
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_channels.id", ondelete="SET NULL"), index=True
    )
    channel_key: Mapped[str] = mapped_column(String(100), default="")
    kind: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10), index=True)
    hold_reason: Mapped[str | None] = mapped_column(String(20))
    """``quiet_hours`` | ``rate_limit``."""
    digest_id: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
