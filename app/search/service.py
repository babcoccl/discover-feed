"""Provider orchestration: per-provider rate limits, Retry-After cooldowns, a result cache,
daily request / monthly spend budgets (``source_usage``) and health stats."""

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

from app.config import SearchProviderConfig
from app.ingest import USER_AGENT, usage_periods
from app.models import SearchCacheEntry, SourceUsage, utcnow
from app.search.base import SearchHit, SearchProvider, create_provider
from app.sources.api import scrub
from app.sources.base import FetchError, Quota, QuotaExhausted, RateLimited

RETRY_INLINE_SECONDS = 10.0
"""A Retry-After this short is waited out once; longer ones put the provider on cooldown."""
DEFAULT_COOLDOWN_SECONDS = 60.0


@dataclass
class SearchOutcome:
    provider: str
    query: str
    hits: list[SearchHit] = field(default_factory=list)
    cached: bool = False
    error: str | None = None
    raw: str = ""


@dataclass
class ProviderHealth:
    requests: int = 0
    cached: int = 0
    hits: int = 0
    errors: int = 0
    rate_limited: int = 0
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
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.session_factory = session_factory
        self.transport = transport
        self.now = now
        self.monotonic = monotonic
        self.sleep = sleep
        self.health: dict[str, ProviderHealth] = {}
        self._last_request: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}

    def _lock(self, name: str) -> asyncio.Lock:
        key = (name, id(asyncio.get_running_loop()))
        if key not in self._locks:
            self._locks = {k: v for k, v in self._locks.items() if k[1] == key[1]}
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _health(self, name: str) -> ProviderHealth:
        return self.health.setdefault(name, ProviderHealth())

    async def search(
        self,
        config: SearchProviderConfig,
        query: str,
        *,
        since: datetime,
        language: str | None,
        limit: int | None = None,
        cache_hours: float = 24,
    ) -> SearchOutcome:
        name = config.name
        limit = limit or config.max_results
        outcome = SearchOutcome(provider=name, query=query)
        health = self._health(name)
        key = cache_key(name, query, language)
        if cache_hours > 0:
            hits = self._cached(key, cache_hours)
            if hits is not None:
                health.cached += 1
                outcome.hits, outcome.cached = hits[:limit], True
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
            try:
                outcome.hits = await self._call(provider, query, since, language, limit)
            except FetchError as exc:
                message = scrub(str(exc), *provider.secrets())
                outcome.error = message
                if isinstance(exc, QuotaExhausted):
                    health.budget_skips += 1
                else:
                    health.errors += 1
                    health.last_error, health.last_error_at = message[:500], self.now()
                outcome.raw = scrub(provider.last_raw, *provider.secrets())
                return outcome
            outcome.raw = scrub(provider.last_raw, *provider.secrets())
        health.hits += len(outcome.hits)
        health.last_ok_at = self.now()
        if cache_hours > 0:
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
        cooldown = self._cooldown_until.get(name, 0.0) - self.monotonic()
        if cooldown > 0:
            raise FetchError(f"{name}: rate limited, cooling down for {int(cooldown) + 1} s")
        for attempt in range(2):
            quota = self._quota(config)
            if config.cost_per_request_usd and quota.reads_allowed() < 1:
                raise QuotaExhausted(f"{name}: monthly spend cap reached")
            await self._wait_turn(name, config.min_interval_seconds)
            try:
                return await provider.search(query, since, language, limit, quota)
            except RateLimited as exc:
                health.rate_limited += 1
                wait = exc.retry_after
                if attempt == 0 and wait is not None and wait <= RETRY_INLINE_SECONDS:
                    await self.sleep(wait)
                    continue
                self._cooldown_until[name] = self.monotonic() + (
                    wait if wait is not None else DEFAULT_COOLDOWN_SECONDS
                )
                raise
            finally:
                health.requests += quota.new_requests
                if config.cost_per_request_usd and quota.new_requests:
                    quota.record_reads(quota.new_requests)
                self._save_usage(usage_id(config), quota)
        raise FetchError(f"{name}: rate limited")  # pragma: no cover

    async def _wait_turn(self, name: str, interval: float) -> None:
        last = self._last_request.get(name)
        if last is not None and interval > 0:
            wait = last + interval - self.monotonic()
            if wait > 0:
                await self.sleep(wait)
        self._last_request[name] = self.monotonic()

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

    def _cached(self, key: str, cache_hours: float) -> list[SearchHit] | None:
        with self.session_factory() as session:
            row = session.get(SearchCacheEntry, key)
            if row is None or row.created_at < self.now() - timedelta(hours=cache_hours):
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
