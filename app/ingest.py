"""Fetch sources, normalize their items and store new articles with deduplication."""

import asyncio
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

import httpx
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from app.config import Source
from app.lifecycle import ensure_stories
from app.models import Article, SourceRecord, SourceStatus, SourceUsage, TextStatus, utcnow
from app.normalize import NormalizedArticle, normalize, normalize_title
from app.runs import record_run
from app.sources import FetchState, Quota, QuotaExhausted, RateLimited, RawArticle, create_adapter

logger = logging.getLogger(__name__)

try:
    _VERSION = version("discover-feed")
except PackageNotFoundError:  # pragma: no cover
    _VERSION = "0.0.0"

USER_AGENT = f"discover-feed/{_VERSION} (+https://github.com/babcoccl/discover-feed)"
# Hosts whose fair-access policy requires a contact in the User-Agent.
CONTACT_REQUIRED_HOSTS = ("sec.gov",)


def build_user_agent(contact_email: str | None = None) -> str:
    contact = (contact_email or "").strip()
    return f"{USER_AGENT[:-1]}; {contact})" if contact else USER_AGENT


def sources_requiring_contact(sources: Sequence[Source]) -> list[Source]:
    def needs(source: Source) -> bool:
        host = (source.url.host or "").lower()
        return any(host == h or host.endswith("." + h) for h in CONTACT_REQUIRED_HOSTS)

    return [s for s in sources if needs(s)]


MAX_BACKOFF = timedelta(hours=24)


class SourceRunResult(BaseModel):
    source_id: str
    status: Literal["ok", "not_modified", "error", "skipped", "disabled"]
    fetched: int = 0
    new_articles: int = 0
    error: str | None = None


def backoff_delay(consecutive_failures: int, refresh_minutes: int) -> timedelta:
    """refresh, 2x, 4x, ... the refresh interval, capped at MAX_BACKOFF."""
    exponent = min(max(consecutive_failures - 1, 0), 20)
    return min(timedelta(minutes=refresh_minutes) * 2**exponent, MAX_BACKOFF)


def is_due(status: SourceStatus, now: datetime, refresh_minutes: int) -> bool:
    # Half an interval of grace so scheduler jitter doesn't skip an extra run.
    if status.next_attempt_at is None:
        return True
    return now >= status.next_attempt_at - timedelta(minutes=refresh_minutes) / 2


