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
from app.models import Article, SourceStatus, utcnow
from app.normalize import NormalizedArticle, normalize, normalize_title
from app.sources import FetchState, RawArticle, create_adapter

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
    status: Literal["ok", "not_modified", "error", "skipped"]
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
    ) -> None:
        self.session_factory = session_factory
        self.timeout = timeout
        self.user_agent = user_agent
        self.transport = transport
        self.clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
            transport=self.transport,
        )

    def _lock(self, source_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(source_id, threading.Lock())

    async def run(self, sources: Sequence[Source], *, force: bool = False) -> list[SourceRunResult]:
        """Ingest all ``sources`` concurrently; a failing source never affects the others.

        ``force`` ignores the failure backoff (used for manual refreshes).
        """
        async with self._client() as client:
            return list(await asyncio.gather(*(self._run_one(client, s, force) for s in sources)))

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
            status = session.get(SourceStatus, source.id) or SourceStatus(source_id=source.id)
        if not force and not is_due(status, self.clock(), source.refresh_minutes):
            return SourceRunResult(
                source_id=source.id,
                status="skipped",
                error=f"backing off until {status.next_attempt_at.isoformat()}",
            )

        state = FetchState(etag=status.etag, last_modified=status.last_modified)
        try:
            raws = await create_adapter(source.type, client).fetch(source, state)
        except Exception as exc:
            logger.warning("fetch of %s failed: %s", source.id, _describe(exc))
            self._record_failure(source, exc)
            return SourceRunResult(source_id=source.id, status="error", error=_describe(exc))

        fetched_at = self.clock()
        new = 0 if state.not_modified else self._store(source, raws, fetched_at)
        self._record_success(source, state, fetched_at)
        return SourceRunResult(
            source_id=source.id,
            status="not_modified" if state.not_modified else "ok",
            fetched=len(raws),
            new_articles=new,
        )

    def _store(self, source: Source, raws: list[RawArticle], fetched_at: datetime) -> int:
        base_url = str(source.url)
        items: list[NormalizedArticle] = []
        for raw in raws:
            item = normalize(raw, source_id=source.id, fetched_at=fetched_at, base_url=base_url)
            if item is not None:
                items.append(item)
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
            for item in items:
                # Untitled items would all share one hash per domain, so only URL dedup applies.
                has_title = bool(normalize_title(item.title))
                duplicate_story = has_title and item.content_hash in seen_hashes
                if item.canonical_url in seen_urls or duplicate_story:
                    continue
                result = session.execute(
                    sqlite_insert(Article)
                    .values(**asdict(item))
                    .on_conflict_do_nothing(index_elements=["canonical_url"])
                )
                new += result.rowcount
                seen_urls.add(item.canonical_url)
                if has_title:
                    seen_hashes.add(item.content_hash)
            session.commit()
        return new

    def _record_success(self, source: Source, state: FetchState, at: datetime) -> None:
        with self.session_factory() as session:
            status = session.get(SourceStatus, source.id) or SourceStatus(source_id=source.id)
            status.last_attempt_at = at
            status.last_success_at = at
            status.last_error = None
            status.consecutive_failures = 0
            status.next_attempt_at = None
            status.etag = state.etag
            status.last_modified = state.last_modified
            session.add(status)
            session.commit()

    def _record_failure(self, source: Source, exc: BaseException) -> None:
        now = self.clock()
        with self.session_factory() as session:
            status = session.get(SourceStatus, source.id) or SourceStatus(source_id=source.id)
            status.consecutive_failures = (status.consecutive_failures or 0) + 1
            status.last_attempt_at = now
            status.last_error = _describe(exc)[:2000]
            status.next_attempt_at = now + backoff_delay(
                status.consecutive_failures, source.refresh_minutes
            )
            session.add(status)
            session.commit()


def _describe(exc: BaseException) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
