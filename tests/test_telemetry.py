"""Phase 16: workflow observability — llm_calls/stage_events, never-fail guarantee,
settings pages, admin API and the report CLI."""

import ast
import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import telemetry
from app.models import LlmCall, StageEvent

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"


@pytest.fixture
def telemetry_on(session_factory):
    """Configure telemetry against a scratch DB and restore it afterwards."""
    old = telemetry._config
    telemetry.configure(session_factory, config=None)
    telemetry.set_enabled(True)
    yield session_factory
    telemetry._config = old


def _llm_calls(session_factory):
    from app.telemetry import writer

    writer.flush(timeout=5)
    with session_factory() as session:
        return session.scalars(select(LlmCall)).all()


def _stages(session_factory):
    from app.telemetry import writer

    writer.flush(timeout=5)
    with session_factory() as session:
        return session.scalars(select(StageEvent)).all()


# --- purpose discipline --------------------------------------------------------


def test_every_chat_call_names_a_purpose():
    """No `.chat(`/`.stream_chat(` call site in app/ may omit ``purpose``."""
    missing = []
    for path in APP.rglob("*.py"):
        if "telemetry" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"chat", "stream_chat"}
                and not any(k.arg == "purpose" for k in node.keywords)
            ):
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not missing, f"chat calls missing purpose: {missing}"


def test_all_purposes_in_the_map():
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"chat", "stream_chat"}
            ):
                continue
            for k in node.keywords:
                if k.arg == "purpose" and isinstance(k.value, ast.Constant):
                    assert k.value.value in telemetry.PURPOSES, (
                        f"{path}:{node.lineno} purpose {k.value.value!r} unknown"
                    )


def test_chat_without_purpose_fails():
    """``purpose`` is a required keyword: omitting it raises TypeError."""
    import httpx

    from app.llm.client import LLMClient, LLMRole

    role = LLMRole(base_url="http://llm.test/v1", model="m")
    client = LLMClient(role, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(TypeError):
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))  # type: ignore[call-arg]


# --- llm_calls rows --------------------------------------------------------------


def _role():
    from app.llm.client import LLMRole

    return LLMRole(base_url="http://llm.test/v1", model="m")


def _ok(request):
    import httpx

    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        },
    )


def test_chat_records_row(telemetry_on):
    import httpx

    from app.llm.client import LLMClient

    with telemetry.story_context(7):
        asyncio.run(
            LLMClient(_role(), transport=httpx.MockTransport(_ok)).chat(
                [{"role": "user", "content": "hi"}], purpose="brief"
            )
        )
    rows = _llm_calls(telemetry_on)
    assert len(rows) == 1
    row = rows[0]
    assert (row.purpose, row.category, row.story_id, row.status) == (
        "brief",
        "summarization",
        7,
        "ok",
    )
    assert row.prompt_tokens == 10 and row.completion_tokens == 4
    assert row.queue_wait_ms is not None and row.latency_ms >= 0


def test_failed_call_recorded_not_raised(telemetry_on):
    import httpx

    from app.llm.client import LLMClient, LLMHTTPError

    refuse = httpx.MockTransport(lambda r: httpx.Response(500, text="down"))
    with pytest.raises(LLMHTTPError):
        asyncio.run(
            LLMClient(_role(), transport=refuse).chat(
                [{"role": "user", "content": "hi"}], purpose="judge"
            )
        )
    rows = _llm_calls(telemetry_on)
    assert rows[0].status in ("error", "failed") and rows[0].error_kind


def test_emit_never_raises(tmp_path):
    """A broken writer must never break the pipeline."""
    telemetry._config = None  # unconfigured: emit is a no-op
    telemetry.emit_llm_call(call_ref="x", purpose="brief")
    with telemetry.story_context(1), telemetry.stage("ingest"):
        pass


def test_validation_failure_updates_row(telemetry_on):
    import httpx

    from app.llm.client import LLMClient

    async def go():
        return await LLMClient(_role(), transport=httpx.MockTransport(_ok)).chat(
            [{"role": "user", "content": "hi"}], purpose="brief"
        )

    result = asyncio.run(go())
    telemetry.update_llm_call(
        result.call_ref, status="invalid", validation_error="verdict verbatim"
    )
    rows = _llm_calls(telemetry_on)
    assert rows[0].status == "invalid" and "verdict" in rows[0].validation_error


# --- stage events ----------------------------------------------------------------


def test_stage_context_manager(telemetry_on):
    with telemetry.story_context(3), telemetry.stage("enrich", detail={"added": 2}):
        time.sleep(0.01)
    rows = [r for r in _stages(telemetry_on) if r.stage == "enrich"]
    assert len(rows) == 1
    assert rows[0].story_id == 3 and rows[0].status == "ok"
    assert rows[0].detail.get("duration_ms", 0) >= 0


def test_stage_failure_recorded(telemetry_on):
    with pytest.raises(RuntimeError), telemetry.stage("extract"):
        raise RuntimeError("boom")
    rows = _stages(telemetry_on)
    assert rows[0].status == "failed" and "boom" in rows[0].detail.get("error", "")


# --- retention + redaction -------------------------------------------------------


