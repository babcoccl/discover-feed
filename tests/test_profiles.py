from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import load_config, parse_config
from app.demo import build_demo
from app.main import create_app
from app.models import Article, ProfileRecord, SourceRecord, Topic
from app.profiles import active_sources, decode_cursor, encode_cursor, feed_page, seed_from_config
from app.settings import Settings
from tests.conftest import EXAMPLE_CONFIG

LLM = "llm: {summarizer: {base_url: 'http://localhost:8080/v1', model: m}}"
SHARED_CONFIG = f"""
profiles:
  - id: a
    name: Profile A
    description: First
    {LLM}
    sources:
      - {{name: Shared, url: 'https://shared.example/rss'}}
      - {{name: Only A, url: 'https://a.example/rss'}}
    topics:
      - {{name: Rates, include: [rates], sources: [Shared]}}
      - {{name: Everything}}
  - id: b
    name: Profile B
    {LLM}
    sources:
      - {{name: Shared, url: 'https://shared.example/rss'}}
"""


def _article(i: int, source_id: str, title: str, *, hours_ago: float, summary: str = "") -> Article:
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    return Article(
        source_id=source_id,
        canonical_url=f"https://{source_id}.example/{i}",
        title=title,
        summary_raw=summary,
        published_at=now - timedelta(hours=hours_ago),
        fetched_at=now - timedelta(hours=i),
        content_hash=f"h{i}",
    )


# --- seeding -------------------------------------------------------------------------------


def test_seed_creates_profiles_shared_sources_and_topics(session_factory) -> None:
    with session_factory() as session:
        assert seed_from_config(session, parse_config(SHARED_CONFIG))
        a, b = session.scalars(select(ProfileRecord).order_by(ProfileRecord.id)).all()
        assert (a.slug, a.name, a.description) == ("a", "Profile A", "First")
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 2
        assert {s.id for s in a.sources} == {"shared", "only-a"}
        assert [s.id for s in b.sources] == ["shared"]
        assert a.sources[[s.id for s in a.sources].index("shared")] is b.sources[0]
        assert [(t.name, t.position, t.source_ids) for t in a.topics] == [
            ("Rates", 0, ["shared"]),
            ("Everything", 1, []),
        ]
        assert b.topics == []
        assert {s.id for s in active_sources(session)} == {"shared", "only-a"}


def test_seed_runs_only_on_first_start_and_db_wins_afterwards(session_factory) -> None:
    config = parse_config(SHARED_CONFIG)
    with session_factory() as session:
        assert seed_from_config(session, config)
        session.get(Topic, 1).name = "Renamed in DB"
        session.commit()
        assert not seed_from_config(session, config)
        assert session.scalar(select(func.count()).select_from(Topic)) == 2
        assert session.get(Topic, 1).name == "Renamed in DB"


def test_example_config_seeds_requested_topics(session_factory) -> None:
    with session_factory() as session:
        seed_from_config(session, load_config(EXAMPLE_CONFIG))
        topics = {
            p.slug: [t.name for t in p.topics] for p in session.scalars(select(ProfileRecord))
        }
    assert topics["personal-reader"] == ["AI", "Home", "Travel"]
    assert topics["market-monitor"][:3] == ["Markets", "SEC filings", "Earnings"]


def test_topic_config_rejects_duplicates_and_unknown_sources() -> None:
    from pydantic import ValidationError

    base = (
        f"profiles: [{{id: a, name: A, {LLM}, sources: [{{name: S, url: 'https://s.example/'}}], "
    )
    with pytest.raises(ValidationError, match="duplicate topic"):
        parse_config(base + "topics: [{name: X}, {name: x}]}]")
    with pytest.raises(ValidationError, match="unknown sources"):
        parse_config(base + "topics: [{name: X, sources: [Nope]}]}]")


# --- feed + cursor -------------------------------------------------------------------------


@pytest.fixture
def seeded(session_factory):
    with session_factory() as session:
        seed_from_config(session, parse_config(SHARED_CONFIG))
        session.add_all(
            [
                _article(1, "shared", "Fed holds rates steady", hours_ago=1),
                _article(2, "only-a", "Rates outlook from A", hours_ago=2),
                _article(3, "shared", "Gardening tips", hours_ago=3),
                _article(4, "shared", "Pirates win the cup", hours_ago=4),  # not whole word
                _article(5, "shared", "Bank news", hours_ago=5, summary="RATES fall again"),
                _article(6, "elsewhere", "Rates elsewhere", hours_ago=0),  # not in any profile
            ]
        )
        session.commit()
    return session_factory


