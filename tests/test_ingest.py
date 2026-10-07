import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.config import Source
from app.ingest import Ingestor, backoff_delay
from app.models import Article, SourceStatus
from tests.conftest import fixture_bytes

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

TECH = Source(name="Tech", url="https://www.example-tech.com/feed")
SCIENCE = Source(name="Science", type="atom", url="https://science.example.org/feed.atom")
GAZETTE = Source(name="Gazette", url="http://gazette.example.net/rss")
WIRE = Source(name="Wire", url="https://www.markets-wire.example.com/rss")
BROKEN = Source(name="Broken", url="https://broken.example/rss")

ROUTES = {
    str(TECH.url): "tech_rss.xml",
    str(SCIENCE.url): "science_atom.xml",
    str(GAZETTE.url): "malformed_dates_rss.xml",
    str(WIRE.url): "tracking_params_rss.xml",
}


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def fixture_handler(request: httpx.Request) -> httpx.Response:
    name = ROUTES.get(str(request.url))
    if name is None:
        return httpx.Response(404)
    return httpx.Response(200, content=fixture_bytes(name), headers={"ETag": f'"{name}"'})


def make_ingestor(
    session_factory, handler: Callable = fixture_handler, clock: Clock | None = None
) -> Ingestor:
    return Ingestor(session_factory, transport=httpx.MockTransport(handler), clock=clock or Clock())


def run(ingestor: Ingestor, *sources: Source, force: bool = False):
    return {r.source_id: r for r in asyncio.run(ingestor.run(sources, force=force))}


def articles(session_factory) -> list[Article]:
    with session_factory() as s:
        return list(s.scalars(select(Article).order_by(Article.canonical_url)))


def count(session_factory) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(Article))


def status(session_factory, source_id: str) -> SourceStatus:
    with session_factory() as s:
        return s.get(SourceStatus, source_id)


def snapshot(session_factory) -> list[dict]:
    return [
        {col.key: getattr(a, col.key) for col in Article.__table__.columns}
        for a in articles(session_factory)
    ]


def test_ingests_and_normalizes_all_fixtures(session_factory) -> None:
    results = run(make_ingestor(session_factory), TECH, SCIENCE, GAZETTE, WIRE)

    assert {k: (r.status, r.new_articles) for k, r in results.items()} == {
        "tech": ("ok", 3),
        "science": ("ok", 2),
        "gazette": ("ok", 3),
        "wire": ("ok", 2),
    }
    stored = {a.canonical_url: a for a in articles(session_factory)}
    jit = stored["https://www.example-tech.com/2026/10/python-315-jit"]
    assert jit.title == "Python 3.15 & the new JIT: what changes for you"
    assert jit.summary_raw == "The new JIT lands in 3.15. Benchmarks show a 10\u201330% speedup."
    assert jit.published_at == datetime(2026, 10, 6, 17, 0, tzinfo=UTC)
    assert jit.fetched_at == T0
    assert jit.image_url == "https://cdn.example-tech.com/img/python-jit.jpg"
    assert jit.raw_json["id"] == "example-tech-90210"
    assert jit.source_id == "tech"

    # Malformed and missing dates fall back to fetched_at; valid offsets become UTC.
    assert stored["http://gazette.example.net/news/bike-lanes"].published_at == T0
    assert stored["http://gazette.example.net/news/library-hours"].published_at == T0
    market = stored["http://gazette.example.net/news/farmers-market"]
    assert market.published_at == datetime(2026, 10, 4, 14, 30, tzinfo=UTC)

    webb = stored["https://science.example.org/articles/webb-water-vapour"]
    assert webb.title == "Webb spots water vapour on a temperate exoplanet"
    assert webb.published_at == datetime(2026, 10, 6, 18, 30, tzinfo=UTC)


def test_ingesting_same_fixture_twice_creates_no_duplicates(session_factory) -> None:
    clock = Clock()
    ingestor = make_ingestor(session_factory, clock=clock)
    run(ingestor, TECH, SCIENCE, GAZETTE, WIRE)
    before = snapshot(session_factory)
    assert len(before) == 10

    clock.now = T0 + timedelta(hours=1)
    results = run(ingestor, TECH, SCIENCE, GAZETTE, WIRE)

    assert all(r.status == "ok" and r.new_articles == 0 for r in results.values())
    assert count(session_factory) == 10
    assert snapshot(session_factory) == before  # incl. fetched_at, which must not change


def test_urls_differing_only_by_tracking_params_produce_one_article(session_factory) -> None:
    run(make_ingestor(session_factory), WIRE)

    fed = [a for a in articles(session_factory) if a.title.startswith("Fed holds")]
    assert len(fed) == 1
    assert fed[0].canonical_url == "https://www.markets-wire.example.com/news/fed-holds-rates"


