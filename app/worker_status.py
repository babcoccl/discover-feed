"""Generation worker run results and status (API/UI models; no summarization code)."""

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel


class SummaryOutcome(BaseModel):
    story_id: int
    kind: str = "brief"
    status: str
    """``ok`` | ``fallback`` | ``failed`` | ``rejected`` | ``cached`` | ``reused`` |
    ``skipped``."""
    reason: str | None = None
    summary_id: int | None = None
    attempts: int = 0
    latency_ms: int | None = None
    completion_tokens: int | None = None


class SummarizeRunResult(BaseModel):
    status: str = "ok"
    """``ok``, ``busy`` (a run is in progress), ``disabled`` (no summarizer), ``paused``."""
    processed: int = 0
    ok: int = 0
    fallback: int = 0
    failed: int = 0
    rejected: int = 0
    """Reports that failed validation (stored as failed)."""
    cached: int = 0
    queued: int = 0
    """Jobs still waiting after this run."""
    outcomes: list[SummaryOutcome] = []


class WorkerStatus(BaseModel):
    enabled: bool
    model: str | None
    running: bool
    queued: int
    due: int
    waiting: int
    """Queued but debounced or backing off after a failure."""
    last_run_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    paused_until: datetime | None
    tokens_per_second: float | None
    """Average generation speed over the last 20 generated briefs/reports."""
    seconds: dict[str, float | None] = {}
    """Median latency (all attempts) of the last 20 generated briefs / reports."""
    summaries: dict[str, int]
    """Stored brief versions by status."""
    reports: dict[str, int]
    """Stored report versions by status."""
    report_mode: str
    queued_reports: int
    paused_by_admin: bool = False
    current_story: int | None = None


@dataclass
class WorkerState:
    running: bool = False
    last_run_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int = 0
    paused_until: datetime | None = None
    paused_by_admin: bool = False
    current_story: int | None = None