class Ingestor:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        timeout: float = 10.0,
        user_agent: str = USER_AGENT,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utcnow,
        auto_disable_failures: int = 10,
    ) -> None:
        self.session_factory = session_factory
        self.auto_disable_failures = auto_disable_failures
        """Disable a source after this many consecutive failures (0 = never)."""
        self.timeout = timeout
        self.user_agent = user_agent
        self.transport = transport
        self.clock = clock
        self.on_new_articles: Callable[[list[int]], object] | None = None
        """Called with the ids of newly stored articles right after they commit (alerts)."""
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
            transport=self.transport,
        )

    def http_client(self) -> httpx.AsyncClient:
        """A client with the ingestion User-Agent, timeout and transport (test fetches)."""
        return self._client()

    def _lock(self, source_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(source_id, threading.Lock())

    async def run(self, sources: Sequence[Source], *, force: bool = False) -> list[SourceRunResult]:
        """Ingest all ``sources`` concurrently; a failing source never affects the others.

        ``force`` ignores the failure backoff (used for manual refreshes).
        """
        from app import telemetry

        async with self._client() as client:
            results = list(
                await asyncio.gather(*(self._run_one(client, s, force) for s in sources))
            )
        telemetry.emit_stage(
            "ingest",
            detail={
                "sources": len(results),
                "ok": sum(r.status == "ok" for r in results),
                "errors": sum(r.status == "error" for r in results),
                "new_articles": sum(r.new_articles or 0 for r in results),
            },
            status="ok" if all(r.status != "error" for r in results) else "failed",
        )
        try:
            record_run(
                self.session_factory,
                "ingest",
                {
                    "sources": [r.source_id for r in results],
                    "ok": sum(r.status == "ok" for r in results),
                    "errors": sum(r.status == "error" for r in results),
                    "new_articles": sum(r.new_articles or 0 for r in results),
                },
            )
        except Exception:
            logger.exception("could not record ingestion run")
        return results

    def run_blocking(self, sources: Sequence[Source], *, force: bool = False) -> None:
        """Entry point for scheduler threads."""
        for result in asyncio.run(self.run(sources, force=force)):
            logger.info("ingest %s", result.model_dump(exclude_none=True))

    async def _run_one(
        self, client: httpx.AsyncClient, source: Source, force: bool
    ) -> SourceRunResult:
        lock = self._lock(source.id)
        if not lock.acquire(blocking=False):
            return SourceRunResult(source_id=source.id, status="skipped", error="already running")
        try:
            return await self._ingest(client, source, force)
        except Exception as exc:
            logger.exception("ingest of %s failed", source.id)
            self._record_failure(source, exc)
            return SourceRunResult(source_id=source.id, status="error", error=_describe(exc))
        finally:
            lock.release()

    async def _ingest(
        self, client: httpx.AsyncClient, source: Source, force: bool
    ) -> SourceRunResult:
        with self.session_factory() as session:
            record = session.get(SourceRecord, source.id)
            if record is not None and (record.removed_at is not None or not record.enabled):
                return SourceRunResult(source_id=source.id, status="skipped", error="disabled")
            status = session.get(SourceStatus, source.id) or SourceStatus(source_id=source.id)
            quota = load_quota(session, source, self.clock()) if source.is_api else None
        if not force and not is_due(status, self.clock(), source.refresh_minutes):
            return SourceRunResult(
                source_id=source.id,
                status="skipped",
                error=f"backing off until {status.next_attempt_at.isoformat()}",
            )

        state = FetchState(etag=status.etag, last_modified=status.last_modified, quota=quota)
        try:
            raws = await create_adapter(source.type, client).fetch(source, state)
        except QuotaExhausted as exc:
            self._save_usage(source, quota)
            self._record_skip(source, exc)
            return SourceRunResult(source_id=source.id, status="skipped", error=str(exc))
        except Exception as exc:
            self._save_usage(source, quota)
            logger.warning("fetch of %s failed: %s", source.id, _describe(exc))
            disabled = self._record_failure(source, exc)
            return SourceRunResult(
                source_id=source.id,
                status="disabled" if disabled else "error",
                error=_describe(exc),
            )

        self._save_usage(source, quota)
        fetched_at = self.clock()
        new = 0 if state.not_modified else self._store(source, raws, fetched_at)
        self._record_success(source, state, fetched_at, len(raws))
        return SourceRunResult(
            source_id=source.id,
            status="not_modified" if state.not_modified else "ok",
            fetched=len(raws),
            new_articles=new,
        )

    def _save_usage(self, source: Source, quota: Quota | None) -> None:
        if quota is None or not (quota.new_requests or quota.new_reads):
            return
        now = self.clock()
        with self.session_factory() as session:
            for period in usage_periods(now):
                row = session.get(SourceUsage, (source.id, period)) or SourceUsage(
                    source_id=source.id, period=period, requests=0, reads=0, spend_usd=0.0
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

    def _record_skip(self, source: Source, exc: BaseException) -> None:
        with self.session_factory() as session:
            status = session.get(SourceStatus, source.id) or SourceStatus(source_id=source.id)
            status.last_attempt_at = self.clock()
            status.last_error = str(exc)[:2000]
            session.add(status)
            session.commit()

    def _store(self, source: Source, raws: list[RawArticle], fetched_at: datetime) -> int:
        base_url = str(source.url)
        items: list[NormalizedArticle] = []
        extra: dict[str, dict] = {}
        for raw in raws:
            item = normalize(raw, source_id=source.id, fetched_at=fetched_at, base_url=base_url)
            if item is not None:
                items.append(item)
                extra[item.canonical_url] = _text_columns(raw, source, fetched_at)
        if not items:
            return 0

        with self.session_factory() as session:
            seen_urls = set(
                session.scalars(
                    select(Article.canonical_url).where(
                        Article.canonical_url.in_({i.canonical_url for i in items})
                    )
                )
            )
            seen_hashes = set(
                session.scalars(
                    select(Article.content_hash).where(
                        Article.content_hash.in_({i.content_hash for i in items})
                    )
                )
            )
            new = 0
            stored: list[str] = []
            for item in items:
                # Untitled items would all share one hash per domain, so only URL dedup applies.
                has_title = bool(normalize_title(item.title))
                duplicate_story = has_title and item.content_hash in seen_hashes
                if item.canonical_url in seen_urls or duplicate_story:
                    continue
                result = session.execute(
                    sqlite_insert(Article)
                    .values(**asdict(item), **extra[item.canonical_url])
                    .on_conflict_do_nothing(index_elements=["canonical_url"])
                )
                new += result.rowcount
                if result.rowcount:
                    stored.append(item.canonical_url)
                seen_urls.add(item.canonical_url)
                if has_title:
                    seen_hashes.add(item.content_hash)
            ensure_stories(session, utcnow())  # every article is a story from ingest on
            session.commit()
            ids = (
                list(session.scalars(select(Article.id).where(Article.canonical_url.in_(stored))))
                if stored
                else []
            )
        if ids and self.on_new_articles is not None:
            try:
                self.on_new_articles(ids)
            except Exception:
                logger.exception("alert evaluation failed for %d new articles", len(ids))
        return new

    def _record_success(
        self, source: Source, state: FetchState, at: datetime, items: int | None = None
    ) -> None:
        with self.session_factory() as session:
            status = session.get(SourceStatus, source.id) or SourceStatus(source_id=source.id)
            status.last_attempt_at = at
            status.last_success_at = at
            status.last_error = None
            status.consecutive_failures = 0
            status.next_attempt_at = None
            status.etag = state.etag
            status.last_modified = state.last_modified
            if not state.not_modified:
                status.items_last_run = items
            session.add(status)
            session.commit()

    def _record_failure(self, source: Source, exc: BaseException) -> bool:
        """Back off; returns True when this failure auto-disabled the source."""
        now = self.clock()
        disabled = False
        with self.session_factory() as session:
            status = session.get(SourceStatus, source.id) or SourceStatus(source_id=source.id)
            status.consecutive_failures = (status.consecutive_failures or 0) + 1
            status.last_attempt_at = now
            status.last_error = _describe(exc)[:2000]
            status.next_attempt_at = now + backoff_delay(
                status.consecutive_failures, source.refresh_minutes
            )
            retry_after = getattr(exc, "retry_after", None)
            if isinstance(exc, RateLimited) and retry_after is not None:
                status.next_attempt_at = max(
                    status.next_attempt_at, now + timedelta(seconds=retry_after)
                )
            session.add(status)
            limit = self.auto_disable_failures
            record = session.get(SourceRecord, source.id)
            if (
                limit
                and record is not None
                and record.enabled
                and status.consecutive_failures >= limit
            ):
                record.enabled = False
                record.auto_disabled_at = now
                record.disabled_reason = (
                    f"{status.consecutive_failures} consecutive failures; last: {status.last_error}"
                )[:2000]
                disabled = True
                logger.warning(
                    "source %s disabled after %d consecutive failures",
                    source.id,
                    status.consecutive_failures,
                )
            session.commit()
        return disabled


def usage_periods(now: datetime) -> tuple[str, str]:
    return f"day:{now:%Y-%m-%d}", f"month:{now:%Y-%m}"


def load_quota(session, source: Source, now: datetime) -> Quota:
    day, month = usage_periods(now)
    today = session.get(SourceUsage, (source.id, day))
    this_month = session.get(SourceUsage, (source.id, month))
    return Quota(
        daily_request_budget=source.daily_request_budget,
        monthly_budget_usd=source.monthly_budget_usd,
        cost_per_read_usd=source.cost_per_read_usd,
        requests_today=today.requests if today else 0,
        spend_month_usd=this_month.spend_usd if this_month else 0.0,
    )


def _text_columns(raw: RawArticle, source: Source, fetched_at: datetime) -> dict:
    """Body text the API already supplied (no extraction needed) or social posts (none)."""
    if raw.text:
        return {
            "text": raw.text,
            "text_status": TextStatus.OK.value,
            "text_fetched_at": fetched_at,
            "word_count": len(raw.text.split()),
        }
    if raw.social or source.social:
        return {"text_status": TextStatus.SKIPPED.value, "text_fetched_at": fetched_at}
    return {}


def _describe(exc: BaseException) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
