"""Profile configuration models and YAML loader.

A profile bundles the topics, sources, keywords, alert rules and LLM endpoint
for one feed. Values in the YAML file may reference environment variables with
``${VAR}`` or ``${VAR:-default}`` (default used when unset or empty) so secrets
never need to live in the file.
"""

import logging
import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    model_validator,
)

from app.context.rules import SameEventRules
from app.enrich.quality import (
    DEFAULT_DENY_DOMAINS,
    DEFAULT_SUSPICIOUS_PATTERNS,
    DEFAULT_SUSPICIOUS_TLDS,
)

logger = logging.getLogger(__name__)

_SLUG = r"^[a-z0-9][a-z0-9_-]*$"
_ENV_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StructuredOutput(StrEnum):
    JSON_SCHEMA = "json_schema"
    JSON_OBJECT = "json_object"
    NONE = "none"


class LLMRole(_Model):
    """One OpenAI-compatible chat completions endpoint (llama.cpp, vLLM, Ollama, OpenAI...).

    Providers differ only in these settings, never in code.
    """

    base_url: HttpUrl
    api_key: SecretStr | None = None
    model: str = Field(min_length=1)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=600, ge=1)
    timeout_seconds: float = Field(default=120, gt=0)
    context_tokens: int = Field(
        default=16384,
        ge=1024,
        description="The model's context window; report inputs are trimmed to leave room for "
        "the answer.",
    )
    structured_output: StructuredOutput = StructuredOutput.JSON_SCHEMA
    disable_thinking: bool = Field(
        default=True,
        description='Send chat_template_kwargs {"enable_thinking": false} (llama.cpp); '
        "set false for providers that reject unknown parameters.",
    )


class LLMConfig(_Model):
    summarizer: LLMRole
    chat: LLMRole | None = Field(
        default=None, description="Story Q&A (POST /api/v1/stories/{id}/ask); unset: no chat."
    )


class CardStyle(StrEnum):
    LEAD_BULLETS = "lead_bullets"
    LEAD_ONLY = "lead_only"


class ReportMode(StrEnum):
    AUTO = "auto"
    """Generate reports in the background for recent multi-source stories."""
    ON_DEMAND = "on_demand"
    """Deprecated: pages never trigger generation now; treated as ``auto``."""
    OFF = "off"


class SummariesConfig(_Model):
    """Briefs (cards) and detailed reports (story page). Read from the YAML on every start."""

    card_style: CardStyle = CardStyle.LEAD_BULLETS
    report_mode: ReportMode = ReportMode.AUTO
    publish_requires_report: bool = Field(
        default=True,
        description="Publish a story only once its report is final (ready, skipped or failed).",
    )
    publish_max_wait_minutes: float = Field(
        default=30,
        ge=0,
        description="After this long with a valid brief, publish with the report still pending.",
    )
    brief_fallback_hours: float = Field(
        default=6,
        ge=0,
        description="After this long without a valid brief, publish the feed snippet instead.",
    )
    extraction_wait_seconds: float = Field(
        default=120, ge=0, description="Cluster a story after this long even if text is pending."
    )
    max_inflight: int = Field(default=1, ge=1, description="LLM requests at once (all kinds).")
    summarize_backfill_hours: float = Field(
        default=24, gt=0, description="Only stories updated this recently are generated."
    )
    summarize_backfill_max: int = Field(
        default=40, ge=0, description="At most this many never-published stories are queued."
    )
    report_auto_max_age_hours: float | None = Field(default=None, deprecated=True)
    """Deprecated, ignored (use ``summarize_backfill_hours``)."""
    max_reports_per_run: int | None = Field(default=None, deprecated=True)
    """Deprecated, ignored: stories go brief -> report -> publish one at a time."""
    report_concurrency: int | None = Field(default=None, deprecated=True)
    """Deprecated, ignored (use ``max_inflight``)."""
    report_max_articles: int = Field(default=4, ge=1)
    report_max_words_per_article: int | None = Field(default=None, deprecated=True)
    """Deprecated, ignored (use ``report_total_words``)."""
    report_total_words: int = Field(
        default=2400,
        ge=100,
        description="Source words sent for a report, shared by its articles (one per source "
        "first); a one-source story may use all of it.",
    )
    report_max_tokens: int = Field(
        default=1400, ge=100, description="max_tokens for report requests (briefs use the role's)."
    )
    report_timeout_seconds: float = Field(
        default=300, gt=0, description="Timeout for report requests (briefs use the role's)."
    )
    duplicate_threshold: float = Field(
        default=0.6,
        gt=0,
        le=1,
        description="Token overlap (Jaccard) at which two bullets/paragraphs count as duplicates.",
    )
    verbatim_max_run: int = Field(
        default=12,
        ge=4,
        description="Reject a report that copies this many consecutive words from a source.",
    )
    summarize_max_articles_per_story: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Full-text members picked for a brief (sources beyond it are listed, "
        "not dropped). Deep-tier stories use summarize_max_articles_per_story_deep.",
    )
    summarize_max_articles_per_story_deep: int = Field(
        default=8,
        ge=1,
        le=20,
        description="Brief and report source cap for stories enriched on the deep tier "
        "(enrichment.depth); also raises report_max_articles for them.",
    )
    verbatim_max_ratio: float = Field(
        default=0.20,
        gt=0,
        le=1,
        description="Reject a report when more than this share of its 8-word phrases occur in "
        "the sources.",
    )
    verbatim_allow: list[str] = Field(
        default_factory=list,
        description="Names, numbers and technical terms that copied runs may consist of (story "
        "title words are always allowed); also skipped by the glued-word warning.",
    )
    retry_temperature_bump: float = Field(
        default=0.2, ge=0, le=1, description="Added to the temperature on validation retries."
    )
    event_retention_days: float = Field(
        default=7, gt=0, description="Story events kept for stream replay (Last-Event-ID)."
    )
    chat_context_words: int = Field(
        default=3000,
        ge=100,
        description="Source words sent with a follow-up question, shared by the story's "
        "articles (also trimmed to the chat role's context_tokens).",
    )
    chat_history_turns: int = Field(
        default=6, ge=0, description="Earlier question/answer pairs sent with a question."
    )
    chat_retention_days: float = Field(
        default=30, gt=0, description="Conversations idle this long are deleted."
    )
    chat_priority: bool = Field(
        default=True,
        description="While a chat request waits or streams, the generation worker starts no "
        "new LLM request (shared endpoint only).",
    )
    chat_max_concurrent: int = Field(
        default=1, ge=1, description="Chat requests streaming at once for this profile."
    )
    followups: bool = Field(
        default=True,
        description="Suggest 3 follow-up questions per published story (needs llm.chat); "
        "generated only when no brief or report is waiting.",
    )