def test_feed_is_scoped_to_profile_sources_and_filtered_by_topic(seeded) -> None:
    with seeded() as session:
        a = session.scalar(select(ProfileRecord).where(ProfileRecord.slug == "a"))
        b = session.scalar(select(ProfileRecord).where(ProfileRecord.slug == "b"))
        titles = lambda page: [x.title for x in page.items]  # noqa: E731
        assert titles(feed_page(session, a)) == [
            "Fed holds rates steady",
            "Rates outlook from A",
            "Gardening tips",
            "Pirates win the cup",
            "Bank news",
        ]
        assert len(feed_page(session, b).items) == 4
        rates, everything = a.topics
        # Rates is restricted to the Shared source, so the "only-a" article is excluded.
        assert titles(feed_page(session, a, rates)) == ["Fed holds rates steady", "Bank news"]
        assert len(feed_page(session, a, everything).items) == 5


def test_cursor_pagination_walks_all_matches_without_gaps(seeded) -> None:
    with seeded() as session:
        a = session.scalar(select(ProfileRecord).where(ProfileRecord.slug == "a"))
        seen, cursor = [], None
        for _ in range(10):
            page = feed_page(session, a, limit=2, cursor=cursor)
            seen += [x.id for x in page.items]
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == [1, 2, 3, 4, 5]

        # time_field=fetched orders by fetched_at (article 1 fetched most recently here).
        page = feed_page(session, a, limit=3, time_field="fetched")
        assert [x.id for x in page.items] == [1, 2, 3]
        assert [
            x.id for x in feed_page(session, a, cursor=page.next_cursor, time_field="fetched").items
        ] == [4, 5]


def test_cursor_pagination_with_topic_filter_and_ties(session_factory) -> None:
    with session_factory() as session:
        seed_from_config(session, parse_config(SHARED_CONFIG))
        # 250 matching articles with identical timestamps, interleaved with non-matches.
        session.add_all(
            _article(i, "shared", "rates" if i % 2 else "other", hours_ago=1) for i in range(1, 501)
        )
        session.commit()
        a = session.scalar(select(ProfileRecord).where(ProfileRecord.slug == "a"))
        seen, cursor = [], None
        while True:
            page = feed_page(session, a, a.topics[0], limit=40, cursor=cursor)
            seen += [x.id for x in page.items]
            if not (cursor := page.next_cursor):
                break
        assert len(seen) == 250 == len(set(seen))
        assert seen == sorted(seen, reverse=True)


def test_cursor_roundtrip_and_invalid() -> None:
    from app.profiles import InvalidCursor

    at = datetime(2026, 10, 7, 12, 30, tzinfo=UTC)
    assert decode_cursor(encode_cursor(at, 42)) == (at, 42)
    for bad in ("not-a-cursor", "!!!", encode_cursor(at, 1)[:-3]):
        with pytest.raises(InvalidCursor):
            decode_cursor(bad)


# --- API -----------------------------------------------------------------------------------


@pytest.fixture
def demo_client(tmp_path: Path):
    settings, _ = build_demo(tmp_path / "demo.db")
    with TestClient(create_app(settings)) as c:
        yield c


def test_profiles_api(client: TestClient) -> None:
    profiles = client.get("/api/profiles").json()
    assert [p["slug"] for p in profiles] == ["personal-reader", "market-monitor"]
    detail = client.get("/api/profiles/personal-reader").json()
    assert [t["name"] for t in detail["topics"]] == ["AI", "Home", "Travel"]
    assert {s["id"] for s in detail["sources"]} == {
        "hacker-news",
        "ars-technica",
        "quanta-magazine",
        "the-verge",
        "techcrunch",
    }
    assert detail["topics"][0]["include_keywords"][0] == "AI"
    assert client.get("/api/profiles/nope").status_code == 404


