import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import init_db, make_engine
from app.demo import build_demo
from app.main import create_app

NIMBUS = (
    "SEC Charges Nimbus Lending and Founder Mark Ellery With Defrauding Retail Crypto Investors"
)
EXTRACTED_ONLY = "third-generation 3-nanometer process"  # appears only in a fixture page body


@pytest.fixture(scope="module")
def app_client(tmp_path_factory):
    build = build_demo(tmp_path_factory.mktemp("stories") / "demo.db")
    with TestClient(create_app(build.settings, llm_transport=build.llm_transport)) as c:
        yield c


_GENERATED = {"summary", "brief", "report"}
_GENERATED_HTML = re.compile(
    r"<div data-summary.*?</div>|<section[^>]*data-summary.*?</section>|"
    r"<section id=\"report\".*?</section>",
    re.S,
)


def _strip_generated(data):
    if isinstance(data, dict):
        return {k: _strip_generated(v) for k, v in data.items() if k not in _GENERATED}
    if isinstance(data, list):
        return [_strip_generated(v) for v in data]
    return data


def _without_generated(resp) -> str:
    """The response minus model-written briefs/reports, which may paraphrase article text."""
    if resp.headers["content-type"].startswith("application/json"):
        return json.dumps(_strip_generated(resp.json()))
    return _GENERATED_HTML.sub("", resp.text)


def _article_text_keys(data, path="") -> list[str]:
    """Paths of any ``text`` key outside generated ``summary``/``brief``/``report`` objects."""
    if isinstance(data, list):
        return [p for i, v in enumerate(data) for p in _article_text_keys(v, f"{path}[{i}]")]
    if not isinstance(data, dict):
        return []
    found = [f"{path}.text"] if "text" in data else []
    return found + [
        p
        for k, v in data.items()
        if k not in _GENERATED
        for p in _article_text_keys(v, f"{path}.{k}")
    ]


def _story(items, source_count):
    return next(i for i in items if i["source_count"] == source_count and i["id"])


def test_story_feed_groups_profile_sources(app_client: TestClient) -> None:
    feed = app_client.get("/api/profiles/market-monitor/feed").json()
    assert feed["group"] == "stories"
    multi = [i for i in feed["items"] if i["source_count"] > 1]
    assert {i["source_count"] for i in multi} == {3}
    nimbus = next(i for i in multi if "Nimbus" in i["title"])
    # TechCrunch also covered it but isn't a market-monitor source.
    assert [s["name"] for s in nimbus["sources"]] == [
        "SEC press releases",
        "CNBC Markets",
        "Guardian Business",
    ]
    assert nimbus["sources"][0]["url"] == "https://www.sec.gov/newsroom/press-releases/2026-118"
    assert nimbus["snippet"] and nimbus["image_url"]
    updated = [i["last_updated_at"] for i in feed["items"]]
    assert updated == sorted(updated, reverse=True)


def test_story_feed_paginates_and_filters_by_any_member(app_client: TestClient) -> None:
    base = "/api/profiles/market-monitor"
    everything = app_client.get(f"{base}/feed", params={"limit": 100}).json()["items"]
    first = app_client.get(f"{base}/feed", params={"limit": 4}).json()
    second = app_client.get(f"{base}/feed", params={"limit": 4, "cursor": first["next_cursor"]})
    titles = [i["title"] for i in first["items"] + second.json()["items"]]
    assert titles == [i["title"] for i in everything[:8]]

    # "third cut" only appears in the CNBC summary, yet the whole story matches.
    topic = app_client.post(
        f"{base}/topics", json={"name": "Third cut", "include_keywords": ["third cut"]}
    ).json()
    items = app_client.get(f"{base}/feed", params={"topic": topic["id"]}).json()["items"]
    assert len(items) == 1
    assert {s["name"] for s in items[0]["sources"]} == {
        "CNBC Markets",
        "Guardian Business",
        "BBC Business",
    }
    app_client.delete(f"{base}/topics/{topic['id']}")


def test_articles_group_still_supported(app_client: TestClient) -> None:
    feed = app_client.get("/api/profiles/market-monitor/feed?group=articles&limit=100").json()
    assert feed["group"] == "articles"
    assert {"url", "canonical_url", "source_name"} <= set(feed["items"][0])
    assert len(feed["items"]) > len(
        app_client.get("/api/profiles/market-monitor/feed?limit=100").json()["items"]
    )


def test_story_endpoint_lists_all_members(app_client: TestClient) -> None:
    items = app_client.get("/api/profiles/market-monitor/feed").json()["items"]
    nimbus = next(i for i in items if i["title"] == NIMBUS)
    story = app_client.get(f"/api/stories/{nimbus['id']}").json()
    assert len(story["members"]) == 4 and story["source_count"] == 4
    assert {m["source"] for m in story["members"]} >= {"TechCrunch", "SEC press releases"}
    assert all(m["url"].startswith("https://") and m["summary"] for m in story["members"])
    scoped = app_client.get(f"/api/stories/{nimbus['id']}?profile=market-monitor").json()
    assert len(scoped["members"]) == 3
    assert app_client.get("/api/stories/99999").status_code == 404