class SourceType(StrEnum):
    RSS = "rss"
    ATOM = "atom"
    WEB = "web"
    API = "api"
    GUARDIAN = "guardian"
    NEWSDATA = "newsdata"
    X = "x"


FEED_TYPES = frozenset({SourceType.RSS, SourceType.ATOM})
API_TYPES = frozenset({SourceType.GUARDIAN, SourceType.NEWSDATA, SourceType.X})
API_PARAMS: dict[SourceType, tuple[str, ...]] = {
    SourceType.GUARDIAN: ("q", "section", "from_date", "page_size", "max_pages", "base_url"),
    SourceType.NEWSDATA: ("q", "country", "language", "category", "max_pages", "base_url"),
    SourceType.X: ("query", "users", "base_url"),
}
"""Allowed ``params`` keys per API source type (``base_url`` points at a fake/proxy API)."""


API_HOMES = {
    SourceType.GUARDIAN: "https://content.guardianapis.com/",
    SourceType.NEWSDATA: "https://newsdata.io/",
    SourceType.X: "https://api.x.com/",
}
_API_KEY_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


class Source(_Model):
    id: str = Field(
        default="",
        pattern=r"^$|" + _SLUG,
        description="Stable source id shared across profiles; defaults to a slug of `name`.",
    )
    name: str = Field(min_length=1)
    type: SourceType = SourceType.RSS
    url: HttpUrl | None = Field(
        default=None, description="Feed URL (rss/atom); API sources default to the API home."
    )
    enabled: bool = True
    refresh_minutes: int = Field(default=30, ge=1)
    tags: list[str] = Field(default_factory=list)
    aggregator: bool = Field(
        default=False,
        description="The feed links to other publishers (Hacker News): citations show the "
        'article\'s domain with "via <name>".',
    )
    first_party: bool = Field(
        default=False,
        description="The publisher is usually the subject of its own news (company blog, "
        "press releases, SEC/Fed releases): briefs and reports attribute its claims to it.",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="API source query: guardian q/section/from_date/page_size/max_pages, "
        "newsdata q/country/language/category/max_pages, x query/users.",
    )
    api_key: str | None = Field(
        default=None,
        description="Environment variable holding the API key or bearer token (YAML: "
        "``api_key: ${GUARDIAN_API_KEY}``); the value itself is never stored.",
    )
    daily_request_budget: int | None = Field(
        default=None, ge=1, description="Stop calling the API for the (UTC) day after this many."
    )
    max_posts_per_run: int | None = Field(default=None, ge=1, le=100)
    monthly_budget_usd: float | None = Field(
        default=None, gt=0, description="X: hard spend cap per (UTC) month; required to fetch."
    )
    cost_per_read_usd: float = Field(default=0.005, gt=0)
    social: bool = Field(
        default=False, description="Short social posts: no extraction, no reports (X)."
    )

    @model_validator(mode="after")
    def _default_id(self) -> "Source":
        if not self.id:
            self.id = slugify(self.name)
        if not self.id:
            raise ValueError(f"cannot derive an id from source name {self.name!r}; set `id`")
        if self.api_key is not None and not _API_KEY_ENV.match(self.api_key):
            raise ValueError(f"source {self.id!r} api_key must be an environment variable name")
        if self.type in API_TYPES:
            allowed = API_PARAMS[self.type]
            unknown = sorted(set(self.params) - set(allowed))
            if unknown:
                raise ValueError(
                    f"source {self.id!r}: unknown {self.type.value} params {unknown}; "
                    f"allowed: {', '.join(allowed)}"
                )
            self.params = {k: v for k, v in self.params.items() if v not in (None, "", [])}
            if self.type is SourceType.X:
                self.social = True
                if not (self.params.get("query") or self.params.get("users")):
                    raise ValueError(f"source {self.id!r}: x needs params.query or params.users")
                if self.enabled and self.monthly_budget_usd is None:
                    raise ValueError(
                        f"source {self.id!r}: x sources need monthly_budget_usd before they "
                        "can be enabled"
                    )
            if self.url is None:
                self.url = HttpUrl(API_HOMES[self.type])
        elif self.url is None:
            raise ValueError(f"source {self.id!r}: {self.type.value} sources need a url")
        return self

    @property
    def is_api(self) -> bool:
        return self.type in API_TYPES


class MatchMode(StrEnum):
    ANY = "any"
    ALL = "all"


class AlertPriority(StrEnum):
    LOW = "low"
    DEFAULT = "default"
    HIGH = "high"


class MatchField(StrEnum):
    TITLE = "title"
    SUMMARY = "summary"
    TEXT = "text"
    """Extracted article text: evaluated again once extraction finishes."""


class AlertKeyword(_Model):
    """A whole-word keyword. A leading ``$`` (``$AAPL``) is a ticker: it is stored without
    the ``$``, defaults to case-sensitive, and matches both ``AAPL`` and ``$AAPL``."""

    keyword: str = Field(min_length=1)
    case_sensitive: bool = False

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = {"keyword": value}
        if isinstance(value, dict) and isinstance(value.get("keyword"), str):
            keyword = " ".join(value["keyword"].split())
            if keyword.startswith("$") and len(keyword) > 1:
                value = {"case_sensitive": True, **value, "keyword": keyword[1:]}
            else:
                value = {**value, "keyword": keyword}
        return value


class QuietHours(_Model):
    """Local-time window (``alerts.timezone``) in which alerts are held, then sent as a digest.
    ``start`` after ``end`` wraps midnight (``22:00``-``07:00``)."""

    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class AlertRule(_Model):
    """Matches incoming articles by keyword and notifies ``channels`` once per story."""

    name: str = Field(min_length=1)
    enabled: bool = True
    include: list[AlertKeyword] = Field(min_length=1)
    exclude: list[AlertKeyword] = Field(default_factory=list)
    """Any exclude keyword wins over the includes."""
    match: MatchMode = MatchMode.ANY
    """``any`` include keyword, or ``all`` of them."""
    match_in: list[MatchField] = Field(
        default_factory=lambda: [MatchField.TITLE, MatchField.SUMMARY], min_length=1
    )
    sources: list[str] = Field(
        default_factory=list, description="Source names to restrict to; empty = all sources."
    )
    priority: AlertPriority = AlertPriority.DEFAULT
    channels: list[str] = Field(default_factory=list)
    """Channel ids from ``alerts.channels``."""
    quiet_hours: QuietHours | None = None
    max_per_hour: int = Field(default=10, ge=1)
    """Further matches within the hour are held and sent as one digest."""
    notify_on_update: bool = False
    """Also notify (at most hourly) when a new source joins an alerted story."""


class ChannelType(StrEnum):
    WEBHOOK = "webhook"
    NTFY = "ntfy"
    EMAIL = "email"


class EmailTLS(StrEnum):
    NONE = "none"
    STARTTLS = "starttls"
    SSL = "ssl"


SECRET_FIELDS = ("secret", "token", "username", "password")
"""Channel fields that must be ``${ENV_VAR}`` references; only the variable name is kept."""
_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"


class AlertChannel(_Model):
    """A notification target. Secret fields hold environment variable *names* (written
    ``${NTFY_TOKEN}`` in YAML); values are read from the environment when sending."""

    id: str = Field(pattern=_SLUG)
    type: ChannelType
    name: str = ""
    enabled: bool = True
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    # webhook
    url: HttpUrl | None = None
    secret: str | None = Field(default=None, pattern=_ENV_NAME)
    """HMAC-SHA256 key for ``X-Discover-Signature``."""
    # ntfy
    server: HttpUrl = HttpUrl("https://ntfy.sh")
    topic: str | None = None
    token: str | None = Field(default=None, pattern=_ENV_NAME)
    # email
    host: str | None = None
    port: int = Field(default=587, ge=1, le=65535)
    tls: EmailTLS = EmailTLS.STARTTLS
    username: str | None = Field(default=None, pattern=_ENV_NAME)
    password: str | None = Field(default=None, pattern=_ENV_NAME)
    sender: str | None = None
    to: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_type_fields(self) -> "AlertChannel":
        if not self.name:
            self.name = self.id
        missing = {
            ChannelType.WEBHOOK: [] if self.url else ["url"],
            ChannelType.NTFY: [] if self.topic else ["topic"],
            ChannelType.EMAIL: [
                f
                for f, v in (("host", self.host), ("sender", self.sender), ("to", self.to))
                if not v
            ],
        }[self.type]
        if missing:
            raise ValueError(f"{self.type} channel {self.id!r} needs: {', '.join(missing)}")
        return self


class AlertsConfig(_Model):
    """Per-profile alert settings (read from YAML on every start) and channels (seeded once)."""

    channels: list[AlertChannel] = Field(default_factory=list)
    alert_max_age_hours: float = Field(default=6, gt=0)
    """Only articles published this recently fire: a first start or backfill never floods."""
    notification_retention_days: int = Field(default=30, ge=1)
    max_attempts: int = Field(default=5, ge=1, le=20)
    timezone: str = "UTC"
    """IANA zone for quiet hours and notification times."""
    public_url: str = "http://localhost:8000"
    """Base URL for story links in notifications."""

    @model_validator(mode="after")
    def _check(self) -> "AlertsConfig":
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValueError(f"unknown timezone {self.timezone!r}") from exc
        ids = [c.id for c in self.channels]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate alert channel ids")
        self.public_url = self.public_url.rstrip("/")
        return self