def test_feed_api_filters_paginates_and_names_sources(demo_client: TestClient) -> None:
    base = "/api/profiles/personal-reader"
    all_items = demo_client.get(f"{base}/feed", params={"group": "articles", "limit": 100}).json()[
        "items"
    ]
    assert len(all_items) >= 15
    assert {i["source_name"] for i in all_items} == {
        "Hacker News",
        "Ars Technica",
        "Quanta Magazine",
        "The Verge",
        "TechCrunch",
    }
    ai = next(t for t in demo_client.get(base).json()["topics"] if t["name"] == "AI")
    ai_feed = demo_client.get(
        f"{base}/feed", params={"group": "articles", "topic": ai["id"], "limit": 100}
    ).json()
    assert ai_feed["topic"]["name"] == "AI"
    assert 0 < len(ai_feed["items"]) < len(all_items)

    first = demo_client.get(f"{base}/feed", params={"group": "articles", "limit": 5}).json()
    second = demo_client.get(
        f"{base}/feed", params={"group": "articles", "limit": 5, "cursor": first["next_cursor"]}
    ).json()
    assert [i["id"] for i in first["items"] + second["items"]] == [i["id"] for i in all_items[:10]]
    published = [i["published_at"] for i in all_items]
    assert published == sorted(published, reverse=True)
    fetched = demo_client.get(
        f"{base}/feed", params={"group": "articles", "time_field": "fetched", "limit": 3}
    )
    assert fetched.status_code == 200 and len(fetched.json()["items"]) == 3

    assert (
        demo_client.get(
            f"{base}/feed", params={"group": "articles", "cursor": "garbage"}
        ).status_code
        == 422
    )
    assert (
        demo_client.get(f"{base}/feed", params={"group": "articles", "topic": 9999}).status_code
        == 404
    )
    assert (
        demo_client.get(
            f"{base}/feed", params={"group": "articles", "time_field": "nope"}
        ).status_code
        == 422
    )


def test_topic_crud_api_changes_feed_immediately(demo_client: TestClient) -> None:
    base = "/api/profiles/personal-reader"
    resp = demo_client.post(
        f"{base}/topics",
        json={"name": "Space", "include_keywords": ["NASA", "astronauts"], "exclude_keywords": []},
    )
    assert resp.status_code == 201
    topic = resp.json()
    assert topic["position"] == 3 and topic["enabled"] is True
    items = demo_client.get(
        f"{base}/feed", params={"group": "articles", "topic": topic["id"]}
    ).json()["items"]
    assert [i["title"] for i in items] == ["NASA's Artemis III crew begins final training"]

    resp = demo_client.put(
        f"{base}/topics/{topic['id']}",
        json={"name": "Science", "include_keywords": ["physics"], "position": 0},
    )
    assert resp.status_code == 200
    names = [t["name"] for t in demo_client.get(base).json()["topics"]]
    assert names == ["Science", "AI", "Home", "Travel"]
    items = demo_client.get(
        f"{base}/feed", params={"group": "articles", "topic": topic["id"]}
    ).json()["items"]
    assert items and all("physics" in (i["title"] + i["summary_raw"]).lower() for i in items)

    assert demo_client.post(f"{base}/topics", json={"name": "ai"}).status_code == 409
    assert demo_client.post(f"{base}/topics", json={"name": ""}).status_code == 422
    assert (
        demo_client.post(
            f"{base}/topics", json={"name": "X", "source_ids": ["sec-press-releases"]}
        ).status_code
        == 422
    )
    other = demo_client.get("/api/profiles/market-monitor").json()["topics"][0]["id"]
    assert demo_client.put(f"{base}/topics/{other}", json={"name": "Z"}).status_code == 404

    assert demo_client.delete(f"{base}/topics/{topic['id']}").status_code == 204
    assert demo_client.delete(f"{base}/topics/{topic['id']}").status_code == 404
    positions = [(t["name"], t["position"]) for t in demo_client.get(base).json()["topics"]]
    assert positions == [("AI", 0), ("Home", 1), ("Travel", 2)]


def test_new_routes_are_in_openapi(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths["/api/profiles/{slug}/topics"]) == {"post"}
    assert set(paths["/api/profiles/{slug}/topics/{topic_id}"]) == {"put", "delete"}
    assert {"/api/profiles", "/api/profiles/{slug}", "/api/profiles/{slug}/feed"} <= set(paths)
    assert not any(p.startswith("/p/") for p in paths)


