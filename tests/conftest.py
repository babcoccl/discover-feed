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
