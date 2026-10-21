"""Image checks for new articles (each pipeline run) and a resumable backfill.

An article is done once ``image_checked_at`` is set; everything it references (feed image,
page image, publisher icon) is validated once per URL (``ImageAsset``) and, with
``images.cache``, stored as WebP. Stories whose members changed are re-published so their
view picks up the new image.
"""

import asyncio
import logging
import threading
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import ImagesConfig
from app.images.cache import ImageCache, icon_tints, image_key, render_webp
from app.images.fetch import USER_AGENT, Rejected, fetch_image
from app.images.meta import domain_icon_urls
from app.images.safety import Guard, Resolver, guarded_transport, system_resolver
from app.images.select import generic_urls
from app.models import Article, ImageAsset, ImageSource, Story, utcnow

logger = logging.getLogger(__name__)

RETRY_FAILED_AFTER = timedelta(hours=24)


class ImageRunResult(BaseModel):
    status: str = "ok"
    checked: int = 0
    """Articles marked checked."""
    fetched: int = 0
    """Image URLs fetched (each URL at most once; already-validated URLs do not count)."""
    ok: int = 0
    rejected: int = 0
    stories: int = 0
    """Stories re-published."""
    remaining: int = 0
    """Backfill: articles still to do."""
    reason: str | None = None
    """Why the sweep fetched nothing (``nothing_to_check`` | ``all_urls_seen``)."""
    reasons: dict[str, int] = Field(default_factory=dict)
    """Per-URL rejection/failure reason counts (``http_404``, ``timeout``, SSRF
    guards like ``private_ip``/``dns_error``, ``too_large``, ``svg``, ``undecodable``...)."""
    error: str | None = None
    """The exception that ended the sweep early — set only for real failures."""


