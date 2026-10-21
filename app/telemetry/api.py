"""Admin telemetry endpoints under ``/api/v1/admin/telemetry`` (Phase 16).

Admin key only — llm payload previews hold article text and never enter the public API.
Mounted into the v1 router (``app.api_v1``)."""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from app.auth import require_admin
from app.telemetry import queries

router = APIRouter(prefix="/admin/telemetry", tags=["v1-admin"])

_NOW = "2026-10-12T08:30:00Z"


class StageRowV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "stage": "enrich",
                    "waiting": 2,
                    "in_progress": 0,
                    "done": 41,
                    "failed": 1,
                    "median_ms": 12400,
                    "p95_ms": 30100,
                    "oldest_wait_at": _NOW,
                    "top_reason": "timeout",
                }
            ]
        }
    )

    stage: str
    waiting: int = 0
    in_progress: int = 0
    done: int = 0
    failed: int = 0
    median_ms: float | None = None
    p95_ms: float | None = None
    oldest_wait_at: Any = None
    top_reason: str | None = None


class TelemetrySummaryV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "since": _NOW,
                    "hours": 24.0,
                    "app_version": "abc1234",
                    "config_hash": "0a1b2c3d4e5f",
                    "stages": [],
                    "llm": {
                        "calls": 12,
                        "failed": 0,
                        "invalid": 1,
                        "busy_ms": 40000,
                        "queue_wait_ms": 300,
                        "utilization": 0.0004,
                        "prompt_tokens": 9000,
                        "completion_tokens": 1200,
                        "categories": {"summarization": 10, "interpretation": 2},
                        "versions": ["abc1234"],
                        "config_hashes": ["0a1b2c3d4e5f"],
                    },
                    "funnel": [{"name": "stories formed", "count": 4, "rate": 0.5}],
                    "insights": ["Most LLM time goes to brief."],
                }
            ]
        }
    )

    since: str
    hours: float
    app_version: str = ""
    config_hash: str = ""
    stages: list[dict] = []
    llm: dict = {}
    funnel: list[dict] = []
    insights: list[str] = []


class LlmStatsV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "since": _NOW,
                    "hours": 24.0,
                    "calls": 12,
                    "failed": 0,
                    "invalid": 1,
                    "busy_ms": 40000,
                    "queue_wait_ms": 300,
                    "utilization": 0.0004,
                    "prompt_tokens": 9000,
                    "completion_tokens": 1200,
                    "categories": {"summarization": 10},
                    "purposes": [
                        {
                            "purpose": "brief",
                            "category": "summarization",
                            "calls": 10,
                            "errors": 0,
                            "invalid": 1,
                            "latency_ms": 40000,
                            "queue_wait_ms": 300,
                            "prompt_tokens": 9000,
                            "completion_tokens": 1200,
                            "median_ms": 4000,
                            "p95_ms": 8100,
                            "tps": 30.0,
                        }
                    ],
                    "versions": ["abc1234"],
                    "config_hashes": ["0a1b2c3d4e5f"],
                    "slowest": [],
                }
            ]
        }
    )

    since: str
    hours: float
    calls: int = 0
    failed: int = 0
    invalid: int = 0
    busy_ms: int = 0
    queue_wait_ms: int = 0
    utilization: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    categories: dict = {}
    purposes: list[dict] = []
    versions: list[str] = []
    config_hashes: list[str] = []
    slowest: list[dict] = []


class FunnelV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "since": _NOW,
                    "hours": 168.0,
                    "steps": [
                        {"name": "articles ingested", "count": 60, "rate": None},
                        {"name": "stories formed", "count": 9, "rate": 0.15},
                    ],
                    "failure_reasons": {"enrich": [["timeout", 2]]},
                    "retries_per_story": {"1": 6, "2": 2},
                }
            ]
        }
    )

    since: str
    hours: float
    steps: list[dict] = []
    failure_reasons: dict = {}
    retries_per_story: dict = {}


