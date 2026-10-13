"""Guardian Open Platform content search (https://open-platform.theguardian.com)."""

from typing import Any

from app.config import Source, SourceType
from app.sources.api import JsonApiAdapter, api_key, as_int, parse_datetime
from app.sources.base import FetchError, FetchState, QuotaExhausted, RawArticle, register

GUARDIAN_API = "https://content.guardianapis.com"


@register(SourceType.GUARDIAN)
class GuardianAdapter(JsonApiAdapter):
    provider = "Guardian"

    async def fetch(self, source: Source, state: FetchState | None = None) -> list[RawArticle]:
        key = api_key(source)
        quota = self.quota(state)
        p = source.params
        base = str(p.get("base_url") or GUARDIAN_API).rstrip("/")
        max_pages = as_int(p.get("max_pages"), 1, 1, 10)
        query = {
            "q": p.get("q"),
            "section": p.get("section"),
            "from-date": p.get("from_date"),
            "page-size": as_int(p.get("page_size"), 20, 1, 50),
            "order-by": "newest",
            "show-fields": "trailText,thumbnail,bodyText,byline",
        }
        query = {k: v for k, v in query.items() if v not in (None, "")}
        items: list[RawArticle] = []
        page = 1
        while True:
            try:
                body = await self.get_json(
                    f"{base}/search",
                    params={**query, "page": page},
                    headers={"api-key": key},
                    secret=key,
                    quota=quota,
                )
            except QuotaExhausted:
                if items:  # budget ran out mid-pagination: keep what was fetched
                    return items
                raise
            response = body.get("response") if isinstance(body, dict) else None
            if not isinstance(response, dict) or not isinstance(response.get("results"), list):
                raise FetchError("Guardian returned an unexpected JSON shape")
            if response.get("status") not in (None, "ok"):
                raise FetchError(f"Guardian error: {response.get('message') or 'unknown'}")
            items.extend(_item(r) for r in response["results"] if isinstance(r, dict))
            pages = as_int(response.get("pages"), 1, 0, 10**6)
            if page >= pages or page >= max_pages:
                return items
            page += 1

    def error_detail(self, body: Any) -> str | None:
        if isinstance(body, dict):
            inner = body.get("response")
            if isinstance(inner, dict) and inner.get("message"):
                return str(inner["message"])
            if body.get("message"):
                return str(body["message"])
        return None

    def is_quota_error(self, body: Any) -> bool:
        detail = (self.error_detail(body) or "").lower()
        return (
            "quota" in detail or "daily" in detail or ("limit" in detail and "rate" not in detail)
        )


def _item(result: dict[str, Any]) -> RawArticle:
    fields = result.get("fields") if isinstance(result.get("fields"), dict) else {}
    body = (fields.get("bodyText") or "").strip()
    return RawArticle(
        url=result.get("webUrl"),
        title=result.get("webTitle"),
        summary=fields.get("trailText"),
        author=fields.get("byline"),
        published_at=parse_datetime(result.get("webPublicationDate")),
        published_raw=result.get("webPublicationDate"),
        image_url=fields.get("thumbnail"),
        raw={k: result.get(k) for k in ("id", "sectionName", "webPublicationDate")},
        text=body or None,
    )
