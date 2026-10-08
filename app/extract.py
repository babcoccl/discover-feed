"""Fetch article pages and extract their main text with trafilatura.

The text is for internal processing only (clustering now, summaries later): it is never
rendered in the UI or returned from public endpoints.

Politeness: one request at a time per domain, at least ``domain_delay`` seconds between
requests to the same domain (robots.txt included), robots.txt is respected, and at most
``max_articles`` pending articles (newest first) are processed per run.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from app.ingest import USER_AGENT
from app.models import Article, TextStatus, utcnow

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 3


class ArticleExtractResult(BaseModel):
    article_id: int
    url: str
    status: Literal["ok", "failed", "skipped"]
    word_count: int = 0
    reason: str | None = None


class ExtractRunResult(BaseModel):
    status: Literal["ok", "busy"] = "ok"
    processed: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    results: list[ArticleExtractResult] = []


class _Rejected(Exception):
    """A page we refuse to process (too large, not HTML, HTTP error)."""


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def extract_text(html: bytes, url: str) -> str:
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    return (text or "").strip()


class Extractor:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        user_agent: str = USER_AGENT,
        timeout: float = 10.0,
        max_bytes: int = 2 * 1024 * 1024,
        min_words: int = 80,
        domain_delay: float = 2.0,
        max_articles: int = 50,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.min_words = min_words
        self.domain_delay = domain_delay
        self.max_articles = max_articles
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self.now = now
        self._last_request: dict[str, float] = {}
        self._run_lock = threading.Lock()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            transport=self.transport,
        )

    def pending(self, limit: int) -> list[tuple[int, str]]:
        stmt = (
            select(Article.id, Article.url)
            .where(Article.text_status == TextStatus.PENDING)
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(limit)
        )
        with self.session_factory() as session:
            return [(row.id, row.url) for row in session.execute(stmt)]

    async def run(self, limit: int | None = None) -> ExtractRunResult:
        """Extract up to ``limit`` (default ``max_articles``) pending articles. Never raises."""
        if not self._run_lock.acquire(blocking=False):
            return ExtractRunResult(status="busy")
        try:
            items = self.pending(limit or self.max_articles)
            by_domain: dict[str, list[tuple[int, str]]] = {}
            for article_id, url in items:
                by_domain.setdefault(domain_of(url), []).append((article_id, url))
            async with self._client() as client:
                groups = await asyncio.gather(
                    *(self._run_domain(client, d, batch) for d, batch in by_domain.items())
                )
            results = [r for group in groups for r in group]
            return ExtractRunResult(
                processed=len(results),
                ok=sum(r.status == "ok" for r in results),
                failed=sum(r.status == "failed" for r in results),
                skipped=sum(r.status == "skipped" for r in results),
                results=results,
            )
        except Exception:
            logger.exception("extraction run failed")
            return ExtractRunResult()
        finally:
            self._run_lock.release()

    def run_blocking(self, limit: int | None = None) -> ExtractRunResult:
        return asyncio.run(self.run(limit))

    async def _run_domain(
        self, client: httpx.AsyncClient, domain: str, batch: list[tuple[int, str]]
    ) -> list[ArticleExtractResult]:
        """Sequential per domain: this is what limits concurrency to one request per domain."""
        robots: dict[str, RobotFileParser | None] = {}
        results = []
        for article_id, url in batch:
            text = None
            try:
                result, text = await self._extract_one(client, domain, article_id, url, robots)
            except Exception as exc:  # never let one page break the run
                logger.warning("extraction of %s failed: %s", url, exc)
                result = ArticleExtractResult(
                    article_id=article_id, url=url, status="failed", reason=_describe(exc)
                )
            self._save(result, text)
            results.append(result)
        return results

    async def _extract_one(
        self,
        client: httpx.AsyncClient,
        domain: str,
        article_id: int,
        url: str,
        robots: dict[str, RobotFileParser | None],
    ) -> tuple[ArticleExtractResult, str | None]:
        def result(status: str, reason: str | None = None, words: int = 0):
            item = ArticleExtractResult(
                article_id=article_id, url=url, status=status, word_count=words, reason=reason
            )
            return item, None

        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return result("failed", "not an http(s) URL")
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in robots:
            robots[origin] = await self._robots(client, domain, origin)
        parser = robots[origin]
        if parser is None:
            return result("skipped", "robots.txt unavailable")
        if not parser.can_fetch(self.user_agent, url):
            return result("skipped", "disallowed by robots.txt")

        try:
            html = await self._get(client, domain, url)
        except _Rejected as exc:
            return result("failed", str(exc))
        except httpx.HTTPError as exc:
            return result("failed", _describe(exc))

        text = await asyncio.to_thread(extract_text, html, url)
        words = len(text.split())
        if words < self.min_words:
            return result("failed", f"extracted {words} words (< {self.min_words})", words)
        return result("ok", words=words)[0], text

    async def _wait_turn(self, domain: str) -> None:
        last = self._last_request.get(domain)
        if last is not None:
            wait = last + self.domain_delay - self.clock()
            if wait > 0:
                await self.sleep(wait)
        self._last_request[domain] = self.clock()

    async def _get(
        self, client: httpx.AsyncClient, domain: str, url: str, *, html_only: bool = True
    ) -> bytes:
        await self._wait_turn(domain)
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise _Rejected(f"HTTP {resp.status_code}")
            length = resp.headers.get("content-length")
            if length and length.isdigit() and int(length) > self.max_bytes:
                raise _Rejected(f"larger than {self.max_bytes} bytes")
            ctype = resp.headers.get("content-type", "").lower()
            if html_only and ctype and "html" not in ctype and "xml" not in ctype:
                raise _Rejected(f"not HTML ({ctype.split(';')[0]})")
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > self.max_bytes:
                    raise _Rejected(f"larger than {self.max_bytes} bytes")
            return bytes(body)

    async def _robots(
        self, client: httpx.AsyncClient, domain: str, origin: str
    ) -> RobotFileParser | None:
        """RFC 9309: 4xx means no rules (allow); 5xx or unreachable means disallow all."""
        parser = RobotFileParser()
        try:
            body = await self._get(client, domain, f"{origin}/robots.txt", html_only=False)
        except _Rejected as exc:
            status = str(exc).removeprefix("HTTP ")
            if status.isdigit() and 400 <= int(status) < 500:
                parser.parse([])
                return parser
            return None
        except httpx.HTTPError:
            return None
        parser.parse(body.decode("utf-8", errors="replace").splitlines())
        return parser

    def _save(self, result: ArticleExtractResult, text: str | None) -> None:
        with self.session_factory() as session:
            session.execute(
                update(Article)
                .where(Article.id == result.article_id)
                .values(
                    text=text if result.status == "ok" else None,
                    text_status=result.status,
                    text_fetched_at=self.now(),
                    word_count=result.word_count or None,
                )
            )
            session.commit()


def _describe(exc: BaseException) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