# --- UI ------------------------------------------------------------------------------------


def test_home_redirects_to_first_profile(client: TestClient) -> None:
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"] == "/p/personal-reader"


def test_profile_page_renders_tabs_cards_and_switcher(demo_client: TestClient) -> None:
    html = demo_client.get("/p/personal-reader").text
    for text in ('aria-current="page"', ">All</a>", ">AI</a>", ">Home</a>", ">Travel</a>"):
        assert text in html
    assert 'id="profile-switcher"' in html and "Market Monitor" in html
    assert html.count("<article") == 24
    assert 'target="_blank" rel="noopener noreferrer"' in html
    assert "https://picsum.photos/seed/ars-technica-1/800/450" in html
    assert "Read on Hacker News" in html
    assert demo_client.get("/p/nope").status_code == 404


def test_htmx_partials_swap_grid_and_load_more(demo_client: TestClient) -> None:
    topics = demo_client.get("/api/profiles/market-monitor").json()["topics"]
    sec = next(t for t in topics if t["name"] == "SEC filings")
    partial = demo_client.get(
        f"/p/market-monitor/feed?topic={sec['id']}", headers={"HX-Request": "true"}
    )
    assert "<html" not in partial.text and 'id="grid"' in partial.text
    assert 0 < partial.text.count("<article") < 21

    from app import web

    web.PAGE_SIZE, old = 8, web.PAGE_SIZE
    try:
        first = demo_client.get("/p/market-monitor").text
        assert first.count("<article") == 8 and "Load more" in first
        cursor = first.split("cursor=")[1].split('"')[0]
        more = demo_client.get(f"/p/market-monitor/feed?cursor={cursor}").text
        assert "<nav" not in more and more.count("<article") == 8
    finally:
        web.PAGE_SIZE = old


def test_settings_page_lists_topics_and_source_status(demo_client: TestClient) -> None:
    html = demo_client.get("/p/market-monitor/settings").text
    assert 'value="SEC filings"' in html and "Add a topic" in html
    assert "Federal Reserve press releases" in html and "Failures" in html
    assert "never" not in html  # demo ingested every source


