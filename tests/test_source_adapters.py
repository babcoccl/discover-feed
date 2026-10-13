"""Guardian / NewsData / X adapters against local fake APIs (httpx.MockTransport)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import Source
from app.db import init_db, make_engine, make_session_factory
from app.ingest import Ingestor
from app.models import Article, SourceRecord, SourceStatus, SourceUsage
from app.sources import FetchError, FetchState, Quota, QuotaExhausted, RateLimited, create_adapter

KEY = "sekrit-key-123"
NOW = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)


def guardian_page(page: int, pages: int = 2, body: str = "") -> dict:
    return {
        "response": {
            "status": "ok",
            "pages": pages,
            "currentPage": page,
            "results": [
                {
                    "webTitle": f"Guardian story {page}-{i}",
                    "webUrl": f"https://www.theguardian.com/world/2026/oct/12/story-{page}-{i}",
                    "webPublicationDate": "2026-10-12T07:00:00Z",
                    "fields": {
                        "trailText": "Trail text",
                        "thumbnail": "https://media.guim.co.uk/t.jpg",
                        "bodyText": body,
                    },
                }
                for i in range(2)
            ],
        }
    }


def newsdata_page(token: str | None, n: int = 2) -> dict:
    return {
        "status": "success",
        "results": [
            {
                "title": f"NewsData item {token}-{i}",
                "link": f"https://example.com/nd/{token}-{i}",
                "description": "Description",
                "content": "ONLY AVAILABLE IN PAID PLANS",
                "pubDate": "2026-10-12 07:30:00",
                "image_url": None,
                "creator": ["Reporter"],
            }
            for i in range(n)
        ],
        "nextPage": "p2" if token is None else None,
    }


def x_body(n: int) -> dict:
    return {
        "data": [
            {
                "id": str(1000 + i),
                "text": f"Post {i} about rates",
                "author_id": "7",
                "created_at": "2026-10-12T07:00:00Z",
            }
            for i in range(n)
        ],
        "includes": {"users": [{"id": "7", "username": "fedwatch", "name": "Fed Watch"}]},
    }


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for name in ("GUARDIAN_API_KEY", "NEWSDATA_API_KEY", "X_BEARER_TOKEN"):
        monkeypatch.setenv(name, KEY)


def guardian(**kw) -> Source:
    params = kw.pop("params", {"q": "rates", "max_pages": 3})
    return Source(name="Guardian", type="guardian", api_key="GUARDIAN_API_KEY", params=params, **kw)


def newsdata(**kw) -> Source:
    return Source(
        name="NewsData",
        type="newsdata",
        api_key="NEWSDATA_API_KEY",
        params={"q": "ecb", "country": "de", "max_pages": 2},
        **kw,
    )


def xsource(**kw) -> Source:
    kw.setdefault("monthly_budget_usd", 1.0)
    return Source(
        name="X",
        type="x",
        api_key="X_BEARER_TOKEN",
        params={"users": ["fedwatch"]},
        max_posts_per_run=10,
        **kw,
    )


def run(source: Source, handler, state: FetchState | None = None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await create_adapter(source.type, client).fetch(source, state)

    return asyncio.run(go())


def test_guardian_success_pagination_and_key_in_header_only() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=guardian_page(page))

    items = run(guardian(), handler)
    assert len(items) == 4 and [int(r.url.params["page"]) for r in seen] == [1, 2]
    assert all(KEY not in str(r.url) and r.headers["api-key"] == KEY for r in seen)
    assert seen[0].url.params["q"] == "rates" and seen[0].url.path == "/search"
    first = items[0]
    assert first.title == "Guardian story 1-0" and first.summary == "Trail text"
    assert first.image_url.endswith("t.jpg") and first.published_at == datetime(
        2026, 10, 12, 7, tzinfo=UTC
    )


def test_newsdata_success_pagination_and_paid_content_ignored() -> None:
    tokens = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.url.params.get("page"))
        assert request.headers["X-ACCESS-KEY"] == KEY and KEY not in str(request.url)
        return httpx.Response(200, json=newsdata_page(request.url.params.get("page")))

    items = run(newsdata(), handler)
    assert tokens == [None, "p2"] and len(items) == 4
    assert items[0].author == "Reporter" and items[0].text is None
    assert items[0].published_at == datetime(2026, 10, 12, 7, 30, tzinfo=UTC)


def test_x_posts_are_social_and_spend_is_recorded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert request.url.params["query"] == "(from:fedwatch) -is:retweet"
        assert request.url.params["max_results"] == "10"
        return httpx.Response(200, json=x_body(10))

    quota = Quota(monthly_budget_usd=1.0)
    items = run(xsource(), handler, FetchState(quota=quota))
    assert len(items) == 10 and all(i.social for i in items)
    assert items[0].url == "https://x.com/fedwatch/status/1000"
    assert quota.new_reads == 10 and quota.spent_usd == pytest.approx(0.05)


ERRORS = {
    "429": (
        httpx.Response(429, headers={"Retry-After": "120"}, json={"message": "slow down"}),
        RateLimited,
        "rate limit",
    ),
    "bad key": (
        httpx.Response(401, json={"message": "Invalid authentication credentials"}),
        FetchError,
        "rejected the API key",
    ),
    "malformed": (httpx.Response(200, text="<html>not json"), FetchError, "malformed JSON"),
}


@pytest.mark.parametrize("factory", [guardian, newsdata, xsource])
@pytest.mark.parametrize("case", list(ERRORS))
def test_adapter_errors_are_clear_and_never_leak_the_key(factory, case) -> None:
    response, exc_type, text = ERRORS[case]

    def handler(request):
        return response

    with pytest.raises(exc_type) as info:
        run(factory(), handler, FetchState(quota=Quota(monthly_budget_usd=1.0)))
    assert text in str(info.value) and KEY not in str(info.value)
    if case == "429":
        assert info.value.retry_after == 120


@pytest.mark.parametrize(
    ("factory", "body"),
    [
        (
            guardian,
            {"response": {"status": "error", "message": "API rate limit exceeded: daily limit"}},
        ),
        (
            newsdata,
            {"status": "error", "results": {"code": "ApiLimitExceeded", "message": "quota used"}},
        ),
        (xsource, {"title": "UsageCapExceeded", "detail": "Usage cap exceeded"}),
    ],
)
def test_provider_quota_exhausted(factory, body) -> None:
    with pytest.raises(FetchError, match="quota exhausted"):
        run(
            factory(),
            lambda r: httpx.Response(403 if factory is not newsdata else 429 - 1, json=body),
        )


def test_missing_key_names_variable_without_request(monkeypatch) -> None:
    monkeypatch.delenv("GUARDIAN_API_KEY")
    with pytest.raises(FetchError, match="GUARDIAN_API_KEY is not set"):
        run(guardian(), lambda r: pytest.fail("no request expected"))


def test_x_never_fetches_without_cap() -> None:
    with pytest.raises(ValueError, match="monthly_budget_usd"):
        Source(name="X", type="x", params={"query": "fed"})
    source = Source(name="X", type="x", params={"query": "fed"}, enabled=False)
    with pytest.raises(QuotaExhausted):
        run(source, lambda r: pytest.fail("no request expected"))


# --- through the Ingestor: budgets persisted, Guardian body text, X social -----------------------


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    init_db(engine)
    return make_session_factory(engine)


def add(factory, source: Source) -> None:
    with factory() as session:
        session.add(SourceRecord.from_config(source))
        session.commit()


def ingestor(factory, handler, clock=lambda: NOW, **kw) -> Ingestor:
    return Ingestor(factory, transport=httpx.MockTransport(handler), clock=clock, **kw)


def test_guardian_body_text_is_stored_and_skips_extraction(factory) -> None:
    body = " ".join(["word"] * 200)
    source = guardian(params={"q": "x"})
    add(factory, source)
    handler = lambda r: httpx.Response(200, json=guardian_page(1, pages=1, body=body))  # noqa: E731
    [result] = asyncio.run(ingestor(factory, handler).run([source]))
    assert result.status == "ok" and result.new_articles == 2
    with factory() as session:
        article = session.query(Article).first()
        assert article.text_status == "ok" and article.word_count == 200 and article.text == body


def test_x_posts_skip_extraction(factory) -> None:
    source = xsource()
    add(factory, source)
    asyncio.run(ingestor(factory, lambda r: httpx.Response(200, json=x_body(10))).run([source]))
    with factory() as session:
        assert {a.text_status for a in session.query(Article)} == {"skipped"}


def test_daily_request_budget_survives_restart_and_resets_next_day(factory) -> None:
    source = newsdata(daily_request_budget=3)
    add(factory, source)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=newsdata_page(request.url.params.get("page")))

    first = asyncio.run(ingestor(factory, handler).run([source], force=True))
    assert first[0].status == "ok" and len(calls) == 2
    # "Restart": a new Ingestor reads the persisted counters.
    second = asyncio.run(ingestor(factory, handler).run([source], force=True))
    assert len(calls) == 3 and second[0].status == "ok"  # 1 request left: one page only
    third = asyncio.run(ingestor(factory, handler).run([source], force=True))
    assert len(calls) == 3 and third[0].status == "skipped" and "budget" in third[0].error
    with factory() as session:
        assert session.get(SourceUsage, (source.id, "day:2026-10-12")).requests == 3
        assert session.get(SourceStatus, source.id).consecutive_failures == 0
    tomorrow = asyncio.run(
        ingestor(factory, handler, clock=lambda: NOW + timedelta(days=1)).run([source], force=True)
    )
    assert tomorrow[0].status == "ok" and len(calls) == 5


def test_x_hard_cap_survives_restart(factory) -> None:
    source = xsource(monthly_budget_usd=0.1)  # 20 reads at $0.005
    add(factory, source)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=x_body(10))

    for _ in range(2):
        assert asyncio.run(ingestor(factory, handler).run([source], force=True))[0].status == "ok"
    capped = asyncio.run(ingestor(factory, handler).run([source], force=True))
    assert capped[0].status == "skipped" and "budget reached" in capped[0].error and len(calls) == 2
    with factory() as session:
        usage = session.get(SourceUsage, (source.id, "month:2026-10"))
        assert usage.reads == 20 and usage.spend_usd == pytest.approx(0.1)


def test_rate_limit_retry_after_sets_next_attempt(factory) -> None:
    source = guardian()
    add(factory, source)
    handler = lambda r: httpx.Response(429, headers={"Retry-After": "3600"})  # noqa: E731
    [result] = asyncio.run(ingestor(factory, handler).run([source]))
    assert result.status == "error"
    with factory() as session:
        status = session.get(SourceStatus, source.id)
        assert status.next_attempt_at >= NOW + timedelta(seconds=3600)


def test_auto_disable_after_consecutive_failures(factory) -> None:
    source = Source(name="Broken", url="https://broken.example/feed.xml")
    add(factory, source)
    ing = ingestor(factory, lambda r: httpx.Response(500), auto_disable_failures=3)
    results = [asyncio.run(ing.run([source], force=True))[0].status for _ in range(4)]
    assert results == ["error", "error", "disabled", "skipped"]
    with factory() as session:
        record = session.get(SourceRecord, source.id)
        assert not record.enabled and record.auto_disabled_at is not None
        assert "3 consecutive failures" in record.disabled_reason
        assert session.query(SourceRecord).count() == 1  # never deleted


def test_secrets_never_reach_db_or_logs(factory, caplog) -> None:
    source = guardian()
    add(factory, source)
    handler = lambda r: httpx.Response(401, json={"message": f"bad key {KEY}"})  # noqa: E731
    asyncio.run(ingestor(factory, handler).run([source]))
    with factory() as session:
        dumped = json.dumps(
            [
                {c.name: str(getattr(row, c.name)) for c in row.__table__.columns}
                for model in (SourceRecord, SourceStatus)
                for row in session.query(model)
            ]
        )
    assert KEY not in dumped and "GUARDIAN_API_KEY" in dumped
    assert KEY not in caplog.text
