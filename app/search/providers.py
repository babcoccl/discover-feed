"""Built-in search providers: GDELT (default, no key), SearXNG, Guardian, NewsData, Brave."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.search.base import (
    LANGUAGE_NAMES,
    ProviderTimeout,
    QueryRejected,
    SearchHit,
    SearchProvider,
    TransientError,
    language_code,
    register,
)
from app.sources.api import parse_datetime, retry_after_seconds, scrub
from app.sources.base import FetchError, Quota, RateLimited
from app.sources.guardian import GUARDIAN_API, GuardianAdapter
from app.sources.newsdata import NEWSDATA_API, NewsDataAdapter

logger = logging.getLogger(__name__)

GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
BRAVE_API = "https://api.search.brave.com/res/v1/news/search"


def _hours_since(since: datetime) -> int:
    return max(1, int((datetime.now(UTC) - since).total_seconds() // 3600) + 1)


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


@register("gdelt")
class GdeltProvider(SearchProvider):
    """GDELT DOC 2.0 ``artlist`` (free, no key; asks for one request per 5 seconds)."""

    async def search(self, query, since, language, limit, quota: Quota) -> list[SearchHit]:
        q = query
        if language and language in LANGUAGE_NAMES:
            q = f"{query} sourcelang:{LANGUAGE_NAMES[language]}"
        params = {
            "query": q,
            "mode": "artlist",
            "format": "json",
            "maxrecords": min(limit, 250),
            "sort": "hybridrel",
            "timespan": f"{min(_hours_since(since), 24 * 90)}h",
        }
        quota.before_request()
        try:
            response = await self.client.get(self.config.base_url or GDELT_API, params=params)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"GDELT request timed out ({type(exc).__name__})") from None
        except Exception as exc:
            raise FetchError(f"GDELT request failed: {type(exc).__name__}: {exc}") from None
        self.last_raw = response.text[:4000]
        if response.status_code == 429:
            raise RateLimited("GDELT rate limit (HTTP 429)", retry_after_seconds(response))
        if response.status_code >= 400:
            raise FetchError(f"GDELT HTTP {response.status_code}")
        if not response.content.strip():  # transient: never cached as "no results"
            raise TransientError("GDELT returned an empty body")
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            text = response.text.strip()
            lower = text.lower()
            if "limit requests" in lower:  # GDELT answers 200 + plain text
                raise RateLimited(f"GDELT rate limit: {text[:200]}", 5.0) from None
            if "too short" in lower or "too long" in lower or "too common" in lower:
                raise QueryRejected(f"GDELT rejected the query: {text[:200]}") from None
            logger.warning("GDELT answered plain text for %r: %s", query, text[:2000])
            raise TransientError(f"GDELT error: {text[:200] or 'not JSON'}") from None
        articles = body.get("articles", []) if isinstance(body, dict) else None
        if not isinstance(articles, list):
            raise FetchError("GDELT returned an unexpected JSON shape")
        hits = []
        for a in articles:
            if not isinstance(a, dict) or not _str(a.get("url")):
                continue
            seen = None
            if _str(a.get("seendate")):
                try:
                    seen = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
                except ValueError:
                    seen = None
            hits.append(
                SearchHit(
                    url=a["url"],
                    title=_str(a.get("title")),
                    domain=_str(a.get("domain")),
                    published_at=seen,
                    image_url=_str(a.get("socialimage")) or None,
                    language=language_code(a.get("language")),
                )
            )
        return hits[:limit]


@register("searxng")
class SearxngProvider(SearchProvider):
    """A self-hosted SearXNG instance (``json`` must be listed in ``search.formats``)."""

    async def search(self, query, since, language, limit, quota: Quota) -> list[SearchHit]:
        hours = _hours_since(since)
        time_range = "day" if hours <= 24 else "week" if hours <= 24 * 7 else "month"
        params = {"q": query, "format": "json", "categories": "news", "time_range": time_range}
        if language:
            params["language"] = language
        base = str(self.config.base_url or "").rstrip("/")
        quota.before_request()
        try:
            response = await self.client.get(f"{base}/search", params=params)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"SearXNG request timed out ({type(exc).__name__})") from None
        except Exception as exc:
            raise FetchError(f"SearXNG request failed: {type(exc).__name__}: {exc}") from None
        self.last_raw = response.text[:4000]
        if response.status_code == 429:
            raise RateLimited("SearXNG rate limit (HTTP 429)", retry_after_seconds(response))
        if response.status_code == 403:
            raise FetchError(
                "SearXNG HTTP 403: enable the json output format "
                "(search.formats: [html, json] in settings.yml)"
            )
        if response.status_code >= 400:
            raise FetchError(f"SearXNG HTTP {response.status_code}")
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise FetchError("SearXNG did not return JSON (is format=json enabled?)") from None
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise FetchError("SearXNG returned an unexpected JSON shape")
        hits = []
        for r in results:
            if not isinstance(r, dict) or not _str(r.get("url")):
                continue
            hits.append(
                SearchHit(
                    url=r["url"],
                    title=_str(r.get("title")),
                    snippet=_str(r.get("content")),
                    published_at=parse_datetime(r.get("publishedDate")),
                    image_url=_str(r.get("img_src")) or _str(r.get("thumbnail")) or None,
                )
            )
        return hits[:limit]


@register("guardian")
class GuardianSearchProvider(SearchProvider):
    """Guardian Open Platform search (same key and error handling as the feed adapter)."""

    default_key_env = "GUARDIAN_API_KEY"
    needs_key = True

    async def search(self, query, since, language, limit, quota: Quota) -> list[SearchHit]:
        key = self.key()
        base = str(self.config.base_url or GUARDIAN_API).rstrip("/")
        body = await GuardianAdapter(self.client).get_json(
            f"{base}/search",
            params={
                "q": query,
                "from-date": since.date().isoformat(),
                "page-size": min(limit, 50),
                "order-by": "relevance",
                "show-fields": "trailText,thumbnail",
            },
            headers={"api-key": key},
            secret=key,
            quota=quota,
        )
        self.remember(body)
        response = body.get("response") if isinstance(body, dict) else None
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise FetchError("Guardian returned an unexpected JSON shape")
        hits = []
        for r in response["results"]:
            if not isinstance(r, dict) or not _str(r.get("webUrl")):
                continue
            fields = r.get("fields") if isinstance(r.get("fields"), dict) else {}
            hits.append(
                SearchHit(
                    url=r["webUrl"],
                    title=_str(r.get("webTitle")),
                    snippet=_str(fields.get("trailText")),
                    published_at=parse_datetime(r.get("webPublicationDate")),
                    image_url=_str(fields.get("thumbnail")) or None,
                    language="en",
                )
            )
        return hits[:limit]


@register("newsdata")
class NewsDataSearchProvider(SearchProvider):
    """NewsData.io ``latest`` search (same key and error handling as the feed adapter)."""

    default_key_env = "NEWSDATA_API_KEY"
    needs_key = True

    async def search(self, query, since, language, limit, quota: Quota) -> list[SearchHit]:
        key = self.key()
        base = str(self.config.base_url or NEWSDATA_API).rstrip("/")
        params = {"q": query[:100]}
        if language:
            params["language"] = language
        body = await NewsDataAdapter(self.client).get_json(
            f"{base}/api/1/latest",
            params=params,
            headers={"X-ACCESS-KEY": key},
            secret=key,
            quota=quota,
        )
        self.remember(body)
        if not isinstance(body, dict) or body.get("status") != "success":
            raise FetchError("NewsData returned an error or unexpected JSON shape")
        results = body.get("results")
        if not isinstance(results, list):
            raise FetchError("NewsData returned an unexpected JSON shape")
        hits = []
        for r in results:
            if not isinstance(r, dict) or not _str(r.get("link")):
                continue
            published = _str(r.get("pubDate"))
            hits.append(
                SearchHit(
                    url=r["link"],
                    title=_str(r.get("title")),
                    snippet=_str(r.get("description")),
                    published_at=parse_datetime(published.replace(" ", "T") or None),
                    image_url=_str(r.get("image_url")) or None,
                    language=language_code(r.get("language")),
                )
            )
        return hits[:limit]


@register("brave")
class BraveProvider(SearchProvider):
    """Brave Search news API (paid per request: set ``cost_per_request_usd`` and a cap)."""

    default_key_env = "BRAVE_API_KEY"
    needs_key = True

    async def search(self, query, since, language, limit, quota: Quota) -> list[SearchHit]:
        key = self.key()
        hours = _hours_since(since)
        freshness = "pd" if hours <= 24 else "pw" if hours <= 24 * 7 else "pm"
        params = {"q": query, "count": min(limit, 50), "freshness": freshness}
        if language:
            params["search_lang"] = language
        body = await self.json_client().get_json(
            self.config.base_url or BRAVE_API,
            params=params,
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            secret=key,
            quota=quota,
        )
        self.last_raw = scrub(json.dumps(body, ensure_ascii=False)[:4000], key)
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise FetchError("Brave returned an unexpected JSON shape")
        hits = []
        for r in results:
            if not isinstance(r, dict) or not _str(r.get("url")):
                continue
            meta = r.get("meta_url") if isinstance(r.get("meta_url"), dict) else {}
            thumb = r.get("thumbnail") if isinstance(r.get("thumbnail"), dict) else {}
            hits.append(
                SearchHit(
                    url=r["url"],
                    title=_str(r.get("title")),
                    snippet=_str(r.get("description")),
                    domain=_str(meta.get("hostname")),
                    published_at=parse_datetime(r.get("page_age")),
                    image_url=_str(thumb.get("src")) or None,
                )
            )
        return hits[:limit]
