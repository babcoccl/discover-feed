"""Provider orchestration: per-provider rate limits, Retry-After cooldowns, a result cache,
daily request / monthly spend budgets (``source_usage``) and health stats."""

import asyncio
import hashlib
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import SearchProviderConfig
from app.ingest import USER_AGENT, usage_periods
from app.models import SearchCacheEntry, SearchProviderState, SourceUsage, utcnow
from app.search.base import (
    NotAuthorized,
    QueryRejected,
    SearchHit,
    SearchProvider,
    TransientError,
    create_provider,
    is_timeout,
)
from app.sources.api import scrub
from app.sources.base import FetchError, Quota, QuotaExhausted, RateLimited

RETRY_INLINE_SECONDS = 10.0
"""A Retry-After this short is waited out once; longer ones put the provider on cooldown."""
DEFAULT_COOLDOWN_SECONDS = 60.0
TRANSIENT_RETRY_SECONDS = 2.0


@dataclass
class SearchOutcome:
    provider: str
    query: str
    hits: list[SearchHit] = field(default_factory=list)
    cached: bool = False
    error: str | None = None
    raw: str = ""
    skipped: str | None = None
    """``cooldown`` | ``budget``: no request was sent."""
    cooldown_seconds: float = 0.0
    """How long the provider is cooling down after this search (0: not)."""
    kind: str | None = None
    """Error class: ``rate_limited`` | ``timeout`` | ``transient`` | ``query_rejected`` |
    ``auth`` | ``budget`` | ``cooldown`` | ``error``."""
    requests: int = 0
    """HTTP requests actually sent for this search (0: cached or skipped)."""

    @property
    def ok(self) -> bool:
        """The provider answered (a cached answer counts)."""
        return self.error is None


@dataclass
class ProviderHealth:
    """Per-process counters (since start) for the Pipeline panel."""

    requests: int = 0
    successes: int = 0
    cached: int = 0
    hits: int = 0
    errors: int = 0
    rate_limited: int = 0
    timeouts: int = 0
    transient_errors: int = 0
    query_rejected: int = 0
    cooldown_skips: int = 0
    budget_skips: int = 0
    last_ok_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None

    def as_dict(self) -> dict:
        return {
            **{k: v for k, v in self.__dict__.items() if not k.endswith("_at")},
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
        }


def usage_id(config: SearchProviderConfig) -> str:
    return config.budget_source or f"search:{config.name}"