class ImageWorker:
    def __init__(
        self,
        session_factory: sessionmaker,
        config: ImagesConfig,
        images_dir: Path,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = system_resolver,
        allow_hosts: Iterable[str] = (),
        pages: Callable[[str], Awaitable[object]] | None = None,
        on_changed: Callable[[Session, list[int]], None] | None = None,
        now: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backfill_delay: float = 1.0,
    ) -> None:
        self.session_factory = session_factory
        self.config = config
        self.cache = ImageCache(images_dir, int(config.max_mb * 1024 * 1024))
        self.guard = Guard(resolver, allow_hosts)
        self.transport = transport
        self.pages = pages
        """``Extractor.page_images``: backfill for pages extracted before image support."""
        self.on_changed = on_changed
        self.now = now
        self.sleep = sleep
        self.backfill_delay = backfill_delay
        self._lock = threading.Lock()
        self.last_backfill: ImageRunResult | None = None
        self.last_run: ImageRunResult | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            headers={"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/*;q=0.8"},
            transport=self.transport or guarded_transport(self.guard),
            follow_redirects=False,
            trust_env=False,
        )

    # --- one URL --------------------------------------------------------------------------

    async def asset(
        self, client: httpx.AsyncClient, session: Session, url: str, kind: str, result
    ) -> ImageAsset:
        row = session.scalar(select(ImageAsset).where(ImageAsset.url == url))
        now = self.now()
        if row is not None:
            stale = row.status == "failed" and now - row.checked_at >= RETRY_FAILED_AFTER
            uncached = row.status == "ok" and self.config.cache and not row.cached_bytes
            if not stale and not uncached:
                return row
        else:
            row = ImageAsset(url=url, key=image_key(url), kind=kind, status="failed")
            session.add(row)
        icon = kind == "icon"
        result.fetched += 1
        try:
            got = await fetch_image(
                client,
                self.guard,
                url,
                max_bytes=self.config.max_bytes,
                max_pixels=self.config.max_pixels,
                min_width=16 if icon else self.config.min_width,
                min_height=16 if icon else self.config.min_height,
                icon=icon,
            )
            width = 96 if icon else self.config.cache_width
            webp = await asyncio.to_thread(
                render_webp, got.data, width, max_pixels=self.config.max_pixels, square=icon
            )
            if icon:
                row.tint, row.tint_dark = icon_tints(got.data, self.config.max_pixels)
        except Rejected as exc:
            row.status = "failed" if exc.failed else "rejected"
            row.reason = exc.reason
            row.checked_at = now
            result.rejected += 1
            result.reasons[exc.reason] = result.reasons.get(exc.reason, 0) + 1
            return row
        except Exception as exc:  # undecodable in a way Pillow did not classify
            logger.info("image %s unusable: %s", url, exc)
            row.status, row.reason, row.checked_at = "rejected", "undecodable", now
            result.rejected += 1
            result.reasons["undecodable"] = result.reasons.get("undecodable", 0) + 1
            return row
        row.status, row.reason = "ok", None
        row.content_type, row.width, row.height = got.content_type, got.width, got.height
        row.bytes, row.checked_at = len(got.data), now
        if self.config.cache:
            row.cached_bytes = self.cache.put(row.key, webp)
            row.last_access_at = now
        result.ok += 1
        return row

    # --- one article ----------------------------------------------------------------------

    async def check_article(
        self, client: httpx.AsyncClient, session: Session, article: Article, result
    ) -> None:
        feed = page = None
        if article.image_url:
            feed = await self.asset(client, session, article.image_url, "photo", result)
        if article.page_image_url and article.page_image_url != article.image_url:
            page = await self.asset(client, session, article.page_image_url, "photo", result)
        feed_ok = feed is not None and feed.status == "ok"
        page_ok = page is not None and page.status == "ok"
        tiny = feed_ok and (feed.width or 0) < self.config.tiny_feed_width
        if page_ok and (not feed_ok or (tiny and (page.width or 0) > (feed.width or 0))):
            article.image_source = ImageSource.PAGE.value
        elif feed_ok:
            article.image_source = ImageSource.FEED.value
        else:
            article.image_source = ImageSource.NONE.value
        await self.check_icon(client, session, article, result)
        article.image_checked_at = self.now()

    async def check_icon(self, client, session: Session, article: Article, result) -> None:
        """First usable icon among the page's declared one and the domain defaults."""
        for url in ([article.icon_url] if article.icon_url else []) + domain_icon_urls(article.url):
            if (await self.asset(client, session, url, "icon", result)).status == "ok":
                return

    def _mark_generic(self, session: Session, urls: set[str]) -> None:
        generic = generic_urls(session, urls, self.config.generic_threshold)
        if generic:
            for row in session.scalars(select(ImageAsset).where(ImageAsset.url.in_(generic))):
                row.generic = True

    # --- runs -----------------------------------------------------------------------------

    def _pending(self, session: Session, limit: int) -> list[Article]:
        """New articles whose page was read (or whose extraction is final)."""
        stmt = (
            select(Article)
            .where(
                Article.image_checked_at.is_(None),
                or_(Article.page_scanned_at.is_not(None), Article.text_status != "pending"),
            )
            .order_by(Article.id.desc())
            .limit(limit)
        )
        return list(session.scalars(stmt))

    def _backfill_query(self, since: datetime):
        return (
            select(Article)
            .join(Story, Story.id == Article.story_id)
            .where(
                Article.image_checked_at.is_(None),
                Story.published_version.is_not(None),
                Story.published_at >= since,
            )
        )

    async def _process(self, articles_of, *, backfill: bool) -> ImageRunResult:
        result = ImageRunResult()
        if not self._lock.acquire(blocking=False):
            return ImageRunResult(status="busy")
        try:
            with self.session_factory() as session:
                articles = articles_of(session)
                urls: set[str] = set()
                story_ids: set[int] = set()
                async with self._client() as client:
                    for i, article in enumerate(articles):
                        if backfill and i:
                            await self.sleep(self.backfill_delay)
                        if backfill and article.page_scanned_at is None and self.pages is not None:
                            found = await self.pages(article.url)
                            if found is not None:
                                article.page_image_url = found.image
                                article.icon_url = found.icon
                            article.page_scanned_at = self.now()
                        await self.check_article(client, session, article, result)
                        session.commit()  # resumable: each finished article is kept
                        result.checked += 1
                        urls.update(u for u in (article.image_url, article.page_image_url) if u)
                        if article.story_id:
                            story_ids.add(article.story_id)
                self._mark_generic(session, urls)
                self.evict(session)
                if story_ids and self.on_changed is not None:
                    self.on_changed(session, sorted(story_ids))
                    result.stories = len(story_ids)
                session.commit()
                if backfill:
                    since = self.now() - timedelta(days=self.config.backfill_days)
                    result.remaining = session.scalar(
                        select(func.count()).select_from(self._backfill_query(since).subquery())
                    )
            if result.fetched == 0:
                # zero-fetch sweep: either the queue was empty or every candidate URL
                # already had an ImageAsset verdict (ok/rejected/cooling-down) — never a failure
                result.reason = "nothing_to_check" if result.checked == 0 else "all_urls_seen"
            return result
        except Exception as exc:
            logger.exception("image run failed")
            # keep the counts the sweep actually produced; error carries the cause
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"[:300]
            return result
        finally:
            self._lock.release()

    async def run(self, limit: int | None = None) -> ImageRunResult:
        self.last_run = await self._process(
            lambda s: self._pending(s, limit or self.config.per_run), backfill=False
        )
        return self.last_run

    async def backfill(self, days: float | None = None, limit: int | None = None) -> ImageRunResult:
        """Members of stories published in the last ``days``: rate limited (one article per
        ``backfill_delay``), resumable (``image_checked_at`` marks finished articles)."""
        since = self.now() - timedelta(days=self.config.backfill_days if days is None else days)
        limit = limit or self.config.backfill_per_run

        def articles(session: Session) -> list[Article]:
            stmt = self._backfill_query(since).order_by(Article.id.desc()).limit(limit)
            return list(session.scalars(stmt))

        self.last_backfill = await self._process(articles, backfill=True)
        return self.last_backfill

    def backfill_blocking(self) -> None:
        result = asyncio.run(self.backfill())
        logger.info("image backfill %s", result.model_dump())

    # --- cache ----------------------------------------------------------------------------

    def evict(self, session: Session) -> int:
        """Drop least-recently-used cached files until the cache fits ``max_mb``."""
        total = session.scalar(select(func.coalesce(func.sum(ImageAsset.cached_bytes), 0)))
        evicted = 0
        if total <= self.cache.max_bytes:
            return 0
        rows = session.scalars(
            select(ImageAsset)
            .where(ImageAsset.cached_bytes.is_not(None))
            .order_by(ImageAsset.last_access_at.asc().nulls_first(), ImageAsset.id.asc())
        )
        for row in rows:
            if total <= self.cache.max_bytes:
                break
            self.cache.delete(row.key)
            total -= row.cached_bytes or 0
            row.cached_bytes = None
            evicted += 1
        return evicted

    def touch(self, key: str) -> ImageAsset | None:
        """Record a cache hit (LRU order); the row, or None for an unknown key."""
        with self.session_factory() as session:
            row = session.scalar(select(ImageAsset).where(ImageAsset.key == key))
            if row is not None:
                row.last_access_at = self.now()
                session.commit()
                session.refresh(row)
                session.expunge(row)
            return row


