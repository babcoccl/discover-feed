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
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, model_validator

_SLUG = r"^[a-z0-9][a-z0-9_-]*$"
_ENV_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMEndpoint(_Model):
    """An OpenAI-compatible chat completions endpoint."""

    base_url: HttpUrl
    api_key: SecretStr | None = None
    model: str = Field(min_length=1)
    temperature: float = Field(default=0.2, ge=0, le=2)
    timeout_seconds: float = Field(default=60, gt=0)


class SourceType(StrEnum):
    RSS = "rss"
    ATOM = "atom"
    WEB = "web"
    API = "api"


class Source(_Model):
    name: str = Field(min_length=1)
    type: SourceType = SourceType.RSS
    url: HttpUrl
    enabled: bool = True
    poll_interval_minutes: int = Field(default=60, ge=1)
    tags: list[str] = Field(default_factory=list)


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


class Profile(_Model):
    id: str = Field(pattern=_SLUG)
    name: str = Field(min_length=1)
    description: str = ""
    topics: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    alert_rules: list[AlertRule] = Field(default_factory=list)
    llm: LLMEndpoint

    @model_validator(mode="after")
    def _check_references(self) -> "Profile":
        names = [s.name for s in self.sources]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate source names: {sorted(dupes)}")
        known = set(names)
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
        return self

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
        if isinstance(llm, dict) and llm.get("api_key") == "":
            llm["api_key"] = None
    return data


def parse_config(text: str) -> AppConfig:
    raw = yaml.safe_load(text) or {}
    return AppConfig.model_validate(_blank_to_none(_expand_env(raw)))


def load_config(path: str | Path) -> AppConfig:
    return parse_config(Path(path).read_text(encoding="utf-8"))
