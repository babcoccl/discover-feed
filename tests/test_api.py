from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings


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
    assert "/health" in schema["paths"]


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