def test_settings_forms_add_edit_move_delete_topic(demo_client: TestClient) -> None:
    base = "/p/personal-reader/settings/topics"
    hx = {"HX-Request": "true"}
    resp = demo_client.post(
        base, data={"name": "Space", "include": "NASA, astronauts", "enabled": "on"}, headers=hx
    )
    assert resp.status_code == 200 and "Added topic" in resp.text and 'value="Space"' in resp.text
    space = next(
        t
        for t in demo_client.get("/api/profiles/personal-reader").json()["topics"]
        if t["name"] == "Space"
    )
    assert space["include_keywords"] == ["NASA", "astronauts"]
    assert ">Space</a>" in demo_client.get("/p/personal-reader").text
    feed = demo_client.get(f"/p/personal-reader?topic={space['id']}").text
    assert feed.count("<article") == 1

    demo_client.post(f"{base}/{space['id']}/move?direction=up", headers=hx)
    names = [t["name"] for t in demo_client.get("/api/profiles/personal-reader").json()["topics"]]
    assert names == ["AI", "Home", "Space", "Travel"]

    # Unchecked "enabled" disables the topic: it disappears from the tab bar.
    resp = demo_client.post(
        f"{base}/{space['id']}",
        data={"name": "Space", "include": "NASA", "exclude": "Artemis"},
        headers=hx,
    )
    assert "Saved" in resp.text
    assert ">Space</a>" not in demo_client.get("/p/personal-reader").text

    resp = demo_client.post(base, data={"name": "ai"}, headers=hx)
    assert "already exists" in resp.text
    resp = demo_client.post(base, data={"name": "ai"})  # non-HTMX form post re-renders with error
    assert resp.status_code == 422 and "already exists" in resp.text

    resp = demo_client.post(f"{base}/{space['id']}/delete", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/p/personal-reader/settings"
    assert "Space" not in [
        t["name"] for t in demo_client.get("/api/profiles/personal-reader").json()["topics"]
    ]


def test_reltime_and_snippet_filters() -> None:
    from app.web import reltime, snippet

    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    assert reltime(now - timedelta(seconds=5), now) == "just now"
    assert reltime(now - timedelta(minutes=5), now) == "5m ago"
    assert reltime(now - timedelta(hours=3), now) == "3h ago"
    assert reltime(now - timedelta(days=2), now) == "2d ago"
    assert reltime(datetime(2026, 1, 3, tzinfo=UTC), now) == "Jan 3"
    assert reltime(datetime(2025, 1, 3, tzinfo=UTC), now) == "Jan 3, 2025"
    assert snippet("a  b\n c") == "a b c"
    assert snippet("word " * 100, 20).endswith("…")


# --- demo ----------------------------------------------------------------------------------


def test_demo_builds_isolated_db_with_enough_articles_and_visible_topic_spread(
    tmp_path: Path,
) -> None:
    settings, results = build_demo(tmp_path / "demo.db")
    assert settings.database_url == f"sqlite:///{(tmp_path / 'demo.db').as_posix()}"
    assert not settings.scheduler_enabled
    assert len(results) == 10 and all(r.status == "ok" and r.new_articles >= 3 for r in results)
    from app.db import make_engine, make_session_factory

    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            for profile in session.scalars(select(ProfileRecord)):
                total = len(feed_page(session, profile, limit=100).items)
                assert total >= 15, profile.slug
                for topic in profile.topics:
                    count = len(feed_page(session, profile, topic, limit=100).items)
                    assert 0 < count < total, (profile.slug, topic.name)
    finally:
        engine.dispose()

    # Rebuilding starts from scratch (throwaway DB).
    _, again = build_demo(tmp_path / "demo.db")
    assert sum(r.new_articles for r in again) == 66


def test_settings_reject_poll_interval_alias_and_default_time_field(client: TestClient) -> None:
    """Phase 1 cleanups stay in place."""
    with pytest.raises(ValueError):
        parse_config(
            f"profiles: [{{id: a, name: A, {LLM}, sources: "
            "[{name: S, url: 'https://s.example/', poll_interval_minutes: 5}]}]"
        )
    params = client.app.openapi()["paths"]["/api/articles"]["get"]["parameters"]
    time_field = next(p for p in params if p["name"] == "time_field")
    assert time_field["schema"]["default"] == "published"
    assert Settings(contact_email="me@example.com").contact_email == "me@example.com"


RAW_LINK = "https://News.Example.com/2026/10/raw-link-story/?utm_source=rss&id=42#comments"


def _ingest_raw_link(client: TestClient) -> None:
    import asyncio

    from app.ingest import Ingestor

    feed = (
        '<rss version="2.0"><channel><title>t</title><item><title>Raw link story</title>'
        f"<link> {RAW_LINK.replace('&', '&amp;')} </link>"
        "<pubDate>Wed, 07 Oct 2026 23:00:00 GMT</pubDate></item></channel></rss>"
    )
    factory = client.app.state.session_factory
    with factory() as session:
        source = next(s for s in active_sources(session) if s.id == "hacker-news")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=feed.encode()))
    asyncio.run(Ingestor(factory, transport=transport).run([source], force=True))


def test_card_href_is_the_feeds_raw_link(demo_client: TestClient) -> None:
    _ingest_raw_link(demo_client)
    html = demo_client.get("/p/personal-reader").text
    href = RAW_LINK.replace("&", "&amp;")
    assert (
        html.count(f'href="{href}" target="_blank" rel="noopener noreferrer"') == 3
    )  # not clustered yet: the card links to the article
    assert "raw-link-story?" not in html  # never the canonical (dedup) form


def test_articles_api_returns_url_and_canonical_url(demo_client: TestClient) -> None:
    _ingest_raw_link(demo_client)
    item = next(
        a
        for a in demo_client.get("/api/articles?limit=100").json()
        if a["title"] == "Raw link story"
    )
    assert item["url"] == RAW_LINK
    assert item["canonical_url"] == "https://news.example.com/2026/10/raw-link-story/?id=42"
    feed = demo_client.get("/api/profiles/personal-reader/feed?group=articles&limit=100").json()
    assert next(i for i in feed["items"] if i["title"] == "Raw link story")["url"] == RAW_LINK
    schema = demo_client.get("/openapi.json").json()["components"]["schemas"]["ArticleOut"]
    assert "link to show users" in schema["properties"]["url"]["description"].lower()
