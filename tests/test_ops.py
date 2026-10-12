"""Docker/ops: compose passes .env through, documented variables, fast shutdown with SSE open."""

import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_compose_passes_env_file_and_keeps_defaults() -> None:
    app = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"][
        "app"
    ]
    assert app["env_file"] == [{"path": ".env", "required": False}]
    assert app["stop_grace_period"] == "15s"
    env = app["environment"]
    assert env["DISCOVER_CONFIG_PATH"].endswith(":-/app/config/profiles.example.yaml}")
    assert {"OPENAI_API_KEY", "LOCAL_LLM_BASE_URL", "DISCOVER_LOG_LEVEL"} <= env.keys()
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert '"--timeout-graceful-shutdown", "5"' in dockerfile


def test_env_example_documents_every_variable() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    lines = text.splitlines()
    names = [
        "DISCOVER_ADMIN_KEY",
        "DISCOVER_API_KEYS",
        "DISCOVER_TIMEZONE",
        "DISCOVER_PUBLIC_URL",
        "NTFY_SERVER",
        "NTFY_TOPIC",
        "NTFY_TOKEN",
        "ALERT_WEBHOOK_URL",
        "ALERT_WEBHOOK_SECRET",
        "SMTP_HOST",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "ALERT_EMAIL_FROM",
        "ALERT_EMAIL_TO",
        "GUARDIAN_API_KEY",
        "NEWSDATA_API_KEY",
        "X_BEARER_TOKEN",
        "DISCOVER_SOURCE_AUTO_DISABLE_FAILURES",
    ]
    for name in names:
        index = next(i for i, line in enumerate(lines) if re.match(rf"#?\s*{name}=", line))
        assert lines[index - 1].startswith("#"), f"{name} has no comment above it"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT to a child process is POSIX-only")
def test_shutdown_is_fast_with_an_open_sse_client(tmp_path) -> None:
    port = _free_port()
    env = {
        **os.environ,
        "DISCOVER_DATABASE_URL": f"sqlite:///{tmp_path / 'db.sqlite'}",
        "DISCOVER_CONFIG_PATH": str(ROOT / "config" / "profiles.example.yaml"),
        "DISCOVER_SCHEDULER_ENABLED": "false",
        "DISCOVER_SUMMARIZE_ENABLED": "false",
        "LOCAL_LLM_BASE_URL": "http://127.0.0.1:9",
    }
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--port",
        str(port),
        "--timeout-graceful-shutdown",
        "2",
    ]
    proc = subprocess.Popen(
        cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(f"{base}/api/v1/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("server did not start")
        slug = httpx.get(f"{base}/api/v1/profiles").json()[0]["slug"]
        opened = threading.Event()

        def listen() -> None:
            try:
                with httpx.stream("GET", f"{base}/api/v1/stream?profile={slug}", timeout=None) as r:
                    opened.set()
                    for _ in r.iter_raw():
                        pass
            except httpx.HTTPError:
                pass

        threading.Thread(target=listen, daemon=True).start()
        assert opened.wait(10)
        start = time.monotonic()
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=12)
        assert time.monotonic() - start < 10
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
