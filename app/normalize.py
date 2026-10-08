"""Turn adapter output (RawArticle) into the normalized fields stored on Article."""

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from app.sources.base import RawArticle

# Only these (plus utm_*) are dropped from the dedup key; every other param can matter.
_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src"}
_DEFAULT_PORTS = {"http": 80, "https": 443}
_WS = re.compile(r"\s+")
_DOMAIN_PREFIXES = ("www.", "m.", "amp.", "mobile.")


class _TextExtractor(HTMLParser):
    _SKIP: ClassVar[set[str]] = {"script", "style", "template", "noscript"}
    _BLOCK: ClassVar[set[str]] = {
        *("p", "div", "br", "li", "ul", "ol", "tr", "blockquote"),
        *("h1", "h2", "h3", "h4", "h5", "h6"),
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def clean_text(value: str | None) -> str:
    """Unescape entities and collapse all whitespace runs to single spaces."""
    return _WS.sub(" ", html.unescape(value or "")).strip()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return clean_text("".join(parser.parts))


def clean_url(url: str, base: str | None = None) -> str:
    """The feed's permalink with minimal cleanup: trimmed and resolved against the feed URL."""
    url = url.strip()
    return urljoin(base, url) if base else url


def canonicalize_url(url: str, base: str | None = None) -> str:
    """Dedup key only, never a link: lowercase scheme/host, drop default port, fragment and
    tracking params (utm_*, fbclid, ...). The path (trailing slash included) and all other
    params are kept; params are sorted."""
    parts = urlsplit(clean_url(url, base))
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port and parts.port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    if parts.username:
        userinfo = parts.username + (f":{parts.password}" if parts.password else "")
        host = f"{userinfo}@{host}"
    path = parts.path or "/"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def source_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for prefix in _DOMAIN_PREFIXES:
        if host.startswith(prefix):
            return host[len(prefix) :]
    return host


def normalize_title(title: str) -> str:
    return _WS.sub(" ", re.sub(r"[^\w\s]", " ", title.casefold())).strip()


def content_hash(title: str, url: str) -> str:
    key = f"{normalize_title(title)}|{source_domain(url)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def to_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parse_date(value: str | None) -> datetime | None:
    """Best-effort RFC 822 / ISO 8601 parsing; returns aware UTC or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return to_utc(parsedate_to_datetime(value))
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


@dataclass(frozen=True)
class NormalizedArticle:
    source_id: str
    url: str
    canonical_url: str
    title: str
    summary_raw: str
    author: str | None
    published_at: datetime
    fetched_at: datetime
    image_url: str | None
    content_hash: str
    raw_json: dict[str, Any]


def normalize(
    raw: RawArticle, *, source_id: str, fetched_at: datetime, base_url: str | None = None
) -> NormalizedArticle | None:
    """Returns None if the item has no usable http(s) URL (it can't be deduplicated)."""
    if not raw.url or not raw.url.strip():
        return None
    url = clean_url(raw.url, base_url)
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        return None
    canonical = canonicalize_url(url)
    title = strip_html(raw.title)
    published = raw.published_at and to_utc(raw.published_at)
    image_url = urljoin(base_url or "", raw.image_url.strip()) if raw.image_url else None
    return NormalizedArticle(
        source_id=source_id,
        url=url,
        canonical_url=canonical,
        title=title,
        summary_raw=strip_html(raw.summary),
        author=clean_text(raw.author) or None,
        published_at=published or parse_date(raw.published_raw) or to_utc(fetched_at),
        fetched_at=to_utc(fetched_at),
        image_url=image_url,
        content_hash=content_hash(title, canonical),
        raw_json=raw.raw,
    )
