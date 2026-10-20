"""Exa search provider (https://api.exa.ai): semantic search + ``find_similar``.

Off by default — opt in via a profile's ``enrichment``/``context`` provider list:
``{name: exa}``. The API key comes from ``EXA_API_KEY`` (or ``api_key``); requests are
POSTs (``/search`` and ``/findSimilar``), so this provider calls ``self.client.post``
directly with ``quota.before_request()`` and maps errors itself.
"""

import json
from datetime import datetime
from typing import Any

from app.search.base import (
    NotAuthorized,
    SearchHit,
    SearchProvider,
    TransientError,
    register,
)
from app.sources.api import parse_datetime, retry_after_seconds, scrub
from app.sources.base import FetchError, Quota, RateLimited

EXA_API = "https://api.exa.ai"


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


@register("exa")
class ExaProvider(SearchProvider):
    default_key_env = "EXA_API_KEY"
    needs_key = True
    supports_find_similar = True

    async def _post(self, path: str, body: dict, quota: Quota) -> dict:
        key = self.key()
        await quota.before_request()
        resp = await self.client.post(
            f"{self.config.base_url or EXA_API}{path}",
            json=body,
            headers={"x-api-key": key, "Accept": "application/json"},
        )
        self.last_raw = scrub(resp.text[:4000], key)
        if resp.status_code == 429:
            raise RateLimited("exa: rate limited", retry_after=retry_after_seconds(resp.headers))
        if resp.status_code in (401, 403):
            raise NotAuthorized("exa: API key rejected", retry_after=3600.0)
        if resp.status_code >= 400:
            raise FetchError(f"exa: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            raise TransientError("exa: malformed JSON") from None
        if not isinstance(data, dict):
            raise TransientError("exa: unexpected JSON shape")
        return data

    def _hits(self, data: dict, limit: int) -> list[SearchHit]:
        results = data.get("results")
        if not isinstance(results, list):
            raise TransientError("exa: missing results")
        hits = []
        for r in results:
            if not isinstance(r, dict) or not _str(r.get("url")):
                continue
            hits.append(
                SearchHit(
                    url=r["url"],
                    title=_str(r.get("title")),
                    snippet=_str(r.get("text")) or _str(r.get("summary")),
                    published_at=parse_datetime(r.get("publishedDate")),
                    image_url=_str(r.get("image")) or None,
                )
            )
        return hits[:limit]

    async def search(
        self,
        query: str,
        since: datetime,
        language: str | None,
        limit: int,
        quota: Quota,
        exclude=(),
        *,
        page: int = 0,
        location: str | None = None,
        include=(),
    ) -> list[SearchHit]:
        body: dict[str, Any] = {
            "query": query,
            "numResults": min(limit, 100),
            "type": "auto",
            "startPublishedDate": since.isoformat(),
        }
        if exclude:
            body["excludeDomains"] = list(exclude)
        return self._hits(await self._post("/search", body, quota), limit)

    async def find_similar(
        self, url: str, since: datetime, language: str | None, limit: int, quota: Quota
    ) -> list[SearchHit]:
        return self._hits(
            await self._post(
                "/findSimilar",
                {"url": url, "numResults": min(limit, 100)},
                quota,
            ),
            limit,
        )