GDELT_MIN_INTERVAL = 5.0
GDELT_DEFAULT_INTERVAL = 10.0
"""GDELT documents one request per 5 s but rate-limits at 6 s spacing in practice."""
TINYFISH_MIN_INTERVAL = 2.0
"""TinyFish allows 30 requests / minute per key by default."""
"""GDELT DOC 2.0 asks for at most one request every 5 seconds."""


class SearchProviderConfig(_Model):
    """One web-search provider used by enrichment (``enrichment.providers``)."""

    name: str = Field(pattern=r"^(tinyfish|gdelt|searxng|guardian|newsdata|brave|exa)$")
    base_url: str | None = None
    """API root (required for ``searxng``: your instance, with ``json`` in search.formats)."""
    api_key: str | None = None
    """Name of the environment variable holding the key (tinyfish / guardian / newsdata /
    brave)."""
    budget_source: str | None = None
    """Share the daily request budget recorded for this source id (e.g. a guardian feed)."""
    daily_request_budget: int | None = Field(default=None, ge=0)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    cost_per_request_usd: float = Field(default=0.0, ge=0)
    min_interval_seconds: float | None = Field(default=None, ge=0)
    """Seconds between requests, shared by the app and the CLI tools. Unset: 10 for gdelt
    (it answers 429 "one every 5 seconds", and often at 6 s too; lower values are raised to
    5), 2 for tinyfish
    (30 requests / minute; lower values are raised to 2 unless ``override_min_interval``),
    1 otherwise."""
    override_min_interval: bool = False
    """Allow a tinyfish ``min_interval_seconds`` below 2 (a key with a higher rate limit)."""
    search_type: str = Field(default="news", pattern=r"^(news|web)$")
    """tinyfish ``domain_type``: ``news`` (publisher + date) or ``web``."""
    max_results: int = Field(default=10, ge=1, le=50)
    timeout_seconds: float = Field(default=15.0, gt=0)

    @model_validator(mode="after")
    def _interval(self) -> "SearchProviderConfig":
        if self.name == "gdelt":
            low = self.min_interval_seconds
            if low is not None and low < GDELT_MIN_INTERVAL:
                logger.warning(
                    "enrichment provider gdelt: min_interval_seconds %s is below GDELT's limit; "
                    "using %s",
                    low,
                    GDELT_MIN_INTERVAL,
                )
            if low is None:
                self.min_interval_seconds = GDELT_DEFAULT_INTERVAL
            else:
                self.min_interval_seconds = max(low, GDELT_MIN_INTERVAL)
        elif self.name == "tinyfish":
            low = self.min_interval_seconds
            if low is None:
                self.min_interval_seconds = TINYFISH_MIN_INTERVAL
            elif low < TINYFISH_MIN_INTERVAL and not self.override_min_interval:
                logger.warning(
                    "enrichment provider tinyfish: min_interval_seconds %s is below its 30 "
                    "requests/minute limit; using %s (set override_min_interval to allow it)",
                    low,
                    TINYFISH_MIN_INTERVAL,
                )
                self.min_interval_seconds = TINYFISH_MIN_INTERVAL
        elif self.min_interval_seconds is None:
            self.min_interval_seconds = 1.0
        return self


class QueryMode(StrEnum):
    HEURISTIC = "heuristic"
    LLM = "llm"


def _default_providers() -> list[SearchProviderConfig]:
    # GDELT rate-limits in practice (32 of 37 requests on a live eval, no useful results):
    # it stays available as an opt-in fallback but is not a default provider.
    return [SearchProviderConfig(name="tinyfish")]


DEFAULT_SKIP_TITLE_PATTERNS = [
    r"^Show HN\b",
    r"^Ask HN\b",
    r"^Tell HN\b",
    r"^Opinion\b",
    r"^Letters\b",
]
DEFAULT_STRIP_TITLE_PREFIXES = [
    "Show HN",
    "Ask HN",
    "Tell HN",
    "Launch HN",
    "Analysis",
    "Revealed",
    "Exclusive",
    "Opinion",
    "Explainer",
    "Live",
    "Watch",
    "Breaking",
    "Update",
    "Comment",
    "Review",
    "Interview",
    "Factbox",
    "Podcast",
    "Video",
]


DEFAULT_SKIP_URL_PATTERNS = [
    "/commentisfree/",
    "/opinion/",
    "/opinions/",
    "/comment/",
    "/columnists/",
    "/blogs/",
    "/live/",
]
DEFAULT_PUBLISHER_ALIASES = {
    "bbc.co.uk": "bbc.com",
    "guardian.co.uk": "theguardian.com",
}
"""Registrable domains that are the same publisher, keyed to the canonical name
(``bbc.co.uk`` -> ``bbc.com``). Applied after ``www.``/``amp.``/``m.`` subdomains
collapse to the registrable domain."""


class ExtraRulesConfig(_Model):
    """Acceptance rules beyond rule A (text similarity) and rule B (strong snippet):
    real coverage whose extracted text scores just below ``min_text_similarity`` but
    shares enough of the story's named entities, or whose headline matches closely."""

    enabled: bool = True
    rule_c_entities: int = Field(default=5, ge=1, le=20)
    """Rule C: at least this many shared names/numbers ..."""
    rule_c_text: float = Field(default=0.25, ge=0, le=1)
    """... and extracted text at least this similar."""
    rule_d_title: float = Field(default=0.55, ge=0, le=1)
    """Rule D: the hit's title this similar to the story headlines ..."""
    rule_d_entities: int = Field(default=3, ge=1, le=20)
    """... plus this many shared entities ..."""
    rule_d_text: float = Field(default=0.22, ge=0, le=1)
    """... and extracted text at least this similar."""