def cache_key(name: str, query: str, language: str | None) -> str:
    raw = f"{name}\0{' '.join(query.split()).casefold()}\0{language or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SearchService:
    """Pacing (``min_interval_seconds``) and cooldowns live in ``search_provider_state``, so
    every process using the same database (the app, ``diagnose``, ``evaluate``) shares them."""

    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.session_factory = session_factory
        self.transport = transport
        self.now = now
        self.sleep = sleep
        self.health: dict[str, ProviderHealth] = {}
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}

    def _lock(self, name: str) -> asyncio.Lock:
        key = (name, id(asyncio.get_running_loop()))
        if key not in self._locks:
            self._locks = {k: v for k, v in self._locks.items() if k[1] == key[1]}
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _health(self, name: str) -> ProviderHealth:
        return self.health.setdefault(name, ProviderHealth())

    # --- shared provider state ----------------------------------------------------------

    def _with_state(self, name: str, change: Callable[[SearchProviderState], object]):
        """Run ``change`` on the provider's row inside one write transaction (the no-op
        UPDATE takes SQLite's write lock first, so two processes cannot interleave)."""
        for attempt in range(2):
            try:
                with self.session_factory() as session:
                    session.execute(
                        update(SearchProviderState)
                        .where(SearchProviderState.provider == name)
                        .values(provider=name)
                    )
                    row = session.get(SearchProviderState, name)
                    if row is None:
                        row = SearchProviderState(provider=name, consecutive_errors=0)
                        session.add(row)
                    out = change(row)
                    row.updated_at = self.now()
                    session.commit()
                    return out
            except IntegrityError:  # another process inserted the row first
                if attempt:
                    raise
        return None  # pragma: no cover

    def state(self, name: str) -> SearchProviderState | None:
        with self.session_factory() as session:
            return session.get(SearchProviderState, name)

    def cooldown_remaining(self, name: str) -> float:
        row = self.state(name)
        if row is None or row.cooldown_until is None:
            return 0.0
        return max(0.0, (row.cooldown_until - self.now()).total_seconds())

    def _reserve_slot(self, name: str, interval: float) -> float:
        """Claim the next request slot; returns how long to wait for it."""
        now = self.now()

        def claim(row: SearchProviderState) -> float:
            slot = now
            if row.last_request_at is not None and interval > 0:
                slot = max(now, row.last_request_at + timedelta(seconds=interval))
            row.last_request_at = slot
            return (slot - now).total_seconds()

        return float(self._with_state(name, claim) or 0.0)

    def _cool_down(self, name: str, seconds: float) -> None:
        until = self.now() + timedelta(seconds=seconds)

        def set_until(row: SearchProviderState) -> None:
            if row.cooldown_until is None or row.cooldown_until < until:
                row.cooldown_until = until

        self._with_state(name, set_until)

    def _record(self, name: str, error: str | None) -> None:
        def change(row: SearchProviderState) -> None:
            if error is None:
                row.consecutive_errors = 0
            else:
                row.consecutive_errors = (row.consecutive_errors or 0) + 1
                row.last_error = error[:500]

        self._with_state(name, change)

    # --- searching ----------------------------------------------------------------------

    async def search(
        self,
        config: SearchProviderConfig,
        query: str,
        *,
        since: datetime,
        language: str | None,
        limit: int | None = None,
        cache_hours: float = 24,
        empty_cache_minutes: float = 10,
        store: bool = True,
    ) -> SearchOutcome:
        """One query. ``store=False`` (dry runs) reads the cache but never writes it."""
        name = config.name
        limit = limit or config.max_results
        outcome = SearchOutcome(provider=name, query=query)
        health = self._health(name)
        key = cache_key(name, query, language)
        if cache_hours > 0:
            hits = self._cached(key, cache_hours, empty_cache_minutes)
            if hits is not None:
                health.cached += 1
                outcome.hits, outcome.cached = hits[:limit], True
                return outcome
        cooling = self.cooldown_remaining(name)
        if cooling > 0:  # wait for the provider: not an error, no request
            health.cooldown_skips += 1
            outcome.skipped, outcome.cooldown_seconds = "cooldown", cooling
            outcome.kind = "cooldown"
            outcome.error = f"{name}: rate limited, cooling down for {math.ceil(cooling)} s"
            return outcome
        async with (
            self._lock(name),
            httpx.AsyncClient(
                timeout=config.timeout_seconds,
                headers={"User-Agent": USER_AGENT},
                transport=self.transport,
            ) as client,
        ):
            provider = create_provider(config, client)
            sent_before = health.requests
            try:
                outcome.hits = await self._call(provider, query, since, language, limit)
            except FetchError as exc:
                outcome.requests = health.requests - sent_before
                message = scrub(str(exc), *provider.secrets())
                outcome.error = message
                if isinstance(exc, QuotaExhausted):
                    health.budget_skips += 1
                    outcome.skipped = outcome.kind = "budget"
                else:
                    if isinstance(exc, RateLimited):
                        outcome.kind = "rate_limited"
                        outcome.cooldown_seconds = self.cooldown_remaining(name)
                    elif is_timeout(exc):
                        outcome.kind = "timeout"
                        health.timeouts += 1
                    elif isinstance(exc, NotAuthorized):
                        outcome.kind = "auth"
                        health.errors += 1
                        self._cool_down(name, exc.retry_after)
                        outcome.cooldown_seconds = self.cooldown_remaining(name)
                    elif isinstance(exc, QueryRejected):
                        outcome.kind = "query_rejected"
                        health.query_rejected += 1
                    elif isinstance(exc, TransientError):
                        outcome.kind = "transient"
                        health.transient_errors += 1
                    else:
                        outcome.kind = "error"
                        health.errors += 1
                    health.last_error, health.last_error_at = message[:500], self.now()
                    self._record(name, message)
                outcome.raw = scrub(provider.last_raw, *provider.secrets())
                return outcome
            outcome.raw = scrub(provider.last_raw, *provider.secrets())
            outcome.requests = health.requests - sent_before
        health.successes += 1
        health.hits += len(outcome.hits)
        health.last_ok_at = self.now()
        self._record(name, None)
        if store and cache_hours > 0 and (outcome.hits or empty_cache_minutes > 0):
            self._store(key, name, query, outcome.hits)
        return outcome

    async def _call(
        self,
        provider: SearchProvider,
        query: str,
        since: datetime,
        language: str | None,
        limit: int,
    ) -> list[SearchHit]:
        config = provider.config
        name = config.name
        health = self._health(name)
        for attempt in range(2):
            quota = self._quota(config)
            if config.cost_per_request_usd and quota.reads_allowed() < 1:
                raise QuotaExhausted(f"{name}: monthly spend cap reached")
            if quota.daily_request_budget is not None and (
                quota.requests_today >= quota.daily_request_budget
            ):
                raise QuotaExhausted(f"{name}: daily request budget reached")
            wait = self._reserve_slot(name, config.min_interval_seconds or 0.0)
            if wait > 0:
                await self.sleep(wait)
            try:
                return await provider.search(query, since, language, limit, quota)
            except RateLimited as exc:
                health.rate_limited += 1
                retry = exc.retry_after
                if attempt == 0 and retry is not None and retry <= RETRY_INLINE_SECONDS:
                    await self.sleep(retry)
                    continue
                self._cool_down(name, retry if retry is not None else DEFAULT_COOLDOWN_SECONDS)
                raise
            except TransientError:
                if attempt == 0:  # empty body / malformed JSON: once more, never a cooldown
                    await self.sleep(TRANSIENT_RETRY_SECONDS)
                    continue
                raise
            finally:
                health.requests += quota.new_requests
                if config.cost_per_request_usd and quota.new_requests:
                    quota.record_reads(quota.new_requests)
                self._save_usage(usage_id(config), quota)
        raise FetchError(f"{name}: rate limited")  # pragma: no cover

    def _quota(self, config: SearchProviderConfig) -> Quota:
        day, month = usage_periods(self.now())
        with self.session_factory() as session:
            today = session.get(SourceUsage, (usage_id(config), day))
            this_month = session.get(SourceUsage, (usage_id(config), month))
            return Quota(
                daily_request_budget=config.daily_request_budget,
                monthly_budget_usd=config.monthly_budget_usd,
                cost_per_read_usd=config.cost_per_request_usd or 0.005,
                requests_today=today.requests if today else 0,
                spend_month_usd=this_month.spend_usd if this_month else 0.0,
            )

    def _save_usage(self, source_id: str, quota: Quota) -> None:
        if not (quota.new_requests or quota.new_reads):
            return
        with self.session_factory() as session:
            for period in usage_periods(self.now()):
                row = session.get(SourceUsage, (source_id, period)) or SourceUsage(
                    source_id=source_id, period=period, requests=0, reads=0, spend_usd=0.0
                )
                row.requests = (row.requests or 0) + quota.new_requests
                row.reads = (row.reads or 0) + quota.new_reads
                row.spend_usd = round((row.spend_usd or 0.0) + quota.new_spend_usd, 6)
                session.add(row)
            session.commit()
        quota.requests_today += quota.new_requests
        quota.spend_month_usd = quota.spent_usd
        quota.new_requests = quota.new_reads = 0
        quota.new_spend_usd = 0.0

    def _cached(
        self, key: str, cache_hours: float, empty_cache_minutes: float = 10
    ) -> list[SearchHit] | None:
        with self.session_factory() as session:
            row = session.get(SearchCacheEntry, key)
            if row is None:
                return None
            max_age = timedelta(hours=cache_hours)
            if not row.hits:  # an empty answer is only trusted briefly
                max_age = min(max_age, timedelta(minutes=empty_cache_minutes))
            if max_age <= timedelta(0) or row.created_at < self.now() - max_age:
                return None
            return [SearchHit.from_dict(h) for h in row.hits or []]

    def _store(self, key: str, name: str, query: str, hits: list[SearchHit]) -> None:
        with self.session_factory() as session:
            row = session.get(SearchCacheEntry, key) or SearchCacheEntry(key=key, provider=name)
            row.query, row.hits, row.created_at = query, [h.to_dict() for h in hits], self.now()
            session.add(row)
            session.commit()

    def prune_cache(self, max_age_hours: float = 48) -> int:
        with self.session_factory() as session:
            cutoff = self.now() - timedelta(hours=max_age_hours)
            result = session.execute(
                delete(SearchCacheEntry).where(SearchCacheEntry.created_at < cutoff)
            )
            session.commit()
            return result.rowcount or 0


def budget_use(session, configs: list[SearchProviderConfig], now: datetime) -> list[dict]:
    """Today's requests / this month's spend per provider (Pipeline panel)."""
    day, month = usage_periods(now)
    rows = []
    for config in configs:
        today = session.get(SourceUsage, (usage_id(config), day))
        this_month = session.get(SourceUsage, (usage_id(config), month))
        rows.append(
            {
                "provider": config.name,
                "requests_today": today.requests if today else 0,
                "daily_budget": config.daily_request_budget,
                "spend_month_usd": round(this_month.spend_usd, 4) if this_month else 0.0,
                "monthly_budget_usd": config.monthly_budget_usd,
            }
        )
    return rows
