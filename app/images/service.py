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
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
    cache_hits: int = 0
    """URLs whose ImageAsset row already existed (validated earlier, or stored by another
    worker between our check and our insert): reused, not refetched — never a failure."""
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

    async def asset(self, client: httpx.AsyncClient, url: str, kind: str, result) -> ImageAsset:
        """Look up or claim the ``ImageAsset`` row in a short transaction, then fetch
        and render with no write transaction open, then record the outcome in a
        second short transaction — a network fetch or a Pillow render can take
        seconds and must never hold SQLite's write lock."""
        # The key is deterministic (sha256 of the URL), so a stored row for this image
        # is found by URL or key before we try to insert.
        key = image_key(url)
        now = self.now()
        with self.session_factory() as session:
            row = session.scalar(
                select(ImageAsset).where(or_(ImageAsset.url == url, ImageAsset.key == key))
            )
            if row is not None:
                stale = row.status == "failed" and now - row.checked_at >= RETRY_FAILED_AFTER
                uncached = row.status == "ok" and self.config.cache and not row.cached_bytes
                if not stale and not uncached:
                    result.cache_hits += 1
                    session.expunge(row)
                    return row
            else:
                # INSERT OR IGNORE, not session.add: autoflush is off, so a pending row
                # is invisible to the SELECTs above — the same URL requested twice in
                # one run would insert twice and die on UNIQUE(images.key). An
                # immediate insert keeps lookups honest, and a row stored by another
                # worker between our SELECT and our INSERT is a cache hit, not a
                # failure.
                stored = session.execute(
                    sqlite_insert(ImageAsset)
                    .values(url=url, key=key, kind=kind, status="failed")
                    .on_conflict_do_nothing()
                )
                row = session.scalar(
                    select(ImageAsset).where(or_(ImageAsset.url == url, ImageAsset.key == key))
                )
                if not stored.rowcount:
                    result.cache_hits += 1
                    logger.debug("image %s already stored (key %s)", url, key)
                    found = row or ImageAsset(url=url, key=key, kind=kind, status="failed")
                    session.expunge_all()
                    return found
            session.commit()
        icon = kind == "icon"
        result.fetched += 1
        tints: tuple[str | None, str | None] = (None, None)
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
                tints = icon_tints(got.data, self.config.max_pixels)
            cached_bytes = self.cache.put(key, webp) if self.config.cache else None
        except Rejected as exc:
            outcome = {
                "status": "failed" if exc.failed else "rejected",
                "reason": exc.reason,
            }
            result.rejected += 1
            result.reasons[exc.reason] = result.reasons.get(exc.reason, 0) + 1
        except Exception as exc:  # undecodable in a way Pillow did not classify
            logger.info("image %s unusable: %s", url, exc)
            outcome = {"status": "rejected", "reason": "undecodable"}
            result.rejected += 1
            result.reasons["undecodable"] = result.reasons.get("undecodable", 0) + 1
        else:
            outcome = {
                "status": "ok",
                "reason": None,
                "content_type": got.content_type,
                "width": got.width,
                "height": got.height,
                "bytes": len(got.data),
                "cached_bytes": cached_bytes,
                "last_access_at": now if cached_bytes else None,
                "tint": tints[0],
                "tint_dark": tints[1],
            }
            result.ok += 1
        with self.session_factory() as session:
            row = session.scalar(
                select(ImageAsset).where(or_(ImageAsset.url == url, ImageAsset.key == key))
            )
            if row is None:  # deleted between our claim and this update: store it again
                row = ImageAsset(url=url, key=key, kind=kind)
                session.add(row)
            row.checked_at = now
            for field, value in outcome.items():
                if value is not None or field in ("status", "reason", "last_access_at"):
                    setattr(row, field, value)
            session.commit()
            session.expunge(row)
            return row

    # --- one article ----------------------------------------------------------------------

    async def check_article(self, client: httpx.AsyncClient, article_id: int, result) -> None:
        """One article's images (resumable via ``image_checked_at``): reads and the
        final ``image_source``/``image_checked_at`` write are each a short
        transaction — no write transaction spans a fetch or a render."""
        with self.session_factory() as session:
            article = session.get(Article, article_id)
            if article is None:
                return
            image_url, page_image_url, icon_url, article_url = (
                article.image_url,
                article.page_image_url,
                article.icon_url,
                article.url,
            )
        feed = page = None
        if image_url:
            feed = await self.asset(client, image_url, "photo", result)
        if page_image_url and page_image_url != image_url:
            page = await self.asset(client, page_image_url, "photo", result)
        feed_ok = feed is not None and feed.status == "ok"
        page_ok = page is not None and page.status == "ok"
        tiny = feed_ok and (feed.width or 0) < self.config.tiny_feed_width
        if page_ok and (not feed_ok or (tiny and (page.width or 0) > (feed.width or 0))):
            source = ImageSource.PAGE.value
        elif feed_ok:
            source = ImageSource.FEED.value
        else:
            source = ImageSource.NONE.value
        await self.check_icon(client, icon_url, article_url, result)
        with self.session_factory() as session:
            article = session.get(Article, article_id)
            if article is not None:
                article.image_source = source
                article.image_checked_at = self.now()
                session.commit()  # resumable: each finished article is kept

    async def check_icon(self, client, icon_url: str | None, article_url: str, result) -> None:
        """First usable icon among the page's declared one and the domain defaults."""
        for url in ([icon_url] if icon_url else []) + domain_icon_urls(article_url):
            if (await self.asset(client, url, "icon", result)).status == "ok":
                return

    def _mark_generic(self, urls: set[str]) -> None:
        with self.session_factory() as session:
            generic = generic_urls(session, urls, self.config.generic_threshold)
            if generic:
                for row in session.scalars(select(ImageAsset).where(ImageAsset.url.in_(generic))):
                    row.generic = True
            session.commit()

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
            # Snapshot the queue in a short read, then check articles one by one; every
            # write is its own short transaction inside check_article / _mark_generic /
            # evict / on_changed, so no write transaction spans a fetch or a render.
            with self.session_factory() as session:
                articles = articles_of(session)
                rows = [
                    (a.id, a.url, a.image_url, a.page_image_url, a.page_scanned_at, a.story_id)
                    for a in articles
                ]
            urls: set[str] = set()
            story_ids: set[int] = set()
            async with self._client() as client:
                for i, row_info in enumerate(rows):
                    article_id, url, image_url, page_image_url, page_scanned_at, story_id = row_info
                    if backfill and i:
                        await self.sleep(self.backfill_delay)
                    if backfill and page_scanned_at is None and self.pages is not None:
                        found = await self.pages(url)
                        with self.session_factory() as session:
                            article = session.get(Article, article_id)
                            if article is not None:
                                if found is not None:
                                    article.page_image_url = found.image
                                    article.icon_url = found.icon
                                    page_image_url = found.image
                                article.page_scanned_at = self.now()
                                session.commit()
                    await self.check_article(client, article_id, result)
                    result.checked += 1
                    urls.update(u for u in (image_url, page_image_url) if u)
                    if story_id:
                        story_ids.add(story_id)
            self._mark_generic(urls)
            self.evict()
            if story_ids and self.on_changed is not None:
                with self.session_factory() as session:
                    self.on_changed(session, sorted(story_ids))
                    session.commit()
                result.stories = len(story_ids)
            if backfill:
                since = self.now() - timedelta(days=self.config.backfill_days)
                with self.session_factory() as session:
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

    def evict(self, session: Session | None = None) -> int:
        """Drop least-recently-used cached files until the cache fits ``max_mb``."""
        if session is not None:
            return self._evict(session)
        with self.session_factory() as session:
            evicted = self._evict(session)
            session.commit()
            return evicted

    def _evict(self, session: Session) -> int:
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