class EnrichmentQualityConfig(_Model):
    """Source-quality gate: flag (never reject) accepted candidates whose domain looks
    like a content farm, and hard-deny confirmed ones. Flagged members are still shown
    and cited — the relevance checks already passed — but counted in Settings > Pipeline
    and badged "source quality uncertain" on the story page."""

    suspicious_tlds: list[str] = Field(default_factory=lambda: list(DEFAULT_SUSPICIOUS_TLDS))
    """Domain endings commonly used by scraper/SEO pages; accepted hits on them are flagged."""
    suspicious_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SUSPICIOUS_PATTERNS)
    )
    """Regexes on the registrable domain name for keyword-stuffed farm names
    (``tech-insider``, ``news24``); accepted hits matching one are flagged."""
    deny_domains: list[str] = Field(default_factory=lambda: list(DEFAULT_DENY_DOMAINS))
    """Confirmed content-farm domains — hard-denied like ``enrichment.deny_domains``
    and the DB block list; review the pipeline panel's flagged domains to fill it.
    Defaults to farms confirmed in live evaluations (``DEFAULT_DENY_DOMAINS``);
    a YAML list replaces the defaults entirely."""

    @model_validator(mode="after")
    def _check(self) -> "EnrichmentQualityConfig":
        self.suspicious_tlds = [t.strip().lower().lstrip(".") for t in self.suspicious_tlds if t]
        self.deny_domains = [d.strip().lower().lstrip(".") for d in self.deny_domains if d]
        for pattern in self.suspicious_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid suspicious_patterns entry {pattern!r}: {exc}") from None
        return self


class EnrichmentDepthConfig(_Model):
    """The deep tier (``enrichment.depth``): important stories get more queries, pages and
    locations so they end up with many distinct outlets. Stories qualify when they have
    at least ``deep_min_publishers`` distinct feed publishers, a priority above
    ``deep_min_priority``, or an alert that fired; everything else keeps the standard
    (cheap) behavior."""

    enabled: bool = True
    """Off: every story is enriched on the standard tier."""
    deep_min_publishers: int = Field(default=2, ge=1)
    """Deep tier for stories whose feed members already come from this many distinct
    publishers (set very high to rely on priority / alerts instead)."""
    deep_min_priority: int | None = Field(default=None, ge=0)
    """Deep tier for stories whose ``priority`` is above this (None: priority alone
    never admits)."""
    deep_on_alert: bool = True
    """Deep tier for stories an alert rule already fired on."""
    target_sources: int = Field(default=12, ge=2, le=30)
    """Distinct publishers a deep-tier story is searched for up to."""
    max_added_sources: int = Field(default=12, ge=1, le=30)
    """Web members added per pass on the deep tier."""
    max_queries_per_story: int = Field(default=4, ge=1, le=8)
    """Query variants per provider (core entities, a paraphrase, key numbers, the main
    action phrase)."""
    pages: int = Field(default=2, ge=1, le=10)
    """Result pages fetched per query (TinyFish ``page`` 0..pages-1); a page with no
    usable candidates stops early."""
    locations: list[str] = Field(default_factory=lambda: ["US", "GB", "AU", "CA", "IN"])
    """Country codes the strongest query is repeated once each for
    (TinyFish ``location``); empty: no location fan-out."""
    daily_request_budget: int | None = Field(default=1500, ge=0)
    """Search requests/day spent on the deep tier across all providers, on top of each
    provider's own budget; None: unlimited."""
    ensure_groups: list[str] = Field(default_factory=list)
    """Outlet groups (``config/outlets.yaml``) a deep story should have a source from:
    missing groups get one targeted query each (``include_domains``)."""
    max_group_queries: int = Field(default=3, ge=0, le=10)
    """At most this many ensure-groups queries per story."""

    @model_validator(mode="after")
    def _check(self) -> "EnrichmentDepthConfig":
        self.locations = [loc.strip().upper() for loc in self.locations if loc.strip()]
        for loc in self.locations:
            if len(loc) != 2 or not loc.isalpha():
                raise ValueError(f"invalid depth.locations entry {loc!r}: country codes only")
        self.ensure_groups = [g.strip() for g in self.ensure_groups if g.strip()]
        return self


