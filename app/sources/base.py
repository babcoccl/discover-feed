"""Generic source adapter interface and registry.

Callers only ever do ``create_adapter(source.type, client).fetch(source, state)``, so new
adapter kinds (e.g. Guardian / NewsData APIs) just need to subclass ``SourceAdapter`` and
``@register`` themselves for a ``SourceType``.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from app.config import Source, SourceType


@dataclass
class RawArticle:
    """One item as provided by a source, before normalization."""

    url: str | None
    title: str | None = None
    summary: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    published_raw: str | None = None
    image_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchState:
    """Per-source state persisted between runs.

    Adapters read it to make conditional requests and update it in place: new validators
    after a successful fetch, and ``not_modified=True`` when the source reported no changes.
    """

    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


class FetchError(Exception):
    """A source could not be fetched or parsed."""


class UnsupportedSourceType(FetchError):
    pass


class SourceAdapter(ABC):
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    @abstractmethod
    async def fetch(self, source: Source, state: FetchState | None = None) -> list[RawArticle]:
        """Fetch the current items of ``source``. Raises (ideally ``FetchError``) on failure.

        ``state`` carries the HTTP validators (ETag / Last-Modified) persisted for this source.
        HTTP feed adapters send them as conditional-request headers, store the new validators
        on it, and set ``state.not_modified = True`` (returning ``[]``) on a 304. Adapters with
        no conditional-request support (e.g. JSON APIs) may ignore ``state`` entirely; the
        caller then always treats the returned items as a full fetch and deduplicates them.
        """


_REGISTRY: dict[SourceType, type[SourceAdapter]] = {}


def register(
    *types: SourceType,
) -> Callable[[type[SourceAdapter]], type[SourceAdapter]]:
    def decorator(cls: type[SourceAdapter]) -> type[SourceAdapter]:
        for source_type in types:
            _REGISTRY[source_type] = cls
        return cls

    return decorator


def create_adapter(source_type: SourceType, client: httpx.AsyncClient) -> SourceAdapter:
    try:
        cls = _REGISTRY[source_type]
    except KeyError:
        raise UnsupportedSourceType(
            f"no adapter registered for source type {source_type!r}"
        ) from None
    return cls(client)
