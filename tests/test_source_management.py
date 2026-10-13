"""Source management: CRUD + sharing, soft delete, OPML, autodiscovery, test fetch, YAML sync,
catalog, health, admin auth on every write route, missing-secret warnings."""

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from app import source_admin as admin
from app import sources_api, sources_web
from app.config import Source
from app.demo import build_demo
from app.main import create_app
from app.models import Article, SourceRecord, SourceStatus, SourceUsage, Story, Topic

from .conftest import fixture_bytes

ADMIN = "admin-secret-key"
FEED = fixture_bytes("tech_rss.xml")
HTML = b"""<html><head><title>Example</title>
<link rel="alternate" type="application/rss+xml" title="Example news" href="/feed.xml">
<link rel="alternate" type="application/atom+xml" title="Example atom" href="https://example.org/atom">
<link rel="stylesheet" href="/s.css"></head><body>hi</body></html>"""


def site(request: httpx.Request) -> httpx.Response:
    if request.url.path in ("/feed.xml", "/atom", "/opml-only.xml"):
        return httpx.Response(200, content=FEED, headers={"content-type": "application/rss+xml"})
    if request.url.path == "/":
        return httpx.Response(200, content=HTML, headers={"content-type": "text/html"})
    return httpx.Response(404)


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    return build_demo(tmp_path_factory.mktemp("sources") / "demo.db")


def _app(build, **update):
    settings = build.settings.model_copy(update={"scheduler_enabled": False, **update})
    return create_app(settings, llm_transport=build.llm_transport)


@pytest.fixture
def client(build):
    with TestClient(_app(build)) as c:
        c.app.state.ingestor.transport = httpx.MockTransport(site)
        yield c


def counts(c: TestClient) -> dict:
    with c.app.state.session_factory() as session:
        return {
            m.__name__: session.scalar(select(func.count()).select_from(m))
            for m in (Article, Story, SourceRecord, SourceStatus, SourceUsage, Topic)
        }


def test_crud_sharing_and_soft_delete(client) -> None:
    body = {
        "name": "Example news",
        "url": "https://example.org/feed.xml",
        "tags": ["x"],
        "profiles": ["personal-reader"],
    }
    created = client.post("/api/v1/sources", json=body)
    assert created.status_code == 201, created.text
    source = created.json()
    assert source["profiles"] == ["personal-reader"] and source["health"]["state"] == "pending"
    shared = client.post(
        "/api/v1/sources", json={**body, "name": "Same feed", "profiles": ["market-monitor"]}
    )
    assert shared.status_code == 200 and shared.json()["id"] == source["id"]
    assert sorted(shared.json()["profiles"]) == ["market-monitor", "personal-reader"]
    with client.app.state.session_factory() as session:  # one record: fetched once
        assert len(admin.list_sources(session)) == len({s.id for s in admin.list_sources(session)})
        active = [s.id for s in __import__("app.profiles").profiles.active_sources(session)]
        assert active.count(source["id"]) == 1

    edited = client.put(
        f"/api/v1/sources/{source['id']}",
        json={**body, "name": "Renamed", "refresh_minutes": 90, "profiles": None},
    )
    assert edited.status_code == 200 and edited.json()["name"] == "Renamed"
    assert edited.json()["refresh_minutes"] == 90 and len(edited.json()["profiles"]) == 2

    fed = next(s for s in client.get("/api/v1/sources").json() if "Federal" in s["name"])
    before = counts(client)
    removed = client.delete(f"/api/v1/sources/{fed['id']}")
    assert removed.status_code == 200 and removed.json()["removed_at"] is not None
    assert removed.json()["health"]["state"] == "removed" and not removed.json()["enabled"]
    after = counts(client)
    assert after["Article"] == before["Article"] and after["Story"] == before["Story"]
    assert fed["id"] not in {s["id"] for s in client.get("/api/v1/sources").json()}
    listed = client.get("/api/v1/sources?include_removed=true").json()
    assert fed["id"] in {s["id"] for s in listed}
    assert client.get("/p/market-monitor").status_code == 200  # its stories still render
    restored = client.post(f"/api/v1/sources/{fed['id']}/restore")
    assert restored.json()["removed_at"] is None and restored.json()["enabled"]
    assert not client.post(f"/api/v1/sources/{fed['id']}/disable").json()["enabled"]
    assert client.post(f"/api/v1/sources/{fed['id']}/enable").json()["enabled"]


