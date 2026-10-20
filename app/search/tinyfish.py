"""TinyFish Search API (https://docs.tinyfish.ai/search-api/reference): the primary
enrichment provider. Keyword search with titles, snippets and (for ``news``) dates.

``GET https://api.search.tinyfish.ai?query=...`` with the key in ``X-API-Key``
(``TINYFISH_API_KEY``; free up to 12,000 requests a day, 30 requests / minute). Without a
key the request is sent without the header (keyless mode, e.g. through a proxy
``base_url``); TinyFish itself answers 401 ``MISSING_API_KEY`` then, which is reported as a
configuration error and the next provider is tried.
"""

import json
import os
import re
from datetime import UTC, datetime, timedelta

import httpx

from app.search.base import (
    NotAuthorized,
    ProviderTimeout,
    QueryRejected,
    SearchHit,
    SearchProvider,
    TransientError,
    register,
)
from app.sources.api import parse_datetime, retry_after_seconds
from app.sources.base import FetchError, Quota, QuotaExhausted, RateLimited

TINYFISH_API = "https://api.search.tinyfish.ai"
PURPOSE = "Find other news outlets' coverage of the same news event"

_RELATIVE = re.compile(
    r"^\s*(?:(\d+(?:\.\d+)?)|an?|one)\s+"
    r"(second|minute|hour|day|week|month|year)s?\s+ago\s*$",
    re.IGNORECASE,
)
_UNITS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 7 * 86400,
    "month": 30 * 86400,
    "year": 365 * 86400,
}


def published_date(value: str, *, now: datetime | None = None) -> datetime | None:
    """TinyFish dates: ISO strings, or relative ones like ``7 hours ago`` / ``3 weeks ago``
    (measured from ``now``, defaulting to the request time)."""
    parsed = parse_datetime(value or None)
    if parsed is not None:
        return parsed
    match = _RELATIVE.match(value or "")
    if not match:
        return None
    amount = float(match.group(1)) if match.group(1) else 1.0
    return (now or datetime.now(UTC)) - timedelta(seconds=amount * _UNITS[match.group(2).lower()])


def _str(value) -> str:
    return value.strip() if isinstance(value, str) else ""


_SNIPPET_KEYS = ("snippet", "content", "description", "summary", "text", "excerpt")
"""Keys TinyFish has used for a result's text blurb, in preference order. The documented
``news`` shape carries ``snippet``; if a future shape renames it the others keep it
working, and ``fields`` records which keys actually came back."""
_FIELDS = [*_SNIPPET_KEYS, "date", "publisher", "site_name", "authors"]
"""Known response keys: counted separately so a key that comes back empty (``snippet:``)
is visible next to ``snippet`` — and every other raw key is counted by presence anyway,
so a renamed field shows up in ``fields`` too."""


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    return _str(error.get("code")) if isinstance(error, dict) else ""


