"""NewsData.io latest news (https://newsdata.io/documentation)."""

from typing import Any

from app.config import Source, SourceType
from app.sources.api import JsonApiAdapter, api_key, as_int, parse_datetime
from app.sources.base import FetchError, FetchState, RawArticle, register

NEWSDATA_API = "https://newsdata.io"
_PAID_ONLY = "ONLY AVAILABLE IN PAID PLANS"


@register(SourceType.NEWSDATA)
class NewsDataAdapter(JsonApiAdapter):
    provider = "NewsData"

    async def fetch(self, source: Source, state: FetchState | None = None) -> list[RawArticle]:
        key = api_key(source)
        quota = self.quota(state)
        p = source.params
        base = str(p.get("base_url") or NEWSDATA_API).rstrip("/")
        max_pages = as_int(p.get("max_pages"), 1, 1, 10)
        query = {k: _csv(p.get(k)) for k in ("q", "country", "language", "category")}
        query = {k: v for k, v in query.items() if v}
        items: list[RawArticle] = []
        page_token: str | None = None
        for _ in range(max_pages):
            params = {**query, **({"page": page_token} if page_token else {})}
            body = await self.get_json(
                f"{base}/api/1/latest",
                params=params,
                headers={"X-ACCESS-KEY": key},
                secret=key,
                quota=quota,
            )
            if not isinstance(body, dict):
                raise FetchError("NewsData returned an unexpected JSON shape")
            if body.get("status") != "success":
                raise FetchError(f"NewsData error: {self.error_detail(body) or 'unknown'}")
            results = body.get("results")
            if not isinstance(results, list):
                raise FetchError("NewsData returned an unexpected JSON shape")
            items.extend(_item(r) for r in results if isinstance(r, dict))
            page_token = body.get("nextPage") or None
            if not page_token:
                break
        return items

    def error_detail(self, body: Any) -> str | None:
        if isinstance(body, dict):
            results = body.get("results")
            if isinstance(results, dict):
                code, message = results.get("code"), results.get("message")
                return ": ".join(str(x) for x in (code, message) if x) or None
            if body.get("message"):
                return str(body["message"])
        return None

    def is_quota_error(self, body: Any) -> bool:
        detail = (self.error_detail(body) or "").lower()
        return "apilimitexceeded" in detail or "quota" in detail or "credit" in detail


def _csv(value: Any) -> str | None:
    if isinstance(value, list | tuple):
        return ",".join(str(v) for v in value if v) or None
    return str(value) if value else None


def _item(result: dict[str, Any]) -> RawArticle:
    content = result.get("content")
    content = content.strip() if isinstance(content, str) and _PAID_ONLY not in content else ""
    creators = result.get("creator")
    published = result.get("pubDate")
    return RawArticle(
        url=result.get("link"),
        title=result.get("title"),
        summary=result.get("description"),
        author=", ".join(creators) if isinstance(creators, list) and creators else None,
        # pubDate is "YYYY-MM-DD HH:MM:SS" in UTC (pubDateTZ).
        published_at=parse_datetime(published.replace(" ", "T") if published else None),
        published_raw=published,
        image_url=result.get("image_url"),
        raw={k: result.get(k) for k in ("article_id", "source_id", "category", "country")},
        text=content if len(content.split()) >= 150 else None,
    )
