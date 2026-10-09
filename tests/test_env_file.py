import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import load_config
from app.settings import load_env_file

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = ROOT / "config" / "profiles.example.yaml"
VARS = ("LOCAL_LLM_BASE_URL", "LOCAL_LLM_API_KEY", "LOCAL_LLM_MODEL")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in VARS:  # setenv+delenv so teardown restores whatever load_dotenv wrote
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    return monkeypatch


def test_env_file_feeds_profile_interpolation(tmp_path: Path, clean_env) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "LOCAL_LLM_BASE_URL=http://from-dotenv:8080/v1\n"
        "LOCAL_LLM_API_KEY=dotenv-key\n"
        "LOCAL_LLM_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    clean_env.setenv("LOCAL_LLM_MODEL", "shell-model")
    assert load_env_file(env)
    role = load_config(EXAMPLE_CONFIG).get_profile("personal-reader").llm.summarizer
    assert str(role.base_url).startswith("http://from-dotenv:8080/v1")
    assert role.api_key.get_secret_value() == "dotenv-key"
    assert role.model == "shell-model"  # the shell wins over .env


def test_missing_env_file_is_fine(tmp_path: Path, clean_env) -> None:
    assert load_env_file(tmp_path / "absent.env") is False
    role = load_config(EXAMPLE_CONFIG).get_profile("personal-reader").llm.summarizer
    assert role.api_key is None


def test_dotenv_in_cwd_is_loaded_on_import(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("LOCAL_LLM_BASE_URL=http://cwd-dotenv:8080/v1\n")
    env = {k: v for k, v in os.environ.items() if k not in (*VARS, "DISCOVER_ENV_FILE")}
    env["PYTHONPATH"] = str(ROOT)
    out = subprocess.run(
        [sys.executable, "-c", "import os, app.settings; print(os.environ['LOCAL_LLM_BASE_URL'])"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "http://cwd-dotenv:8080/v1"