@register("tinyfish")
class TinyFishProvider(SearchProvider):
    default_key_env = "TINYFISH_API_KEY"
    supports_exclude = True
    """``exclude_domains`` keeps the story's own publishers out of the results."""
    supports_pages = True
    """``page`` 0-10: the deep tier fetches page 1 for more outlets."""
    supports_location = True
    """``location`` (country code): the deep tier repeats its strongest query per
    configured country for regional outlets."""
    supports_include = True
    """``include_domains``: ensure-groups queries restrict hits to a group's domains."""
    supports_before = True
    """``before`` -> ``before_date`` (combinable with ``after_date``)."""
    supports_domain_type = True
    """``domain_type``: per-query ``news`` | ``web`` | ``research_paper``."""
    supports_pub_years = True
    """``pub_year_min``/``pub_year_max`` for ``research_paper`` queries."""

    def optional_key(self) -> str:
        return os.environ.get(self.key_env or "", "").strip()

    async def search(
        self,
        query,
        since,
        language,
        limit,
        quota: Quota,
        exclude=(),
        *,
        page: int = 0,
        location: str | None = None,
        include=(),
        before=None,
        domain_type: str | None = None,
        pub_year_min: int | None = None,
        pub_year_max: int | None = None,
    ) -> list[SearchHit]:
        key = self.optional_key()
        params = {
            "query": query,
            "domain_type": domain_type or self.config.search_type,
            "purpose": PURPOSE,
        }
        if since is not None:
            params["after_date"] = since.date().isoformat()
        if before is not None:
            params["before_date"] = before.date().isoformat()
        if pub_year_min is not None:
            params["pub_year_min"] = pub_year_min
        if pub_year_max is not None:
            params["pub_year_max"] = pub_year_max
        if language:
            params["language"] = language
        if exclude:
            params["exclude_domains"] = ",".join(dict.fromkeys(exclude))
        if include:
            params["include_domains"] = ",".join(dict.fromkeys(include))
        if page:
            params["page"] = page
        if location:
            params["location"] = location
        headers = {"Accept": "application/json"}
        if key:
            headers["X-API-Key"] = key
        quota.before_request()
        try:
            response = await self.client.get(
                self.config.base_url or TINYFISH_API, params=params, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"TinyFish request timed out ({type(exc).__name__})") from None
        except Exception as exc:
            raise FetchError(f"TinyFish request failed: {type(exc).__name__}: {exc}") from None
        self.last_raw = response.text[:4000]
        status = response.status_code
        if status == 429:
            raise RateLimited("TinyFish rate limit (HTTP 429)", retry_after_seconds(response))
        if status == 401:
            code = _error_code(response) or "unauthorized"
            hint = (
                f"set {self.key_env} (free tier: 12,000 requests a day)"
                if not key
                else f"check {self.key_env}"
            )
            raise NotAuthorized(f"TinyFish HTTP 401 {code}: {hint}")
        if status == 402:
            code = _error_code(response) or "payment required"
            raise QuotaExhausted(f"TinyFish daily allowance used ({code})")
        if status == 400:
            raise QueryRejected(f"TinyFish rejected the query: {response.text[:200]}")
        if status in (500, 502, 503, 504):
            raise TransientError(f"TinyFish HTTP {status}: {response.text[:200]}")
        if status >= 400:
            raise FetchError(f"TinyFish HTTP {status}: {response.text[:200]}")
        if not response.content.strip():
            raise TransientError("TinyFish returned an empty body")
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TransientError(
                f"TinyFish returned malformed JSON: {response.text[:200]}"
            ) from None
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise TransientError("TinyFish returned an unexpected JSON shape")
        hits = []
        fields: dict[str, int] = {}
        for r in results:
            if not isinstance(r, dict) or not _str(r.get("url")).startswith("http"):
                continue
            for key, value in r.items():
                name = key
                if key in _FIELDS and not _str(value):
                    name = f"{key}:empty"  # present but empty, e.g. "snippet": ""
                fields[name] = fields.get(name, 0) + 1
            # Every other key counts by presence too, so a renamed/new field shows up.
            hits.append(
                SearchHit(
                    url=_str(r["url"]),
                    title=_str(r.get("title")),
                    snippet=next((s for k in _SNIPPET_KEYS if (s := _str(r.get(k)))), ""),
                    domain=_str(r.get("site_name")),
                    published_at=published_date(_str(r.get("date"))),
                    language=language,
                )
            )
        self.last_fields = fields
        """Field counts across the raw results: non-empty for the known keys (``key:empty``
        when they come back blank) plus presence for every other key (evaluate/diagnose
        print them so an upstream shape change shows up instead of silently losing
        snippets)."""
        return hits[:limit]

    async def fetch_pages(self, urls: list[str], quota: Quota) -> dict[str, str]:
        """TinyFish Fetch API: POST ``{urls (1-10), format: markdown}`` -> clean
        extracted text per URL, rendering JS-heavy pages. Used by the context
        pipeline as an opt-in fallback for pages our extractor cannot read
        (``context.fetch_via``); robots-blocked/paywalled pages are never sent."""
        if not urls:
            return {}
        endpoint = self.config.fetch_url or self.config.base_url
        if not endpoint:
            raise FetchError("tinyfish fetch needs fetch_url (or base_url)")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        key = self.optional_key()
        if key:
            headers["X-API-Key"] = key
        quota.before_request()
        try:
            response = await self.client.post(
                endpoint, json={"urls": urls[:10], "format": "markdown"}, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"TinyFish fetch timed out ({type(exc).__name__})") from None
        except Exception as exc:
            raise FetchError(f"TinyFish fetch failed: {type(exc).__name__}: {exc}") from None
        self.last_raw = response.text[:4000]
        status = response.status_code
        if status == 429:
            raise RateLimited("TinyFish fetch rate limit (HTTP 429)", retry_after_seconds(response))
        if status == 401:
            raise NotAuthorized("TinyFish fetch HTTP 401 (check the API key)")
        if status == 402:
            raise QuotaExhausted("TinyFish fetch: daily allowance used")
        if status >= 400:
            raise FetchError(f"TinyFish fetch HTTP {status}: {response.text[:200]}")
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TransientError(
                f"TinyFish fetch returned malformed JSON: {response.text[:200]}"
            ) from None
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise TransientError("TinyFish fetch returned an unexpected JSON shape")
        out = {}
        for r in results:
            if not isinstance(r, dict):
                continue
            url = _str(r.get("url"))
            text = next((s for k in ("markdown", "content", "text") if (s := _str(r.get(k)))), "")
            if url.startswith("http") and text:
                out[url] = text
        return out