def test_extracted_text_never_public(app_client: TestClient) -> None:
    responses = [
        app_client.get("/api/profiles/personal-reader/feed?limit=100"),
        app_client.get("/api/profiles/personal-reader/feed?group=articles&limit=100"),
        app_client.get("/api/articles?limit=500"),
        app_client.get("/p/personal-reader"),
        app_client.get("/p/personal-reader?view=articles"),
    ]
    story_id = _story(responses[0].json()["items"], 4)["id"]
    responses += [app_client.get(f"/api/stories/{story_id}"), app_client.get(f"/story/{story_id}")]
    for resp in responses:
        assert resp.status_code == 200
        assert EXTRACTED_ONLY not in _without_generated(resp)
        assert '"text_status"' not in resp.text
        if resp.headers["content-type"].startswith("application/json"):
            assert not _article_text_keys(resp.json())

    articles = app_client.get("/api/profiles/personal-reader/feed?group=articles&limit=100")
    aid = next(a["id"] for a in articles.json()["items"] if "M5 MacBook Pro is here" in a["title"])
    hidden = app_client.get(f"/api/admin/articles/{aid}").json()
    assert hidden["text_status"] == "ok" and hidden["text"] is None
    shown = app_client.get(f"/api/admin/articles/{aid}?include_text=true").json()
    assert EXTRACTED_ONLY in shown["text"]


def test_admin_extract_and_cluster_endpoints(app_client: TestClient) -> None:
    extract = app_client.post("/api/admin/extract")
    assert extract.status_code == 200 and extract.json()["processed"] == 0
    cluster = app_client.post("/api/admin/cluster").json()
    assert cluster["processed"] == 0 and cluster["multi_source_stories"] == 4
    rebuilt = app_client.post("/api/admin/cluster?rebuild=true").json()
    assert rebuilt["rebuild"] and rebuilt["multi_source_stories"] == 4


def test_story_cards_badges_and_story_page(app_client: TestClient) -> None:
    html = app_client.get("/p/personal-reader").text
    assert "4 sources" in html and "Covered by 4 sources" in html
    assert 'aria-label="Group by"' in html and ">Stories</a>" in html
    assert "Read on Quanta Magazine" in html  # single-article stories keep the old card
    story_id = _story(app_client.get("/api/profiles/personal-reader/feed").json()["items"], 4)["id"]
    assert f'href="/story/{story_id}?p=personal-reader"' in html

    topics = app_client.get("/api/profiles/personal-reader").json()["topics"]
    ai = next(t for t in topics if t["name"] == "AI")
    page = app_client.get(f"/story/{story_id}?p=personal-reader&topic={ai['id']}").text
    assert f'href="/p/personal-reader?topic={ai["id"]}"' in page and "Back to feed" in page
    assert page.count('<li class="p-4">') == 4
    assert 'target="_blank" rel="noopener noreferrer"' in page
    story = app_client.get(f"/api/stories/{story_id}").json()
    for member in story["members"]:
        assert f'href="{member["url"]}"' in page.replace("&amp;", "&")

    articles = app_client.get("/p/personal-reader?view=articles").text
    assert "sources</span>" not in articles and "view=articles" in articles


def test_settings_pipeline_panel(app_client: TestClient) -> None:
    html = app_client.get("/p/market-monitor/settings").text
    assert "Pipeline" in html and "Text ok" in html and "Run clustering" in html
    for endpoint in ("refresh", "extract", "cluster"):
        assert f'hx-post="/api/admin/{endpoint}"' in html
    panel = app_client.get("/p/market-monitor/settings/pipeline").text
    assert "<html" not in panel and "stories" in panel and "not yet" not in panel


def test_migration_adds_pipeline_columns(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with engine.begin() as conn:  # pre-extraction articles schema
        conn.execute(
            text(
                "CREATE TABLE articles (id INTEGER PRIMARY KEY, source_id VARCHAR(100),"
                " url VARCHAR(2048) NOT NULL DEFAULT '', canonical_url VARCHAR(2048) UNIQUE,"
                " title TEXT, summary_raw TEXT, author VARCHAR(500), published_at DATETIME,"
                " fetched_at DATETIME, image_url VARCHAR(2048), content_hash VARCHAR(64),"
                " raw_json JSON)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO articles (source_id, url, canonical_url, title, summary_raw,"
                " published_at, fetched_at, content_hash, raw_json) VALUES ('s', 'u', 'u', '',"
                " '', '2026-10-07 00:00:00', '2026-10-07 00:00:00', 'h', '{}')"
            )
        )
    init_db(engine)
    init_db(engine)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT text_status, story_id, text FROM articles")).one()
    assert tuple(row) == ("pending", None, None)
    engine.dispose()


def test_demo_build_reports_pipeline(tmp_path: Path) -> None:
    build = build_demo(tmp_path / "d.db")
    assert build.extracted.ok >= 20 and build.extracted.skipped == 1
    reasons = {r.reason for r in build.extracted.results}
    assert "disallowed by robots.txt" in reasons
    assert any(r and r.startswith("extracted 0 words") for r in reasons)
    assert json.dumps(build.clustered.model_dump())  # serializable for the admin endpoint