def test_invalid_sources_are_rejected(client) -> None:
    bad = client.post("/api/v1/sources", json={"name": "No url"})
    assert bad.status_code == 422
    key = client.post(
        "/api/v1/sources",
        json={
            "name": "G",
            "type": "guardian",
            "api_key": "abc-123-real-key-value",
            "params": {"q": "x"},
        },
    )
    assert key.status_code == 422 and "abc-123-real-key-value" not in key.text
    assert (
        client.post(
            "/api/v1/sources", json={"name": "X", "type": "x", "params": {"query": "fed"}}
        ).status_code
        == 422
    )  # no cap
    assert client.get("/api/v1/sources/nope").status_code == 404


def test_api_source_stores_only_the_variable_name(client, monkeypatch) -> None:
    monkeypatch.setenv("GUARDIAN_API_KEY", "real-secret-value")
    created = client.post(
        "/api/v1/sources",
        json={
            "name": "Guardian business",
            "type": "guardian",
            "api_key": "${GUARDIAN_API_KEY}",
            "params": {"q": "rates", "section": "business"},
            "daily_request_budget": 50,
            "profiles": ["market-monitor"],
        },
    )
    assert created.status_code == 201, created.text
    out = created.json()
    assert out["api_key_env"] == "GUARDIAN_API_KEY" and out["api_key_set"] is True
    assert out["limits"]["daily_request_budget"] == 50
    assert out["url"].startswith("https://content.guardianapis.com")
    page = client.get(f"/p/market-monitor/settings/sources/{out['id']}").text
    everything = client.get("/api/v1/sources").text + page
    assert "real-secret-value" not in everything and "GUARDIAN_API_KEY" in page


def test_feed_autodiscovery() -> None:
    found = admin.feed_links(HTML.decode(), "https://example.org/blog/")
    assert [(c.url, c.type.value, c.title) for c in found] == [
        ("https://example.org/feed.xml", "rss", "Example news"),
        ("https://example.org/atom", "atom", "Example atom"),
    ]
    assert admin.feed_links("<html><head></head></html>", "https://e.org/") == []


def test_discover_and_test_fetch_write_nothing(client) -> None:
    before = counts(client)
    found = client.post("/api/v1/sources/discover", json={"url": "https://example.org/"}).json()
    assert [c["url"] for c in found] == ["https://example.org/feed.xml", "https://example.org/atom"]
    direct = client.post("/api/v1/sources/discover", json={"url": "https://example.org/feed.xml"})
    assert [c["url"] for c in direct.json()] == ["https://example.org/feed.xml"]
    preview = client.post(
        "/api/v1/sources/test", json={"name": "t", "url": "https://example.org/feed.xml"}
    ).json()
    assert preview["ok"] and preview["status"] == 200 and preview["item_count"] > 0
    assert preview["title"] and 0 < len(preview["items"]) <= 5 and preview["latency_ms"] >= 0
    dates = [i["published_at"] for i in preview["items"] if i["published_at"]]
    assert dates == sorted(dates, reverse=True)
    failed = client.post(
        "/api/v1/sources/test", json={"name": "t", "url": "https://example.org/missing"}
    ).json()
    assert not failed["ok"] and failed["status"] == 404 and "404" in failed["error"]
    html = client.post(
        "/p/personal-reader/settings/sources/test",
        data={"url": "https://example.org/feed.xml", "type": "rss"},
    ).text
    assert "nothing has been saved" in html and "Save source" in html
    page = client.post(
        "/p/personal-reader/settings/sources/discover", data={"url": "https://example.org/"}
    ).text
    assert "Example atom" in page and "Test fetch" in page
    assert counts(client) == before


