"""Find a page's lead image and publisher icon in HTML the extractor already downloaded.

Order: ``og:image`` (``og:image:secure_url``), ``twitter:image``, JSON-LD ``image``, then
``trafilatura.extract_metadata``. Relative URLs are resolved against the page; only absolute
http(s) URLs are returned.
"""

import json
import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import trafilatura
from lxml import html as lxml_html

logger = logging.getLogger(__name__)

_IMAGE_META = (
    ("property", "og:image:secure_url"),
    ("property", "og:image"),
    ("property", "og:image:url"),
    ("name", "og:image"),
    ("name", "twitter:image"),
    ("name", "twitter:image:src"),
    ("property", "twitter:image"),
)
_ICON_RELS = ("apple-touch-icon", "apple-touch-icon-precomposed", "icon", "shortcut icon")
DOMAIN_ICON_PATHS = ("/apple-touch-icon.png", "/favicon.ico")


@dataclass(frozen=True)
class PageImages:
    image: str | None = None
    icon: str | None = None


def absolute_http(url: str | None, base: str) -> str | None:
    """``url`` resolved against ``base``; None unless it is an http(s) URL with a host."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url or url.startswith("data:"):
        return None
    resolved = urljoin(base, url)
    parts = urlsplit(resolved)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return resolved


def _ld_image(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return next((u for u in (_ld_image(v) for v in value) if u), None)
    if isinstance(value, dict):
        return _ld_image(value.get("url") or value.get("contentUrl") or value.get("@id"))
    return None


def _ld_nodes(data):
    if isinstance(data, list):
        for item in data:
            yield from _ld_nodes(item)
    elif isinstance(data, dict):
        yield data
        if "@graph" in data:
            yield from _ld_nodes(data["@graph"])


def _json_ld_image(tree) -> str | None:
    for script in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(script.text_content() or "")
        except (ValueError, TypeError):
            continue
        for node in _ld_nodes(data):
            found = _ld_image(node.get("image") or node.get("thumbnailUrl"))
            if found:
                return found
    return None


def page_images(html: bytes | str, url: str) -> PageImages:
    """Never raises: a page we cannot parse simply has no images."""
    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return PageImages()
    base = absolute_http(next(iter(tree.xpath("//base/@href")), None), url) or url
    image = None
    for attr, name in _IMAGE_META:
        for content in tree.xpath(f'//meta[translate(@{attr}, "OGTWIER", "ogtwier")="{name}"]'):
            image = absolute_http(content.get("content"), base)
            if image:
                break
        if image:
            break
    if image is None:
        image = absolute_http(_json_ld_image(tree), base)
    if image is None:
        try:
            meta = trafilatura.extract_metadata(html, default_url=url)
            image = absolute_http(meta.image if meta else None, base)
        except Exception:  # trafilatura is best effort here
            logger.debug("extract_metadata failed for %s", url, exc_info=True)
    icon = None
    links = tree.xpath("//link[@rel and @href]")
    for rel in _ICON_RELS:
        for link in links:
            if (link.get("rel") or "").lower().strip() == rel:
                icon = absolute_http(link.get("href"), base)
                if icon:
                    break
        if icon:
            break
    return PageImages(image=image, icon=icon)


def domain_icon_urls(page_url: str) -> list[str]:
    parts = urlsplit(page_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return []
    return [f"{parts.scheme}://{parts.netloc}{path}" for path in DOMAIN_ICON_PATHS]
