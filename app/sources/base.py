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
    text: str | None = None
    """Full body text supplied by the source (Guardian): stored as-is, extraction skipped."""
    social: bool = False
    """A short social post (X): never extracted, never reported."""


@dataclass
class FetchState:
    """Per-source state persisted between runs.

    Adapters read it to make conditional requests and update it in place: new validators
    after a successful fetch, and ``not_modified=True`` when the source reported no changes.
    """

    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    quota: "Quota | None" = None
    """Request/spend counters for API sources; adapters check it before every call."""


class FetchError(Exception):
    """A source could not be fetched or parsed."""


class UnsupportedSourceType(FetchError):
    pass


class QuotaExhausted(FetchError):
    """A local budget (requests per day, spend per month) is used up: skip, not a failure."""


class RateLimited(FetchError):
    """HTTP 429: retry no earlier than ``retry_after`` seconds from now."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class Quota:
    """A source's budgets plus usage so far (loaded from ``source_usage``); adapters call
    ``before_request`` / ``record_reads`` and the Ingestor persists the ``new_*`` deltas."""

    daily_request_budget: int | None = None
    monthly_budget_usd: float | None = None
    cost_per_read_usd: float = 0.005
    requests_today: int = 0
    spend_month_usd: float = 0.0
    new_requests: int = 0
    new_reads: int = 0
    new_spend_usd: float = 0.0

    @property
    def requests_used(self) -> int:
        return self.requests_today + self.new_requests

    @property
    def spent_usd(self) -> float:
        return round(self.spend_month_usd + self.new_spend_usd, 6)

    def before_request(self) -> None:
        budget = self.daily_request_budget
        if budget is not None and self.requests_used >= budget:
            raise QuotaExhausted(
                f"daily request budget used up ({self.requests_used}/{budget}); "
                "resumes tomorrow (UTC)"
            )
        self.new_requests += 1

    def reads_allowed(self) -> int:
        """Paid reads left this month (unlimited when no spend cap is set)."""
        if self.monthly_budget_usd is None:
            return 10**9
        remaining = self.monthly_budget_usd - self.spent_usd
        return max(0, int(remaining / self.cost_per_read_usd + 1e-9))

    def record_reads(self, count: int) -> None:
        self.new_reads += count
        self.new_spend_usd = round(self.new_spend_usd + count * self.cost_per_read_usd, 6)


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
