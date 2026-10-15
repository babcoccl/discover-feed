"""Search provider interface and registry.

A provider turns one query into ``SearchHit``s. Keys come from the environment variable
named by ``api_key`` (or the provider's default) and are only ever sent in headers; every
error message is scrubbed of them. Budgets, rate limits, caching and health live in
``SearchService`` so providers stay small.
"""

import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import SearchProviderConfig
from app.sources.api import JsonApiAdapter
from app.sources.base import FetchError, Quota

LANGUAGES = {
    "english": "en",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "portuguese": "pt",
    "dutch": "nl",
    "russian": "ru",
    "chinese": "zh",
    "japanese": "ja",
    "arabic": "ar",
}
LANGUAGE_NAMES = {v: k for k, v in LANGUAGES.items()}


def language_code(value: Any) -> str | None:
    """``"English"`` / ``"en"`` / ``"en-US"`` -> ``"en"`` (None when unknown/empty)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text in LANGUAGES:
        return LANGUAGES[text]
    code = text.split("-", 1)[0].split("_", 1)[0]
    return code if len(code) == 2 else None


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str = ""
    domain: str = ""
    published_at: datetime | None = None
    image_url: str | None = None
    language: str | None = None

    def __post_init__(self) -> None:
        self.domain = (self.domain or host_of(self.url)).lower().removeprefix("www.")
        if self.published_at is not None and self.published_at.tzinfo is None:
            self.published_at = self.published_at.replace(tzinfo=UTC)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["published_at"] = self.published_at.isoformat() if self.published_at else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchHit":
        at = data.get("published_at")
        return cls(
            url=data["url"],
            title=data.get("title") or "",
            snippet=data.get("snippet") or "",
            domain=data.get("domain") or "",
            published_at=datetime.fromisoformat(at) if at else None,
            image_url=data.get("image_url"),
            language=data.get("language"),
        )


class ProviderTimeout(FetchError):
    """The provider did not answer within ``timeout_seconds``."""


def is_timeout(exc: Exception) -> bool:
    """Timeouts from our providers or from ``JsonApiAdapter.get_json`` (``ReadTimeout`` ...)."""
    return isinstance(exc, ProviderTimeout) or "Timeout:" in str(exc)


class _JsonClient(JsonApiAdapter):
    """``JsonApiAdapter.get_json`` (429/Retry-After, key scrubbing, quota errors) for a
    search API that is not a feed source."""

    def __init__(self, client: httpx.AsyncClient, provider: str) -> None:
        super().__init__(client)
        self.provider = provider

    async def fetch(self, source, state=None):  # pragma: no cover - not a feed
        raise NotImplementedError


class SearchProvider(ABC):
    name: str = ""
    default_key_env: str | None = None
    needs_key: bool = False

    def __init__(self, config: SearchProviderConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self.client = client
        self.last_raw: str = ""

    @property
    def key_env(self) -> str | None:
        return self.config.api_key or self.default_key_env

    def key(self) -> str:
        if not self.needs_key:
            return ""
        name = self.key_env
        value = os.environ.get(name or "", "").strip()
        if not value:
            raise FetchError(f"{self.name}: environment variable {name} is not set")
        return value

    def secrets(self) -> list[str]:
        name = self.key_env
        value = os.environ.get(name, "").strip() if name else ""
        return [value] if value else []

    def json_client(self) -> JsonApiAdapter:
        return _JsonClient(self.client, self.name)

    def remember(self, body: Any) -> None:
        try:
            self.last_raw = json.dumps(body, ensure_ascii=False)[:4000]
        except (TypeError, ValueError):
            self.last_raw = str(body)[:4000]

    @abstractmethod
    async def search(
        self, query: str, since: datetime, language: str | None, limit: int, quota: Quota
    ) -> list[SearchHit]:
        """Hits for ``query`` published after ``since`` (best effort per provider).
        ``quota.before_request()`` is called once per HTTP request (budgets)."""


PROVIDERS: dict[str, type[SearchProvider]] = {}


def register(name: str) -> Callable[[type[SearchProvider]], type[SearchProvider]]:
    def decorator(cls: type[SearchProvider]) -> type[SearchProvider]:
        cls.name = name
        PROVIDERS[name] = cls
        return cls

    return decorator


def create_provider(config: SearchProviderConfig, client: httpx.AsyncClient) -> SearchProvider:
    from app.search import providers  # noqa: F401  (registers the built-in providers)

    try:
        cls = PROVIDERS[config.name]
    except KeyError:
        raise FetchError(f"unknown search provider {config.name!r}") from None
    return cls(config, client)