def test_prune_drops_old_rows(telemetry_on):
    old = datetime.now(UTC) - timedelta(days=30)
    telemetry.emit_llm_call(call_ref="old", purpose="brief", started_at=old, finished_at=old)
    telemetry.emit_stage("ingest", started_at=old, finished_at=old)
    telemetry.emit_llm_call(call_ref="new", purpose="brief")
    _llm_calls(telemetry_on)
    pruned = telemetry.prune(now=datetime.now(UTC))
    with telemetry_on() as session:
        left = session.scalar(select(func.count(LlmCall.id)))
        stages = session.scalar(select(func.count(StageEvent.id)))
    assert pruned == 2 and left == 1 and stages == 0


def test_scrub_redacts_keys():
    text = 'call api_key=sk-abc123secret and token "AIzaSyDabcdefghijklmnopqrstuv"'
    out = telemetry.scrub(text, "sk-abc123secret")
    assert "sk-abc123secret" not in out and "***" in out


def test_payload_mode(telemetry_on):
    from app.config import PayloadMode

    telemetry._config.store_payloads = PayloadMode.NONE
    telemetry.emit_llm_call(call_ref="a", purpose="brief", prompt_text="SECRET BODY")
    telemetry._config.store_payloads = PayloadMode.ALL
    telemetry.emit_llm_call(call_ref="b", purpose="brief", prompt_text="BODY")
    rows = {r.call_ref: r for r in _llm_calls(telemetry_on)}
    assert rows["a"].payload is None
    assert rows["b"].payload and "BODY" in rows["b"].payload["prompt"]
    telemetry._config.store_payloads = PayloadMode.FAILURES


# --- queries ---------------------------------------------------------------------


def test_story_trace_orders_events(telemetry_on):
    t0 = datetime.now(UTC)
    telemetry.emit_stage("enrich", story_id=9, started_at=t0, finished_at=t0)
    telemetry.emit_stage(
        "brief",
        story_id=9,
        started_at=t0 + timedelta(seconds=1),
        finished_at=t0 + timedelta(seconds=2),
    )
    _stages(telemetry_on)
    from app.telemetry import queries

    with telemetry_on() as session:
        trace = queries.story_trace(session, 9)
    assert [e.stage for e in trace["events"]] == ["enrich", "brief"]


def test_summary_shape(telemetry_on):
    telemetry.emit_llm_call(call_ref="s", purpose="brief", latency_ms=1000, completion_tokens=10)
    _llm_calls(telemetry_on)
    from app.telemetry import queries

    with telemetry_on() as session:
        data = queries.telemetry_summary(session, hours=24)
    assert data["llm"]["calls"] >= 1 and data["funnel"]


# --- pages + API + CLI ------------------------------------------------------------


@pytest.fixture
def admin_client(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.settings import Settings

    settings = Settings(
        config_path=ROOT / "config" / "profiles.example.yaml",
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        scheduler_enabled=False,
        admin_key="admin-secret",  # type: ignore[arg-type]
        api_keys="key1",  # type: ignore[arg-type]
    )
    with TestClient(create_app(settings)) as c:
        yield c


def test_workflow_page_requires_admin(admin_client):
    unauth = admin_client.get("/p/personal-reader/settings/workflow", follow_redirects=False)
    assert unauth.status_code == 303 and "/login" in unauth.headers["location"]
    ok = admin_client.get(
        "/p/personal-reader/settings/workflow", headers={"x-api-key": "admin-secret"}
    )
    assert ok.status_code == 200 and "Workflow" in ok.text
    panel = admin_client.get(
        "/p/personal-reader/settings/workflow/panel", headers={"x-api-key": "admin-secret"}
    )
    assert panel.status_code == 200


def test_story_inspector_page(admin_client):
    resp = admin_client.get(
        "/p/personal-reader/settings/workflow/story?q=x", headers={"x-api-key": "admin-secret"}
    )
    assert resp.status_code == 200
    trace = admin_client.get(
        "/p/personal-reader/settings/workflow/story/1", headers={"x-api-key": "admin-secret"}
    )
    assert trace.status_code == 200


def test_admin_api_endpoints(admin_client):
    head = {"x-api-key": "admin-secret"}
    for path in (
        "/api/v1/admin/telemetry/summary",
        "/api/v1/admin/telemetry/llm",
        "/api/v1/admin/telemetry/funnel",
        "/api/v1/admin/telemetry/stories/1/trace",
    ):
        assert admin_client.get(path).status_code in (401, 403)
        assert admin_client.get(path, headers=head).status_code == 200, path
    # reader key is not admin
    assert (
        admin_client.get(
            "/api/v1/admin/telemetry/summary", headers={"x-api-key": "key1"}
        ).status_code
        == 401
    )


def test_report_cli(tmp_path, session_factory):
    from app.telemetry import report

    telemetry.configure(session_factory, config=None)
    telemetry.set_enabled(True)
    telemetry.emit_llm_call(call_ref="c", purpose="brief", latency_ms=5)
    _llm_calls(session_factory)
    assert report.main(["--since", "24h", "--markdown"]) == 0
    assert report.main(["--since", "24h", "--story-id", "1"]) == 0
