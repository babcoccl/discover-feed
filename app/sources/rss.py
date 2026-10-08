import io
import re
import time
from datetime import UTC, datetime
from typing import Any

import feedparser

from app.config import Source, SourceType
from app.sources.base import FetchError, FetchState, RawArticle, SourceAdapter, register

_IMG_SRC = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif|webp|avif)(\?|$)", re.IGNORECASE)


@register(SourceType.RSS, SourceType.ATOM)
class RSSAdapter(SourceAdapter):
    """RSS 0.9x/1.0/2.0 and Atom feeds via feedparser, with conditional GETs."""

    async def fetch(self, source: Source, state: FetchState | None = None) -> list[RawArticle]:
        state = state if state is not None else FetchState()
        url = str(source.url)
        headers = {}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified

        resp = await self.client.get(url, headers=headers)
        if resp.status_code == 304:
            state.not_modified = True
            return []
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} from {url}")

        response_headers = {k.lower(): v for k, v in resp.headers.items()}
        response_headers["content-location"] = str(resp.url)
        parsed = feedparser.parse(io.BytesIO(resp.content), response_headers=response_headers)
        if parsed.bozo and not parsed.entries:
            raise FetchError(f"invalid feed from {url}: {parsed.get('bozo_exception')}")

        state.etag = resp.headers.get("etag") or state.etag
        state.last_modified = resp.headers.get("last-modified") or state.last_modified
        state.not_modified = False
        return [_to_raw(entry) for entry in parsed.entries]


def _to_raw(entry: dict[str, Any]) -> RawArticle:
    entry_id = entry.get("id") or ""
    url = entry.get("link") or (entry_id if entry_id.startswith(("http://", "https://")) else None)
    content = entry.get("content") or []
    summary = entry.get("summary") or (content[0].get("value") if content else None)
    return RawArticle(
        url=url,
        title=entry.get("title"),
        summary=summary,
        author=entry.get("author"),
        published_at=_struct_to_dt(
            _plain(entry, "published_parsed") or _plain(entry, "updated_parsed")
        ),
        published_raw=_plain(entry, "published") or _plain(entry, "updated"),
        image_url=_image_url(entry, summary, content),
        raw=_jsonable(entry),
    )


def _plain(entry: dict[str, Any], key: str) -> Any:
    # Bypasses FeedParserDict's deprecated updated->published key aliasing.
    return dict.get(entry, key)


def _struct_to_dt(value: time.struct_time | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime(*value[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _is_image(media: dict[str, Any]) -> bool:
    if media.get("medium") == "image" or str(media.get("type", "")).startswith("image/"):
        return True
    return (
        not media.get("type")
        and not media.get("medium")
        and bool(_IMAGE_EXT.search(media.get("url") or media.get("href") or ""))
    )


def _image_url(entry: dict[str, Any], summary: str | None, content: list[Any]) -> str | None:
    for media in entry.get("media_content") or []:
        if media.get("url") and _is_image(media):
            return media["url"]
    for thumb in entry.get("media_thumbnail") or []:
        if thumb.get("url"):
            return thumb["url"]
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and link.get("href") and _is_image(link):
            return link["href"]
    image = entry.get("image")
    if isinstance(image, dict) and image.get("href"):
        return image["href"]
    for key in ("og_image", "og:image", "twitter_image"):
        if isinstance(entry.get(key), str):
            return entry[key]
    html = " ".join([summary or "", *(c.get("value", "") for c in content)])
    match = _IMG_SRC.search(html)
    return match.group(1) if match else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, time.struct_time):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)