def image_stats(session: Session, cache: ImageCache | None) -> dict:
    """Pipeline panel numbers."""
    by_story = dict(
        session.execute(
            select(Story.image_source, func.count())
            .where(Story.published_version.is_not(None))
            .group_by(Story.image_source)
        ).all()
    )
    by_article = dict(
        session.execute(
            select(Article.image_source, func.count())
            .where(Article.image_checked_at.is_not(None))
            .group_by(Article.image_source)
        ).all()
    )
    unchecked = session.scalar(
        select(func.count()).select_from(Article).where(Article.image_checked_at.is_(None))
    )
    failures = Counter(
        dict(
            session.execute(
                select(ImageAsset.reason, func.count())
                .where(ImageAsset.status != "ok")
                .group_by(ImageAsset.reason)
            ).all()
        )
    )
    cached = session.execute(
        select(
            func.count(ImageAsset.cached_bytes),
            func.coalesce(func.sum(ImageAsset.cached_bytes), 0),
        )
    ).one()
    generic = session.scalar(
        select(func.count()).select_from(ImageAsset).where(ImageAsset.generic.is_(True))
    )
    stories = {k or "unknown": v for k, v in by_story.items()}
    return {
        "stories": stories,
        "articles": {k or "unknown": v for k, v in by_article.items()},
        "missing": sum(v for k, v in stories.items() if k in ("logo", "none")),
        "unchecked": unchecked,
        "failures": dict(failures.most_common()),
        "cached_files": cached[0],
        "cache_bytes": cached[1],
        "max_bytes": cache.max_bytes if cache else 0,
        "generic": generic,
    }


__all__ = ["ImageRunResult", "ImageWorker", "image_stats"]