def test_tracking_param_duplicates_across_sources_and_runs(session_factory) -> None:
    feed = b"""<rss version="2.0"><channel><title>t</title>
      <item><title>Same story</title><link>%s</link></item></channel></rss>"""
    urls = iter(
        [
            b"https://news.example/story?utm_source=a&amp;utm_medium=rss",
            b"https://news.example/story/?utm_source=b&amp;utm_campaign=c#x",
        ]
    )
    ingestor = make_ingestor(
        session_factory, lambda r: httpx.Response(200, content=feed % next(urls))
    )
    run(ingestor, TECH)
    run(ingestor, WIRE)
    assert [a.canonical_url for a in articles(session_factory)] == ["https://news.example/story"]


def test_same_title_and_domain_with_different_url_is_deduplicated(session_factory) -> None:
    run(make_ingestor(session_factory), WIRE)

    oil = [a for a in articles(session_factory) if a.title.lower().startswith("oil")]
    assert len(oil) == 1
    assert oil[0].canonical_url == "https://www.markets-wire.example.com/news/oil-climbs?id=77"


def test_304_keeps_data_and_updates_source_status(session_factory) -> None:
    clock = Clock()
    run(make_ingestor(session_factory, clock=clock), TECH)
    before = snapshot(session_factory)
    seen: list[httpx.Request] = []

    def not_modified(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(304)

    clock.now = T0 + timedelta(minutes=30)
    result = run(make_ingestor(session_factory, not_modified, clock), TECH)["tech"]

    assert result.status == "not_modified"
    assert seen[0].headers["If-None-Match"] == '"tech_rss.xml"'
    assert snapshot(session_factory) == before
    st = status(session_factory, "tech")
    assert st.last_success_at == clock.now
    assert st.last_attempt_at == clock.now
    assert st.consecutive_failures == 0
    assert st.etag == '"tech_rss.xml"'


def test_error_keeps_data_and_updates_source_status_with_backoff(session_factory) -> None:
    clock = Clock()
    run(make_ingestor(session_factory, clock=clock), TECH)
    before = snapshot(session_factory)
    failing = make_ingestor(session_factory, lambda r: httpx.Response(503), clock)

    clock.now = T0 + timedelta(minutes=30)
    result = run(failing, TECH)["tech"]

    assert result.status == "error"
    assert "HTTP 503" in result.error
    assert snapshot(session_factory) == before
    st = status(session_factory, "tech")
    assert st.consecutive_failures == 1
    assert "HTTP 503" in st.last_error
    assert st.last_success_at == T0
    assert st.etag == '"tech_rss.xml"'
    assert st.next_attempt_at == clock.now + timedelta(minutes=30)

    # Second failure doubles the delay; scheduled runs inside the window are skipped.
    clock.now += timedelta(minutes=30)
    run(failing, TECH)
    st = status(session_factory, "tech")
    assert st.consecutive_failures == 2
    assert st.next_attempt_at == clock.now + timedelta(minutes=60)

    clock.now += timedelta(minutes=30)
    skipped = run(failing, TECH)["tech"]
    assert skipped.status == "skipped"
    assert status(session_factory, "tech").consecutive_failures == 2

    # A forced (manual) run ignores the backoff, and success resets the status.
    recovered = run(make_ingestor(session_factory, clock=clock), TECH, force=True)["tech"]
    assert recovered.status == "ok"
    st = status(session_factory, "tech")
    assert (st.consecutive_failures, st.last_error, st.next_attempt_at) == (0, None, None)
    assert count(session_factory) == 3


def test_backoff_delay_is_exponential_and_capped() -> None:
    assert [backoff_delay(n, 30).total_seconds() / 60 for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert backoff_delay(50, 30) == timedelta(hours=24)


def test_one_failing_source_does_not_block_others(session_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "broken.example":
            raise httpx.ConnectTimeout("timed out", request=request)
        if request.url.host == "gazette.example.net":
            return httpx.Response(200, text="<html><body>Oops, not a feed</body></html>")
        return fixture_handler(request)

    results = run(make_ingestor(session_factory, handler), BROKEN, TECH, GAZETTE, SCIENCE)

    assert results["broken"].status == "error"
    assert "ConnectTimeout" in results["broken"].error
    assert results["gazette"].status == "error"
    assert results["tech"].new_articles == 3
    assert results["science"].new_articles == 2
    assert count(session_factory) == 5
    assert status(session_factory, "broken").consecutive_failures == 1
    assert status(session_factory, "tech").consecutive_failures == 0


def test_unsupported_source_type_is_isolated(session_factory) -> None:
    web = Source(name="Web", type="web", url="https://web.example/")
    results = run(make_ingestor(session_factory), web, TECH)
    assert results["web"].status == "error"
    assert "no adapter registered" in results["web"].error
    assert results["tech"].status == "ok"


def test_timeout_and_user_agent_are_configured(session_factory) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return fixture_handler(request)

    run(make_ingestor(session_factory, handler), TECH)
    assert seen[0].headers["User-Agent"].startswith("discover-feed/")
    assert seen[0].extensions["timeout"] == {
        "connect": 10.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }
