"""X (Twitter) recent search, opt-in and spend-capped.

Posts are billed per read: each run asks for at most ``max_posts_per_run`` posts and never
more than ``monthly_budget_usd`` still allows at ``cost_per_read_usd``; with no cap set it
does not call the API at all.
"""

from typing import Any

from app.config import Source, SourceType
from app.sources.api import JsonApiAdapter, api_key, parse_datetime
from app.sources.base import FetchError, FetchState, QuotaExhausted, RawArticle, register

X_API = "https://api.x.com"
_MIN_RESULTS = 10  # recent search rejects max_results below 10


@register(SourceType.X)
class XAdapter(JsonApiAdapter):
    provider = "X"

    async def fetch(self, source: Source, state: FetchState | None = None) -> list[RawArticle]:
        if source.monthly_budget_usd is None:
            raise QuotaExhausted("X source has no monthly_budget_usd: not fetching")
        quota = self.quota(state)
        quota.monthly_budget_usd = source.monthly_budget_usd
        quota.cost_per_read_usd = source.cost_per_read_usd
        wanted = max(_MIN_RESULTS, min(100, source.max_posts_per_run or _MIN_RESULTS))
        allowed = quota.reads_allowed()
        if allowed < wanted:
            raise QuotaExhausted(
                f"monthly X budget reached (${quota.spent_usd:.2f} of "
                f"${source.monthly_budget_usd:.2f}); resumes next month (UTC)"
            )
        token = api_key(source)
        p = source.params
        base = str(p.get("base_url") or X_API).rstrip("/")
        body = await self.get_json(
            f"{base}/2/tweets/search/recent",
            params={
                "query": build_query(p),
                "max_results": wanted,
                "tweet.fields": "created_at,author_id,lang",
                "expansions": "author_id",
                "user.fields": "username,name",
            },
            headers={"Authorization": f"Bearer {token}"},
            secret=token,
            quota=quota,
        )
        if not isinstance(body, dict):
            raise FetchError("X returned an unexpected JSON shape")
        posts = body.get("data") or []
        if not isinstance(posts, list):
            raise FetchError("X returned an unexpected JSON shape")
        posts = [post for post in posts if isinstance(post, dict)][:wanted]
        quota.record_reads(len(posts))
        users = {
            u.get("id"): u
            for u in (body.get("includes") or {}).get("users") or []
            if isinstance(u, dict)
        }
        return [_item(post, users.get(post.get("author_id")) or {}) for post in posts]

    def error_detail(self, body: Any) -> str | None:
        if isinstance(body, dict):
            parts = [body.get("title"), body.get("detail")]
            errors = body.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                parts.append(errors[0].get("message"))
            return ": ".join(str(x) for x in parts if x) or None
        return None

    def is_quota_error(self, body: Any) -> bool:
        detail = (self.error_detail(body) or "").lower()
        return "usagecap" in detail.replace(" ", "") or "credits" in detail


def build_query(params: dict[str, Any]) -> str:
    users = params.get("users") or []
    if isinstance(users, str):
        users = [u for u in users.replace(",", " ").split() if u]
    parts = []
    if params.get("query"):
        parts.append(f"({params['query']})" if users else str(params["query"]))
    if users:
        parts.append("(" + " OR ".join(f"from:{u.lstrip('@')}" for u in users) + ")")
    return " ".join(parts) + " -is:retweet"


def _item(post: dict[str, Any], user: dict[str, Any]) -> RawArticle:
    text = str(post.get("text") or "").strip()
    username = user.get("username") or "i"
    first_line = text.splitlines()[0] if text else ""
    title = first_line if len(first_line) <= 120 else first_line[:117].rstrip() + "..."
    return RawArticle(
        url=f"https://x.com/{username}/status/{post.get('id')}",
        title=title,
        summary=text,
        author=f"@{username}" if user.get("username") else None,
        published_at=parse_datetime(post.get("created_at")),
        published_raw=post.get("created_at"),
        raw={"id": post.get("id"), "author_id": post.get("author_id"), "lang": post.get("lang")},
        social=True,
    )
