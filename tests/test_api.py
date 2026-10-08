import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ingest import USER_AGENT, Ingestor
from app.main import create_app
from app.settings import Settings
from tests.conftest import EXAMPLE_CONFIG


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["profiles"] == 2


def test_home_lists_profiles(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Personal Reader" in resp.text
    assert "Market Monitor" in resp.text


def test_openapi_docs(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert {"/health", "/api/articles", "/api/admin/refresh"} <= set(schema["paths"])


def test_missing_config_starts_empty(tmp_path: Path) -> None:
    settings = Settings(
        config_path=tmp_path / "missing.yaml",
        database_url=f"sqlite:///{tmp_path / 'db' / 'test.db'}",
        scheduler_enabled=False,
    )
    with TestClient(create_app(settings)) as c:
        assert c.get("/health").json()["profiles"] == 0
        assert "No profiles configured" in c.get("/").text
    assert (tmp_path / "db" / "test.db").exists()


def test_scheduler_starts_and_stops(tmp_path: Path) -> None:
    settings = Settings(
        config_path=tmp_path / "missing.yaml",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        scheduler_enabled=True,
    )
    app = create_app(settings)
    with TestClient(app):
        assert app.state.scheduler.running
    assert not app.state.scheduler.running


def _refresh_transport() -> httpx.MockTransport:
    from tests.conftest import fixture_bytes

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hnrss.org":
            return httpx.Response(200, content=fixture_bytes("tech_rss.xml"))
        if request.url.host == "www.quantamagazine.org":
            return httpx.Response(200, content=fixture_bytes("science_atom.xml"))
        return httpx.Response(500)

    return httpx.MockTransport(handler)


def test_refresh_and_list_articles(client: TestClient) -> None:
    app = client.app
    app.state.ingestor = Ingestor(app.state.session_factory, transport=_refresh_transport())

    resp = client.post("/api/admin/refresh")
    assert resp.status_code == 200
    results = {r["source_id"]: r for r in resp.json()["results"]}
    assert len(results) == 6
    assert results["hacker-news"]["new_articles"] == 3
    assert results["quanta-magazine"]["new_articles"] == 2
    assert results["ars-technica"]["status"] == "error"

    body = client.get("/api/articles").json()
    assert len(body) == 5
    published = [a["published_at"] for a in body]
    assert published == sorted(published, reverse=True)
    assert set(body[0]) == {
        "id",
        "source_id",
        "url",
        "canonical_url",
        "title",
        "summary_raw",
        "author",
        "published_at",
        "fetched_at",
        "image_url",
        "content_hash",
    }
    assert body[0]["title"] == "Webb spots water vapour on a temperate exoplanet"
    assert body[0]["published_at"] == "2026-10-06T18:30:00Z"

    assert len(client.get("/api/articles?limit=2").json()) == 2
    only = client.get("/api/articles?source_id=quanta-magazine").json()
    assert {a["source_id"] for a in only} == {"quanta-magazine"} and len(only) == 2
    since = client.get("/api/articles", params={"since": "2026-10-06T00:00:00Z"}).json()
    assert len(since) == 3
    fetched = client.get(
        "/api/articles", params={"since": "2026-10-06T00:00:00Z", "time_field": "fetched"}
    ).json()
    assert len(fetched) == 5
    future = {"since": "2999-01-01T00:00:00Z", "time_field": "fetched"}
    assert client.get("/api/articles", params=future).json() == []
    assert client.get("/api/articles?time_field=updated").status_code == 422
    assert client.get("/api/articles?limit=0").status_code == 422


def test_refresh_single_source(client: TestClient) -> None:
    app = client.app
    app.state.ingestor = Ingestor(app.state.session_factory, transport=_refresh_transport())

    resp = client.post("/api/admin/refresh", params={"source_id": "hacker-news"})
    assert resp.status_code == 200
    assert [r["source_id"] for r in resp.json()["results"]] == ["hacker-news"]
    assert client.post("/api/admin/refresh?source_id=nope").status_code == 404


def test_scheduler_registers_ingestion_jobs(tmp_path: Path) -> None:
    from tests.conftest import EXAMPLE_CONFIG

    settings = Settings(
        config_path=EXAMPLE_CONFIG,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        scheduler_enabled=True,
    )
    app = create_app(settings)
    with TestClient(app):
        app.state.scheduler.pause()
        ids = {job.id for job in app.state.scheduler.get_jobs()}
        assert ids == {f"ingest:{s.id}" for s in app.state.config.all_sources()}
        assert len(ids) == 6


def test_warns_when_sec_source_has_no_contact_email(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISCOVER_CONTACT_EMAIL", raising=False)
    base = {
        "config_path": EXAMPLE_CONFIG,
        "database_url": f"sqlite:///{tmp_path}/t.db",
        "scheduler_enabled": False,
    }
    with (
        caplog.at_level(logging.WARNING, logger="discover_feed"),
        TestClient(create_app(Settings(**base))) as c,
    ):
        assert c.app.state.ingestor.user_agent == USER_AGENT
    assert "sec-press-releases" in caplog.text and "DISCOVER_CONTACT_EMAIL" in caplog.text

    caplog.clear()
    settings = Settings(**base, contact_email="me@example.com")
    with (
        caplog.at_level(logging.WARNING, logger="discover_feed"),
        TestClient(create_app(settings)) as c,
    ):
        assert c.app.state.ingestor.user_agent.endswith("; me@example.com)")
    assert "DISCOVER_CONTACT_EMAIL" not in caplog.text
