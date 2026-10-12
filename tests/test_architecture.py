"""Pages and read APIs never generate: they import no summarization/LLM code and work
with a generation worker that refuses every call."""

import ast
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.demo import build_demo
from app.main import create_app

APP = Path(__file__).resolve().parent.parent / "app"
READ_SIDE = ["web.py", "api.py", "story_views.py", "pipeline_panel.py", "events.py"]
FORBIDDEN = ("app.summarize", "app.llm", "app.publish")


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", READ_SIDE)
def test_read_side_imports_no_generation_code(module: str) -> None:
    bad = {n for n in _imports(APP / module) if n.startswith(FORBIDDEN)}
    assert not bad, f"app/{module} imports {sorted(bad)}"


class Refuse:
    """Stands in for every worker method that could generate or queue."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"a GET route called worker.{self.name}")


def test_no_get_route_starts_or_waits_for_generation(tmp_path: Path) -> None:
    build = build_demo(tmp_path / "demo.db")
    app = create_app(build.settings, llm_transport=build.llm_transport)
    with TestClient(app) as client:
        worker = app.state.summaries
        for name in ("start", "run", "process", "enqueue", "enqueue_all", "retry", "prioritize"):
            setattr(worker, name, Refuse(name))
        story = client.get("/api/profiles/personal-reader/feed").json()["items"][0]
        values = {"slug": "personal-reader", "story_id": story["id"], "article_id": "the-verge-1"}
        for route in app.routes:
            if not isinstance(route, APIRoute) or "GET" not in route.methods:
                continue
            if route.path.endswith("/stream"):
                continue  # covered by tests/test_publish_gate.py (never returns)
            path = re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), 1)), route.path)
            if route.path == "/api/admin/articles/{article_id}":
                path = "/api/admin/articles/1"
            if route.path == "/api/profiles/{slug}/published":
                path += "?since=2020-01-01T00:00:00Z"
            response = client.get(path, follow_redirects=False)
            assert response.status_code < 500, (path, response.status_code)
