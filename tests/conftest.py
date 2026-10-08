from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = ROOT / "config" / "profiles.example.yaml"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        config_path=EXAMPLE_CONFIG,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        scheduler_enabled=False,
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as c:
        yield c


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def session_factory(tmp_path: Path):
    from app.db import init_db, make_engine, make_session_factory

    engine = make_engine(f"sqlite:///{tmp_path / 'ingest.db'}")
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never reach a real LLM: an LLMClient without a fake transport fails loudly."""
    import httpx

    from app.llm import client

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"test tried to call a real LLM at {request.url}")

    original = client.LLMClient.__init__

    def guarded(self, role, *, transport=None):
        original(self, role, transport=transport or httpx.MockTransport(refuse))

    monkeypatch.setattr(client.LLMClient, "__init__", guarded)
