"""Page metadata the link finders need beyond the extracted text: the publication
date (for the same-event window check and the prior-date ranking preference) and
opinion hints (``og:type`` / ``article:section``).

Publication date order: ``article:published_time`` / ``og:article:published_time``
meta, JSON-LD ``datePublished``, ``<meta name="date">`` / ``itemprop="datePublished"``,
then the first ``<time datetime>``. Never raises — an unparseable page has no meta.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lxml import html as lxml_html

_DATE_META = (
    "article:published_time",
    "og:article:published_time",
    "datepublished",
    "date",
    "pubdate",
    "parsely-pub-date",
    "publishdate",
    "dc.date",
    "dc.date.issued",
)
_TYPE_META = ("og:type",)
_SECTION_META = ("article:section", "section", "parsely-section")


@dataclass(frozen=True)
class PageMeta:
    published_at: datetime | None = None
    """Best-effort publication date (aware UTC); None when the page declares none."""
    og_type: str = ""
    """``og:type`` lower-cased (``article``, ``website``, ...)."""
    section: str = ""
    """``article:section`` / section meta, lower-cased (``opinion``, ``tech``, ...)."""


def _parse_date(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # drop a weekday prefix ("Mon, 2026-10-19T..." is not ISO)
    if "," in text[:12]:
        text = text.split(",", 1)[1].strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _ld_nodes(data: Any):
    if isinstance(data, list):
        for item in data:
            yield from _ld_nodes(item)
    elif isinstance(data, dict):
        yield data
        if "@graph" in data:
            yield from _ld_nodes(data["@graph"])


def _ld_date(tree) -> datetime | None:
    for script in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(script.text_content() or "")
        except (ValueError, TypeError):
            continue
        for node in _ld_nodes(data):
            value = node.get("datePublished") or node.get("dateCreated")
            if isinstance(value, dict):
                value = value.get("@value") or value.get("url")
            found = _parse_date(value)
            if found:
                return found
    return None


def _meta_content(tree, names: tuple[str, ...]) -> str:
    """First ``content`` of a ``<meta>`` matching ``names`` (case-insensitive) as
    ``property``, ``name`` or ``itemprop``."""
    for name in names:
        for attr in ("property", "name", "itemprop"):
            nodes = tree.xpath(
                f'//meta[translate(@{attr}, "ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                f' "abcdefghijklmnopqrstuvwxyz")="{name}"]/@content'
            )
            if nodes:
                return str(nodes[0]).strip()
    return ""


def page_meta(html: bytes | str, url: str = "") -> PageMeta:
    """Never raises: a page we cannot parse simply has no metadata."""
    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return PageMeta()
    published = _parse_date(_meta_content(tree, _DATE_META))
    if published is None:
        published = _ld_date(tree)
    if published is None:
        for node in tree.xpath("//time[@datetime]") + tree.xpath("//time[@pubdate]"):
            published = _parse_date(node.get("datetime") or node.get("pubdate"))
            if published:
                break
    og_type = _meta_content(tree, _TYPE_META).lower()
    section = _meta_content(tree, _SECTION_META).lower()
    return PageMeta(published_at=published, og_type=og_type, section=section)


# --- opinion hints ------------------------------------------------------------

OPINION_PATHS = (
    "/opinion/",
    "/opinions/",
    "/commentisfree/",
    "/columnists/",
    "/columns/",
    "/commentary/",
    "/perspective/",
    "/mnenia/",  # fakti.bg opinion section
    "/blog/",
)
"""URL path segments that mark opinion/column pages (mirrors the enrichment
``skip_url_patterns`` vocabulary plus hosts seen in the context eval)."""

OPINION_HOSTS = (
    "substack.com",
    "beehiiv.com",
    "buttondown.com",
    "buttondown.email",
    "ghost.io",
    "medium.com",
    "wordpress.com",
    "blogspot.com",
    "typepad.com",
)
"""Personal-newsletter/blog hosts: everything on them is opinion/advocacy, not
reported news (most are already social-blocked; the list also guards custom
``social_domains`` setups)."""

OPINION_SECTIONS = (
    "opinion",
    "opinions",
    "comment",
    "commentary",
    "commentisfree",
    "editorial",
    "editorials",
    "column",
    "columns",
    "perspective",
    "perspectives",
    "blogs",
    "blog",
)
"""``article:section`` / section-meta values treated as opinion."""

_OPINION_RE = re.compile(r"opinion|comment|editorial|column|perspective|blog")


def is_opinion_page(url: str, meta: PageMeta | None = None) -> bool:
    """URL path segments, newsletter hosts and ``og:type``/``article:section``:
    any hit marks the page opinion rather than reported news."""
    from urllib.parse import urlsplit

    from app.enrich.domains import host, registrable_domain

    name = host(url)
    domain = registrable_domain(name)
    if domain in OPINION_HOSTS or name.endswith(".substack.com"):
        return True
    low = urlsplit(url).path.lower() + "/"
    if any(seg in low for seg in OPINION_PATHS):
        return True
    if meta is not None:
        if meta.og_type and _OPINION_RE.search(meta.og_type):
            return True
        section = meta.section
        if section and (section in OPINION_SECTIONS or _OPINION_RE.search(section)):
            return True
    return False
