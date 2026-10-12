"""Profile configuration models and YAML loader.

A profile bundles the topics, sources, keywords, alert rules and LLM endpoint
for one feed. Values in the YAML file may reference environment variables with
``${VAR}`` or ``${VAR:-default}`` (default used when unset or empty) so secrets
never need to live in the file.
"""

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
    url: HttpUrl
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

    @model_validator(mode="after")
    def _default_id(self) -> "Source":
        if not self.id:
            self.id = slugify(self.name)
        if not self.id:
            raise ValueError(f"cannot derive an id from source name {self.name!r}; set `id`")
        return self


class MatchMode(StrEnum):
    ANY = "any"
    ALL = "all"


class AlertRule(_Model):
    name: str = Field(min_length=1)
    keywords: list[str] = Field(min_length=1)
    match: MatchMode = MatchMode.ANY
    min_score: float = Field(default=0.0, ge=0, le=1)
    sources: list[str] = Field(
        default_factory=list, description="Source names to restrict to; empty = all sources."
    )
    enabled: bool = True


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
    llm: LLMConfig
    summaries: SummariesConfig = Field(default_factory=SummariesConfig)

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
        for rule in self.alert_rules:
            unknown = set(rule.sources) - known
            if unknown:
                raise ValueError(
                    f"alert rule {rule.name!r} references unknown sources: {sorted(unknown)}"
                )
        return self


class AppConfig(_Model):
    profiles: list[Profile] = Field(default_factory=list)

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


def parse_config(text: str) -> AppConfig:
    raw = yaml.safe_load(text) or {}
    return AppConfig.model_validate(_blank_to_none(_expand_env(raw)))


def load_config(path: str | Path) -> AppConfig:
    return parse_config(Path(path).read_text(encoding="utf-8"))