class EnrichmentConfig(_Model):
    """Web-search enrichment: find other publishers' coverage of single-sourced stories."""

    enabled: bool = False
    providers: list[SearchProviderConfig] = Field(default_factory=_default_providers)
    target_sources: int = Field(default=3, ge=2, le=20)
    """Stories with fewer distinct publishers than this are searched."""
    max_age_hours: float = Field(default=48, gt=0)
    max_added_sources: int = Field(default=3, ge=1, le=10)
    wait_seconds: float = Field(default=180, ge=0)
    """How long a new story's brief waits for enrichment before going ahead without it."""
    concurrency: int = Field(default=2, ge=1, le=8)
    # Similarity thresholds are starting points from one live evaluation (Ubuntu 26.10 via
    # TinyFish); tune them with ``python -m app.enrich.evaluate`` as more live data comes in.
    min_snippet_similarity: float = Field(default=0.25, ge=0, le=1)
    """Hit title + snippet vs. the story headlines (TF-IDF cosine)."""
    min_text_similarity: float = Field(default=0.35, ge=0, le=1)
    """Extracted page text vs. the story's headlines and member text (TF-IDF cosine)."""
    min_shared_entities: int = Field(default=2, ge=0, le=10)
    min_snippet_strong: float = Field(default=0.45, ge=0, le=1)
    """Rule B: a hit whose snippet is this similar can be accepted with weaker page text."""
    min_text_floor: float = Field(default=0.25, ge=0, le=1)
    """Rule B: extracted text must still reach this similarity (and share the entities)."""
    link_only: bool = False
    """Candidates with a strong snippet (``min_snippet_strong``) and ``min_shared_entities``
    whose page cannot be fetched (failed / blocked / paywalled, never robots-disallowed)
    are stored as ``text_status = none`` members: shown as \"also reported by (link only)\",
    never summarized, cited or counted in the word budget."""
    headline_only_title_similarity: float = Field(default=0.50, ge=0, le=1)
    """Headline-only members: an unfetchable candidate (never robots-disallowed) whose
    title is at least this similar to the story headlines is stored as a
    ``text_status = none`` member — shown in coverage, never summarized or cited."""
    headline_only_min_entities: int = Field(default=2, ge=0, le=10)
    """Headline-only members also need this many shared entities (names/numbers) between
    the hit's title and the story headlines."""
    max_time_gap_hours: float = Field(default=72, gt=0)
    duplicate_overlap: float = Field(default=0.6, gt=0, le=1)
    """Shingle overlap at which a candidate counts as a copy (syndicated wire text)."""
    deny_domains: list[str] = Field(default_factory=list)
    """Blocked candidate domains; the admin "Block this domain" action persists more in
    the database (both apply, the DB wins when they disagree)."""
    social_domains: list[str] = Field(default_factory=list)
    """Extra hosts treated as social (never enrichment members); the built-in list
    already covers the common networks and mastodon-style hosts."""
    aggregator_domains: list[str] = Field(default_factory=list)
    """Extra hosts treated as aggregators/scrapers (never enrichment members)."""
    publisher_aliases: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_PUBLISHER_ALIASES)
    )
    """Domains that are the same publisher (``bbc.co.uk`` -> ``bbc.com``): they count as
    one in the same-domain and publisher-count checks, before any fetch."""
    extra_rules: ExtraRulesConfig = Field(default_factory=ExtraRulesConfig)
    quality: EnrichmentQualityConfig = Field(default_factory=EnrichmentQualityConfig)
    depth: EnrichmentDepthConfig = Field(default_factory=EnrichmentDepthConfig)
    """The deep tier: more queries, pages, locations and a bigger source target for
    stories that qualify (publishers / priority / alerts)."""
    reenrich_hours: float = Field(default=24, ge=0)
    query_mode: QueryMode = QueryMode.HEURISTIC
    resummarize_on_enrichment: bool = True
    """Sources found after a story was published re-generate it once."""
    cache_hours: float = Field(default=24, ge=0)
    language: str = "en"
    max_candidates: int = Field(default=10, ge=1, le=50)
    """Candidates fetched per story after the snippet filters."""
    max_queries_per_story: int = Field(default=2, ge=1, le=4)
    """Requests per provider per story: the strict query, then a looser one on 0 hits."""
    min_title_similarity: float = Field(default=0.30, ge=0, le=1)
    """Hits without a snippet (GDELT): title vs. story headlines, plus a shared entity."""
    empty_cache_minutes: float = Field(default=10, ge=0)
    """Empty results are cached this long at most (0: not cached)."""
    second_pass_hours: float = Field(default=3, ge=0)
    """Stories still under ``target_sources`` are searched once more this long after the
    first pass (0: off)."""
    late_regen_max_per_hour: int = Field(default=6, ge=0)
    """Published stories re-generated because of late sources, at most this many per hour
    (the rest keep their new sources for their next regeneration)."""
    skip_title_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SKIP_TITLE_PATTERNS)
    )
    """Regexes (case-insensitive) on the headline: matching stories are not searched
    (``enrich_reason = skipped:pattern``); other outlets rarely cover them."""
    strip_title_prefixes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_STRIP_TITLE_PREFIXES)
    )
    """Headline boilerplate (``Exclusive:``, ``Analysis -`` ...) removed before building
    queries."""
    min_signal_tokens: int = Field(default=1, ge=0, le=10)
    """Stories whose headlines have fewer distinct names / numbers than this are not
    searched (``skipped:no_signal``; 0: off)."""
    skip_url_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_SKIP_URL_PATTERNS))
    """Substrings (case-insensitive) on the anchor article's URL path: opinion columns,
    features and live blogs are searched and never match (``skipped:url_pattern``)."""

    @model_validator(mode="after")
    def _check(self) -> "EnrichmentConfig":
        names = [p.name for p in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("duplicate enrichment providers")
        for p in self.providers:
            if p.name == "searxng" and not p.base_url:
                raise ValueError("the searxng provider needs base_url")
        self.deny_domains = [d.strip().lower().lstrip(".") for d in self.deny_domains if d]
        self.social_domains = [d.strip().lower().lstrip(".") for d in self.social_domains if d]
        self.aggregator_domains = [
            d.strip().lower().lstrip(".") for d in self.aggregator_domains if d
        ]
        self.publisher_aliases = {
            k.strip().lower().lstrip("."): v.strip().lower().lstrip(".")
            for k, v in self.publisher_aliases.items()
            if k and v
        }
        for pattern in self.skip_title_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid skip_title_patterns entry {pattern!r}: {exc}") from None
        return self


class ContextConfig(_Model):
    """Background-and-context links (``app.context``): 1-3 pages from other sources that
    add history, context, data or opposing views — never coverage of the same event, never
    a source of the story, never summarized or alerted on. Domain rules (``deny_domains``,
    ``social_domains``, ``aggregator_domains``, ``publisher_aliases``) come from the
    profile's ``enrichment`` block."""

    enabled: bool = False
    providers: list[SearchProviderConfig] = Field(default_factory=_default_providers)
    """Same provider registry/budget keys as enrichment (``search:tinyfish`` is shared);
    ``exa`` additionally supports ``find_similar`` on the anchor URL."""
    max_angles: int = Field(default=3, ge=1, le=8)
    """Search angles (history / data / opposing view ...) generated per story."""
    max_items: int = Field(default=3, ge=1, le=10)
    """Context links kept per story."""
    max_per_domain: int = Field(default=1, ge=1, le=10)
    """Accepted links per story from a single domain; extra accepted candidates on an
    already-kept domain are rejected ``domain_cap``."""
    min_words: int = Field(default=300, ge=0)
    """Extracted pages below this are rejected."""
    similarity_floor: float = Field(default=0.10, ge=0, le=1)
    """Below: the page is off topic."""
    similarity_ceiling: float = Field(default=0.45, ge=0, le=1)
    """At/above: the page is coverage of the same event (that's enrichment's job)."""
    min_shared_entities: int = Field(default=1, ge=0, le=10)
    min_shared_entities_heuristic: int = Field(default=2, ge=0, le=10)
    """Shared entities required for hits found via a heuristic (non-LLM) angle."""
    min_priority: int = Field(default=0, ge=0)
    """Eligible when ``story.priority`` is above this ..."""
    min_publishers: int = Field(default=2, ge=1)
    """... or the story has at least this many distinct publishers."""
    daily_request_budget: int | None = Field(default=30, ge=0)
    """Context searches/day across all providers, on top of each provider's own budget."""
    llm_judge: bool = True
    """LLM scores each surviving page 0/1/2 for added context (kept only at 2; off: keep
    pages without a note). Failures reject the candidate — nothing is fabricated."""
    reject_flagged: bool = True
    """Reject candidates the shared source-quality gate flags (content-farm domains or
    SEO-template titles) instead of keeping them with a ``flagged`` marker like
    enrichment does."""
    max_opinion: int = Field(default=1, ge=0, le=10)
    """Opinion/column links kept per story (``context_kind = opinion``; 0: none)."""
    same_event: "SameEventRules" = Field(default_factory=lambda: SameEventRules())
    """Deterministic same-event signals run before the judge (shared numbers, a
    stricter band for contemporaneous pages, title entities + an event word)."""
    language: str = "en"
    cache_hours: float = Field(default=24, ge=0)
    empty_cache_minutes: float = Field(default=10, ge=0)
    max_age_hours: float = Field(default=168, gt=0)
    """Stories are eligible while published within this window."""
    concurrency: int = Field(default=2, ge=1, le=8)

    @model_validator(mode="after")
    def _check(self) -> "ContextConfig":
        names = [p.name for p in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("duplicate context providers")
        for p in self.providers:
            if p.name == "searxng" and not p.base_url:
                raise ValueError("the searxng provider needs base_url")
        if self.similarity_floor >= self.similarity_ceiling:
            raise ValueError("context.similarity_floor must be below similarity_ceiling")
        return self


class TopicConfig(_Model):
    """A feed tab: articles matching any `include` keyword and no `exclude` keyword."""

    name: str = Field(min_length=1)
    include: list[str] = Field(
        default_factory=list, description="Whole-word, case-insensitive; empty = all articles."
    )
    exclude: list[str] = Field(default_factory=list)
    sources: list[str] = Field(
        default_factory=list, description="Source names to restrict to; empty = all sources."
    )
    enabled: bool = True


class Profile(_Model):
    id: str = Field(pattern=_SLUG)
    name: str = Field(min_length=1)
    description: str = ""
    topics: list[TopicConfig] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    alert_rules: list[AlertRule] = Field(default_factory=list)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    llm: LLMConfig
    summaries: SummariesConfig = Field(default_factory=SummariesConfig)
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)

    @model_validator(mode="after")
    def _check_references(self) -> "Profile":
        names = [s.name for s in self.sources]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate source names: {sorted(dupes)}")
        known = set(names)
        topic_names = [t.name.casefold() for t in self.topics]
        if len(topic_names) != len(set(topic_names)):
            raise ValueError("duplicate topic names")
        for topic in self.topics:
            unknown = set(topic.sources) - known
            if unknown:
                raise ValueError(
                    f"topic {topic.name!r} references unknown sources: {sorted(unknown)}"
                )
        rule_names = [r.name.casefold() for r in self.alert_rules]
        if len(rule_names) != len(set(rule_names)):
            raise ValueError("duplicate alert rule names")
        channel_ids = {c.id for c in self.alerts.channels}
        for rule in self.alert_rules:
            unknown = set(rule.sources) - known
            if unknown:
                raise ValueError(
                    f"alert rule {rule.name!r} references unknown sources: {sorted(unknown)}"
                )
            unknown = set(rule.channels) - channel_ids
            if unknown:
                raise ValueError(
                    f"alert rule {rule.name!r} references unknown channels: {sorted(unknown)}"
                )
        return self


class NoImageStyle(StrEnum):
    LOGO = "logo"
    """Publisher icon on a tint derived from it, with the publisher name."""
    TEXT_ONLY = "text_only"
    """Compact card without an image area."""
    GRADIENT = "gradient"
    """Per-publisher gradient with the publisher name (the pre-image-support look)."""


class ImagesConfig(_Model):
    """Top-level ``images:`` block: card images (``app/images``)."""

    cache: bool = Field(
        default=True,
        description="Download validated images once, serve resized WebP from /img/{key}.",
    )
    max_mb: float = Field(default=500, gt=0, description="LRU cap of the image cache.")
    no_image_style: NoImageStyle = NoImageStyle.LOGO
    backfill_days: float = Field(
        default=7, ge=0, description="Backfill images of stories published this recently."
    )
    backfill_per_run: int = Field(default=20, ge=1, description="Articles per backfill run.")
    backfill_interval_minutes: float = Field(default=10, gt=0)
    per_run: int = Field(default=50, ge=1, description="New articles checked per pipeline run.")
    timeout_seconds: float = Field(default=10, gt=0)
    max_bytes: int = Field(default=3 * 1024 * 1024, gt=0)
    min_width: int = Field(default=320, ge=1)
    min_height: int = Field(default=180, ge=1)
    tiny_feed_width: int = Field(
        default=640, ge=1, description="Prefer the page image over a feed image narrower."
    )
    cache_width: int = Field(default=640, ge=16)
    generic_threshold: int = Field(
        default=3, ge=2, description="Same image URL on this many articles of one domain."
    )
    max_pixels: int = Field(
        default=40_000_000, ge=1, description="Decompression-bomb guard (Pillow)."
    )


class AppConfig(_Model):
    profiles: list[Profile] = Field(default_factory=list)
    images: ImagesConfig = Field(default_factory=ImagesConfig)

    @model_validator(mode="after")
    def _unique_ids(self) -> "AppConfig":
        ids = [p.id for p in self.profiles]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate profile ids: {sorted(dupes)}")
        seen: dict[str, Source] = {}
        for profile in self.profiles:
            for source in profile.sources:
                other = seen.setdefault(source.id, source)
                if (other.url, other.type) != (source.url, source.type):
                    raise ValueError(
                        f"source id {source.id!r} is used for different feeds; "
                        "give one of them an explicit `id`"
                    )
        return self

    def all_sources(self, *, enabled_only: bool = True) -> list[Source]:
        """Unique sources across all profiles, keyed by source id (first definition wins)."""
        unique: dict[str, Source] = {}
        for profile in self.profiles:
            for source in profile.sources:
                if source.enabled or not enabled_only:
                    unique.setdefault(source.id, source)
        return list(unique.values())

    def get_source(self, source_id: str) -> Source | None:
        return next((s for s in self.all_sources(enabled_only=False) if s.id == source_id), None)

    def get_profile(self, profile_id: str) -> Profile | None:
        return next((p for p in self.profiles if p.id == profile_id), None)


def _expand_env(value: Any) -> Any:
    if isinstance(value, _NoExpand):
        return str(value)
    if isinstance(value, str):
        return _ENV_VAR.sub(lambda m: os.environ.get(m.group(1)) or m.group(2) or "", value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _blank_to_none(data: Any) -> Any:
    """Treat an empty ``api_key`` (e.g. an unset env var) as "no key"."""
    profiles = data.get("profiles") if isinstance(data, dict) else None
    for profile in profiles or []:
        llm = profile.get("llm") if isinstance(profile, dict) else None
        for role in (llm or {}).values() if isinstance(llm, dict) else ():
            if isinstance(role, dict) and role.get("api_key") == "":
                role["api_key"] = None
    return data


_SECRET_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def secret_ref_name(value: Any, where: str) -> Any:
    """``${NTFY_TOKEN}`` -> ``NTFY_TOKEN``; literal secrets are rejected so they never reach
    the DB."""
    if value is None or value == "":
        return None
    match = _SECRET_REF.match(str(value).strip())
    if not match:
        raise ValueError(f"{where} must be an environment reference like ${{NTFY_TOKEN}}")
    return match.group(1)


def _protect_secret_refs(raw: Any) -> Any:
    """Keep channel secrets as variable names instead of expanding them into the config."""
    if not isinstance(raw, dict):
        return raw
    for profile in raw.get("profiles") or []:
        for source in profile.get("sources") or [] if isinstance(profile, dict) else []:
            if isinstance(source, dict) and source.get("api_key") not in (None, ""):
                source["api_key"] = _NoExpand(
                    secret_ref_name(source["api_key"], f"source {source.get('name')!r} api_key")
                )
        alerts = profile.get("alerts") if isinstance(profile, dict) else None
        for channel in (alerts or {}).get("channels") or [] if isinstance(alerts, dict) else []:
            if not isinstance(channel, dict):
                continue
            for key in SECRET_FIELDS:
                if key in channel:
                    channel[key] = _NoExpand(
                        secret_ref_name(channel[key], f"channel {channel.get('id')!r} {key}")
                    )
    return raw


class _NoExpand(str):
    """A string _expand_env leaves alone."""


def parse_config(text: str) -> AppConfig:
    raw = _protect_secret_refs(yaml.safe_load(text) or {})
    return AppConfig.model_validate(_blank_to_none(_expand_env(raw)))


def load_config(path: str | Path) -> AppConfig:
    return parse_config(Path(path).read_text(encoding="utf-8"))
