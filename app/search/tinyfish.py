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


def _str(value) -> str:
    return value.strip() if isinstance(value, str) else ""


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

    def optional_key(self) -> str:
        return os.environ.get(self.key_env or "", "").strip()

    async def search(self, query, since, language, limit, quota: Quota) -> list[SearchHit]:
        key = self.optional_key()
        params = {
            "query": query,
            "domain_type": self.config.search_type,
            "after_date": since.date().isoformat(),
            "purpose": PURPOSE,
        }
        if language:
            params["language"] = language
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
        for r in results:
            if not isinstance(r, dict) or not _str(r.get("url")).startswith("http"):
                continue
            hits.append(
                SearchHit(
                    url=_str(r["url"]),
                    title=_str(r.get("title")),
                    snippet=_str(r.get("snippet")) or _str(r.get("content")),
                    domain=_str(r.get("site_name")),
                    published_at=parse_datetime(_str(r.get("date")) or None),
                    language=language,
                )
            )
        return hits[:limit]
