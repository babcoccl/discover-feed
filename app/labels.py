"""Display labels of a cited article: the publisher (the article URL's host) and, when the
feed is an aggregator (``aggregator: true`` on the source), the feed it came "via".

Pure functions shared by the write side (prompts, published views) and the read side."""

import ipaddress
import re
from collections.abc import Iterable

import httpx

_GENERIC_LABELS = frozenset(
    "www m amp blog blogs news press newsroom investor investors ir about corporate media "  # noqa: SIM905
    "en us uk www2 home site web ai research engineering developer developers static "
    "com org net gov edu io co ac info biz".split()
)


def _host(url: str) -> str:
    try:
        host = httpx.URL(url).host or ""
    except (httpx.InvalidURL, TypeError, ValueError):
        return ""
    return host.lower().rstrip(".")


def _is_local(host: str) -> bool:
    if not host or "." not in host or host.endswith((".local", ".localhost", ".internal")):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def publisher_of(url: str, fallback: str = "") -> str:
    """``https://www.theverge.com/x`` -> ``theverge.com``; local hosts (``localhost``, IPs,
    single-label names such as the demo's) have no meaningful domain: ``fallback``."""
    host = _host(url)
    if _is_local(host):
        return fallback or host
    return host.removeprefix("www.")


def via_of(
    source_id: str, source_name: str, publisher: str, aggregators: Iterable[str]
) -> str | None:
    """The aggregator feed's name when the source is an aggregator and the publisher differs."""
    if source_id not in set(aggregators) or not source_name or publisher == source_name:
        return None
    return source_name


def label(publisher: str, via: str | None) -> str:
    return f"{publisher} via {via}" if via else publisher


def brand_labels(publisher: str) -> list[str]:
    """Distinctive parts of a publisher domain (``ai.meta.com`` -> ``["meta"]``,
    ``federalreserve.gov`` -> ``["federalreserve"]``)."""
    parts = publisher.lower().split(".")
    if len(parts) < 2:
        return []
    return [p for p in parts[:-1] if len(p) >= 3 and p not in _GENERIC_LABELS]


def names_subject(publisher: str, text: str) -> bool:
    """True when ``text`` (a headline) names the publisher: one of its brand labels appears
    as a word, or (labels of 8+ letters, e.g. ``federalreserve``) with spaces removed."""
    low = text.lower()
    words = set(re.findall(r"[a-z0-9]+", low))
    compact = re.sub(r"[^a-z0-9]", "", low)
    return any(b in words or (len(b) >= 8 and b in compact) for b in brand_labels(publisher))