class StoryTraceV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "story_id": 3,
                    "active_llm_ms": 12300,
                    "llm_wait_ms": 400,
                    "stage_ms": 30000,
                    "wall_ms": 420000,
                    "events": [
                        {
                            "stage": "enrich",
                            "started_at": _NOW,
                            "finished_at": _NOW,
                            "status": "ok",
                            "detail": {"added": 2},
                        }
                    ],
                    "calls": [
                        {
                            "purpose": "brief",
                            "attempt": 1,
                            "model": "qwen3.5-9b",
                            "started_at": _NOW,
                            "latency_ms": 4000,
                            "queue_wait_ms": 50,
                            "status": "ok",
                            "validation_error": None,
                        }
                    ],
                    "jobs": [],
                }
            ]
        }
    )

    story_id: int
    active_llm_ms: int = 0
    llm_wait_ms: int = 0
    stage_ms: int = 0
    wall_ms: int | None = None
    events: list[dict] = []
    calls: list[dict] = []
    jobs: list[dict] = []


def _session(request: Request):
    return request.app.state.session_factory


def _dump(row, fields: tuple[str, ...]) -> dict:
    out = {}
    for f in fields:
        v = getattr(row, f)
        out[f] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


_EVENT_FIELDS = (
    "stage",
    "started_at",
    "finished_at",
    "status",
    "detail",
    "article_id",
    "profile",
)
_CALL_FIELDS = (
    "purpose",
    "category",
    "attempt",
    "model",
    "role",
    "started_at",
    "finished_at",
    "latency_ms",
    "queue_wait_ms",
    "prompt_tokens",
    "completion_tokens",
    "status",
    "error_kind",
    "validation_error",
    "article_id",
)


@router.get("/summary", response_model=TelemetrySummaryV1)
def telemetry_summary(request: Request, hours: float = Query(24, ge=0.1, le=720)) -> dict:
    """System summary: stages, LLM totals and funnel for the window (admin key)."""
    with _session(request)() as session:
        return queries.telemetry_summary(session, hours=hours)


@router.get("/llm", response_model=LlmStatsV1)
def telemetry_llm(request: Request, hours: float = Query(24, ge=0.1, le=720)) -> dict:
    """LLM usage: calls, tokens, queue wait, per-purpose table, slowest (admin key)."""
    with _session(request)() as session:
        return queries.llm_stats(session, hours=hours)


@router.get("/funnel", response_model=FunnelV1)
def telemetry_funnel(request: Request, hours: float = Query(168, ge=0.1, le=2160)) -> dict:
    """The ingest -> publish funnel with failure reasons and retries (admin key)."""
    with _session(request)() as session:
        return queries.funnel(session, hours=hours)


@router.get("/stories/{story_id}/trace", response_model=StoryTraceV1)
def telemetry_trace(request: Request, story_id: int) -> dict:
    """One story's full trace: stage events and LLM calls (admin key)."""
    with _session(request)() as session:
        trace = queries.story_trace(session, story_id)
    return {
        "story_id": story_id,
        "active_llm_ms": trace["active_llm_ms"],
        "llm_wait_ms": trace["llm_wait_ms"],
        "stage_ms": trace["stage_ms"],
        "wall_ms": trace["wall_ms"],
        "events": [_dump(e, _EVENT_FIELDS) for e in trace["events"]],
        "calls": [_dump(c, _CALL_FIELDS) for c in trace["calls"]],
        "jobs": [
            {"kind": j.kind, "attempts": j.attempts, "last_error": j.last_error}
            for j in trace["jobs"]
        ],
    }


def mount(v1_router: APIRouter) -> None:
    """Include into the v1 router so the admin key (``require_admin``) stacks on the
    router-level ``require_api_key``."""
    for r in router.routes:
        r.dependencies = [*r.dependencies, Depends(require_admin)]
    v1_router.include_router(router)