def test_web_add_flow_saves_after_test(client) -> None:
    saved = client.post(
        "/p/personal-reader/settings/sources",
        data={
            "name": "Web added",
            "url": "https://example.org/atom",
            "type": "atom",
            "tags": "a, b",
            "refresh_minutes": "15",
            "enabled": "on",
            "profiles": "personal-reader",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303 and "Added" in client.get(saved.headers["location"]).text
    source = next(s for s in client.get("/api/v1/sources").json() if s["name"] == "Web added")
    assert source["tags"] == ["a", "b"] and source["refresh_minutes"] == 15


def test_opml_round_trip(client) -> None:
    exported = client.get("/api/v1/profiles/market-monitor/sources.opml")
    assert exported.status_code == 200 and exported.headers["content-type"].startswith(
        "text/x-opml"
    )
    text = exported.text
    assert "<opml" in text and "Federal Reserve press releases" in text and 'category="' in text
    opml = text.replace(
        "</body>",
        '<outline text="Broken" xmlUrl="not a url"/>\n'
        '<outline text="New feed" xmlUrl="https://example.org/opml-only.xml"'
        ' category="tech,ai"/></body>',
    )
    result = client.post("/api/v1/profiles/personal-reader/sources.opml", content=opml).json()
    assert result["added"] == ["new-feed"] and len(result["shared"]) >= 3
    assert len(result["errors"]) == 1 and "Broken" in result["errors"][0]
    again = client.post("/api/v1/profiles/personal-reader/sources.opml", content=opml).json()
    assert again["added"] == [] and again["shared"] == [] and len(again["errors"]) == 1
    back = client.get("/api/v1/profiles/personal-reader/sources.opml").text
    for line in text.splitlines():
        if "xmlUrl" in line:
            assert line.strip() in back  # name, url and tags survive the round trip
    new = next(s for s in client.get("/api/v1/sources").json() if s["id"] == "new-feed")
    assert new["tags"] == ["tech", "ai"]
    bad = client.post("/api/v1/profiles/personal-reader/sources.opml", content="<html>")
    assert bad.status_code == 422
    page = client.post("/p/personal-reader/settings/sources/opml", data={"opml": opml}).text
    assert "already here" in page


def test_yaml_sync_adds_missing_and_never_deletes_or_overwrites(client) -> None:
    sources = client.get("/api/v1/sources").json()
    first, second = sources[0], sources[1]
    client.put(
        f"/api/v1/sources/{first['id']}",
        json={"name": "Edited in app", "url": first["url"], "refresh_minutes": 77},
    )
    client.delete(f"/api/v1/sources/{second['id']}")
    extra = client.post(
        "/api/v1/sources", json={"name": "App only", "url": "https://example.org/x"}
    )
    with client.app.state.session_factory() as session:
        topic = session.scalars(select(Topic)).first()
        topic_name = topic.name
        session.delete(topic)
        session.commit()
    before = counts(client)
    result = client.post("/api/v1/sources/sync-yaml").json()
    after = counts(client)
    assert all(after[k] >= before[k] for k in before)
    assert (
        topic_name in " ".join(result["topics_added"]) and second["id"] in result["skipped_removed"]
    )
    listed = {s["id"]: s for s in client.get("/api/v1/sources?include_removed=true").json()}
    assert listed[first["id"]]["name"] == "Edited in app"
    assert listed[first["id"]]["refresh_minutes"] == 77
    assert listed[second["id"]]["removed_at"] is not None and extra.json()["id"] in listed
    assert client.post("/api/v1/sources/sync-yaml").json()["topics_added"] == []


def test_catalog_is_well_formed() -> None:
    entries = admin.load_catalog()
    assert len(entries) >= 20 and len({e.id for e in entries}) == len(entries)
    assert len({e.url for e in entries}) == len(entries)
    categories = {e.category for e in entries}
    for word in ("Finance", "Regulators", "Technology", "Science", "World"):
        assert any(word in c for c in categories), word
    for entry in entries:
        source = entry.to_source()
        assert isinstance(source, Source) and str(source.url).startswith("https://")


def test_catalog_picker_adds_selected(client) -> None:
    entry = admin.load_catalog()[0]
    assert entry.name in client.get("/p/personal-reader/settings/sources/catalog").text
    added = client.post("/p/personal-reader/settings/sources/catalog", data={"entry": entry.id})
    assert "Added 1 source" in added.text and "added</span>" in added.text
    assert client.get("/api/v1/source-catalog").json()[0]["id"] == entry.id


def test_health_settings_and_auto_disabled_banner(client) -> None:
    health = client.get("/api/v1/sources/health").json()
    fed = next(h for h in health if "Federal" in h["name"])
    assert fed["state"] == "ok" and fed["last_success_at"] and fed["items_per_day"] > 0
    assert {
        "consecutive_failures",
        "last_error",
        "next_fetch_at",
        "requests_today",
        "spend_this_month_usd",
        "monthly_budget_usd",
    } <= fed.keys()
    with client.app.state.session_factory() as session:
        record = session.get(SourceRecord, fed["id"])
        record.enabled, record.auto_disabled_at = False, admin.utcnow()
        record.disabled_reason = "10 consecutive failures; last: HTTP 500"
        session.commit()
    page = client.get("/p/market-monitor/settings").text
    assert f'data-auto-disabled="{fed["id"]}"' in page and "Re-enable" in page
    assert "data-source-health" in page
    client.post(f"/p/market-monitor/settings/sources/{fed['id']}/enable")
    state = client.get(f"/api/v1/sources/{fed['id']}").json()
    assert state["enabled"] and state["health"]["state"] != "auto_disabled"
    assert "data-auto-disabled" not in client.get("/p/market-monitor/settings").text


def test_every_source_write_route_needs_admin(build) -> None:
    with TestClient(_app(build, admin_key=SecretStr(ADMIN))) as c:
        writes = [
            (method, route.path)
            for route in [*sources_api.router.routes, *sources_web.router.routes]
            for method in route.methods - {"GET", "HEAD"}
        ]
        assert len(writes) >= 15
        for method, path in writes:
            url = path.format(slug="personal-reader", source_id="x", action="enable")
            response = c.request(method, url, follow_redirects=False)
            assert response.status_code in (401, 303), (method, path, response.status_code)
        assert c.get("/api/v1/sources").status_code == 401
        admin_hdr = {"Authorization": f"Bearer {ADMIN}"}
        assert c.get("/api/v1/sources", headers=admin_hdr).status_code == 200
        assert c.post("/api/v1/sources/x/enable", headers=admin_hdr).status_code == 404


def test_missing_secret_warnings_name_variable_not_value(build, monkeypatch, caplog) -> None:
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    with TestClient(_app(build)) as c:
        alerts = c.get("/p/personal-reader/alerts").text
        settings = c.get("/p/personal-reader/settings").text
    assert "NTFY_TOKEN" in caplog.text and "is not set" in caplog.text
    for html in (alerts, settings):
        assert "data-secret-warnings" in html and "NTFY_TOKEN" in html
    monkeypatch.setenv("NTFY_TOKEN", "tok-very-secret")
    with TestClient(_app(build)) as c:
        html = c.get("/p/personal-reader/alerts").text + c.get("/p/personal-reader/settings").text
    assert "tok-very-secret" not in html and "NTFY_TOKEN is not set" not in html
