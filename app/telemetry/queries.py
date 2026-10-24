"""Read side of workflow telemetry: aggregate queries over ``llm_calls`` /
``stage_events`` plus the domain queues (summary_jobs, story state columns).

Everything here is read-only and used by Settings > Workflow, the admin API and the
``telemetry.report`` CLI. Results are cached for ``CACHE_SECONDS`` so HTMX polling does
not re-run the aggregates on every refresh."""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app import telemetry
from app.models import (
    Article,
    ContextCandidate,
    LlmCall,
    StageEvent,
    Story,
    StorySummary,
    SummaryJob,
)

CACHE_SECONDS = 5.0
"""The panel polls every 5-10 s; cache aggregates so SQLite stays fast under writes."""

_cache: dict[tuple, tuple[float, Any]] = {}


def _cached(key, produce):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = produce()
    _cache[key] = (time.monotonic(), value)
    return value


def _window(session, table, hours: float, **extra):
    since = datetime.now(UTC) - timedelta(hours=hours)
    stmt = select(table).where(table.started_at >= since)
    for k, v in extra.items():
        stmt = stmt.where(getattr(table, k) == v)
    return list(session.scalars(stmt))


def _durations(rows) -> list[float]:
    return [
        (r.finished_at - r.started_at).total_seconds() * 1000
        for r in rows
        if r.finished_at and r.started_at and r.status != "running"
    ]


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(q * len(ordered)))
    return ordered[idx]


def _top_reason(rows) -> str | None:
    counts: dict[str, int] = {}
    for r in rows:
        reason = (r.detail or {}).get("reason") or (r.detail or {}).get("error")
        if telemetry.canonical_status(r.status) == "failed" and reason:
            counts[str(reason)[:120]] = counts.get(str(reason)[:120], 0) + 1
    return max(counts, key=counts.get) if counts else None


def _reason_counts(rows) -> list[tuple[str, int]]:
    """Per-reason counts over a stage's finished events: ``detail.reason`` (the
    run-level outcome — all_rejected, all_urls_seen...) plus the nested
    breakdowns ``detail.rejects`` (context) and ``detail.reasons`` (images)."""
    from collections import Counter

    counts: Counter[str] = Counter()
    for r in rows:
        if r.status == "running":
            continue
        detail = r.detail or {}
        reason = detail.get("reason")
        if reason:
            counts[str(reason)[:120]] += 1
        limiter = detail.get("limiter")
        if limiter:
            counts[f"limiter:{str(limiter)[:100]}"] += 1
        cache_hits = detail.get("cache_hits")
        if cache_hits:
            # e.g. images reused an existing ImageAsset row — a hit, not a failure
            counts["cache_hit"] += int(cache_hits)
        for key in ("rejects", "reasons"):
            nested = detail.get(key)
            if isinstance(nested, dict):
                for k, n in nested.items():
                    counts[str(k)[:120]] += n or 0
    return counts.most_common(6)


# --- flow overview --------------------------------------------------------------------------


# Domain queues feeding each stage: ``waiting`` comes from the work tables, not events.
def _waiting(session) -> dict[str, dict[str, Any]]:
    now = datetime.now(UTC)
    out: dict[str, dict[str, Any]] = {}

    due = func.coalesce(SummaryJob.next_attempt_at, Story.clustered_at, now)
    jobs = list(
        session.execute(
            select(SummaryJob.kind, func.count(), func.min(due))
            .select_from(SummaryJob)
            .join(Story, Story.id == SummaryJob.story_id)
            .group_by(SummaryJob.kind)
        )
    )
    for kind, count, oldest in jobs:
        out.setdefault(kind, {"count": 0, "oldest_at": None})
        out[kind]["count"] += count
        out[kind]["oldest_at"] = min(filter(None, [out[kind]["oldest_at"], oldest]), default=oldest)

    for status_col, stage in (
        (Story.enrich_status, "enrich"),
        (Story.context_status, "context"),
    ):
        rows = session.execute(
            select(status_col, func.count(), func.min(Story.first_seen_at))
            .where(status_col.in_(("pending", "retry")))
            .group_by(status_col)
        ).all()
        total = sum(r[1] for r in rows)
        oldest = min((r[2] for r in rows if r[2]), default=None)
        if total:
            out.setdefault(stage, {"count": 0, "oldest_at": None})
            out[stage]["count"] += total
            if oldest and (out[stage]["oldest_at"] is None or oldest < out[stage]["oldest_at"]):
                out[stage]["oldest_at"] = oldest

    extracted = session.execute(
        select(func.count(), func.min(Article.published_at)).where(Article.text_status == "pending")
    ).one()
    if extracted and extracted[0]:
        out.setdefault("extract", {"count": 0, "oldest_at": None})
        out["extract"]["count"] += extracted[0]
        out["extract"]["oldest_at"] = extracted[1]
    return out


def flow_overview(session, *, hours: float = 24) -> list[dict[str, Any]]:
    """One row per pipeline node: waiting, in-progress, done/failed in the window,
    median/p95 duration, oldest waiting and the top failure reason."""

    def build():
        rows = _window(session, StageEvent, hours)
        waiting = _waiting(session)
        by_stage: dict[str, list[StageEvent]] = {}
        for r in rows:
            by_stage.setdefault(r.stage, []).append(r)
        stages = sorted(set(by_stage) | set(waiting))
        result = []
        for name in stages:
            evs = by_stage.get(name, [])
            done = [e for e in evs if telemetry.canonical_status(e.status) in ("ok", "skipped")]
            failed = [e for e in evs if telemetry.canonical_status(e.status) == "failed"]
            running = [e for e in evs if e.status == "running"]
            ds = _durations(evs)
            wait = waiting.get(name, {"count": 0, "oldest_at": None})
            result.append(
                {
                    "stage": name,
                    "waiting": wait["count"],
                    "in_progress": len(running),
                    "done": len(done),
                    "failed": len(failed),
                    "median_ms": _percentile(ds, 0.5),
                    "p95_ms": _percentile(ds, 0.95),
                    "oldest_wait_at": wait["oldest_at"],
                    "top_reason": _top_reason(evs),
                    "reasons": _reason_counts(evs),
                }
            )
        order = {
            s: i
            for i, s in enumerate(
                (
                    "ingest",
                    "extract",
                    "cluster",
                    "enrich",
                    "brief",
                    "report",
                    "followups",
                    "perspectives",
                    "context",
                    "publish_gate",
                    "images",
                    "alerts",
                    "regenerate",
                )
            )
        }
        result.sort(key=lambda r: order.get(r["stage"], 99))
        return result

    return _cached(("flow", hours), build)


# --- pipeline visualizer --------------------------------------------------------------------

PIPELINE_STAGES = (
    "ingest",
    "extract",
    "cluster",
    "enrich",
    "brief",
    "report",
    "followups",
    "perspectives",
    "context",
    "publish_gate",
    "images",
    "alerts",
)
"""The strip order for the visualizer; unknown stages trail at the end."""

RECENT_EVENTS = 30


def pipeline_viz(session, *, hours: float = 24) -> dict[str, Any]:
    """Live strip data: one node per pipeline stage (fixed order, even with no
    events) plus the last ``RECENT_EVENTS`` stage events, running first."""

    def build():
        flow = {row["stage"]: row for row in flow_overview(session, hours=hours)}
        known = list(PIPELINE_STAGES)
        extras = sorted(set(flow) - set(known))

        def node(name: str) -> dict[str, Any]:
            row = flow.get(name)
            if row is None:
                row = {
                    "stage": name,
                    "waiting": 0,
                    "in_progress": 0,
                    "done": 0,
                    "failed": 0,
                    "median_ms": None,
                    "p95_ms": None,
                    "oldest_wait_at": None,
                    "top_reason": None,
                    "reasons": [],
                }
            if row["failed"]:
                state = "failed"
            elif row["in_progress"]:
                state = "running"
            elif row["waiting"]:
                state = "stalled"
            elif row["done"]:
                state = "ok"
            else:
                state = "idle"
            return {**row, "state": state}

        stages = [node(n) for n in known + extras]
        events = list(
            session.scalars(
                select(StageEvent)
                .order_by(
                    (StageEvent.status == "running").desc(),
                    StageEvent.started_at.desc(),
                )
                .limit(RECENT_EVENTS)
            )
        )
        recent = []
        for e in events:
            detail = e.detail or {}
            duration_ms = None
            if e.finished_at and e.started_at:
                duration_ms = int((e.finished_at - e.started_at).total_seconds() * 1000)
            recent.append(
                {
                    "stage": e.stage,
                    "status": telemetry.canonical_status(e.status),
                    "started_at": e.started_at,
                    "finished_at": e.finished_at,
                    "duration_ms": duration_ms,
                    "story_id": e.story_id,
                    "reason": detail.get("reason") or detail.get("error"),
                }
            )
        return {"stages": stages, "recent": recent, "now": datetime.now(UTC)}

    return _cached(("viz", hours), build)


# --- LLM usage ------------------------------------------------------------------------------


def llm_stats(session, *, hours: float = 24) -> dict[str, Any]:
    """Calls, tokens, failures, queue wait and busy time for one window."""

    def build():
        rows = _window(session, LlmCall, hours)
        since = datetime.now(UTC) - timedelta(hours=hours)
        total = len(rows)
        finished = [r for r in rows if r.status != "running"]
        busy_ms = sum(r.latency_ms or 0 for r in finished)
        queue_ms = sum(r.queue_wait_ms or 0 for r in finished)
        span_ms = hours * 3600 * 1000
        per_purpose: dict[str, dict[str, Any]] = {}
        per_category: dict[str, int] = {}
        versions: set[str] = set()
        config_hashes: set[str] = set()
        for r in finished:
            p = per_purpose.setdefault(
                r.purpose,
                {
                    "purpose": r.purpose,
                    "category": r.category,
                    "calls": 0,
                    "errors": 0,
                    "invalid": 0,
                    "latency_ms": 0,
                    "queue_wait_ms": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "latencies": [],
                },
            )
            p["calls"] += 1
            p["errors"] += int(r.status == "error" or r.status == "timeout")
            p["invalid"] += int(r.status == "invalid")
            p["latency_ms"] += r.latency_ms or 0
            p["queue_wait_ms"] += r.queue_wait_ms or 0
            p["prompt_tokens"] += r.prompt_tokens or 0
            p["completion_tokens"] += r.completion_tokens or 0
            if r.latency_ms:
                p["latencies"].append(r.latency_ms)
            per_category[r.category] = per_category.get(r.category, 0) + 1
            if r.app_version:
                versions.add(r.app_version)
            if r.config_hash:
                config_hashes.add(r.config_hash)
        purposes = []
        for p in per_purpose.values():
            lats = p.pop("latencies")
            p["median_ms"] = _percentile(lats, 0.5)
            p["p95_ms"] = _percentile(lats, 0.95)
            p["tps"] = (
                round(p["completion_tokens"] / (p["latency_ms"] / 1000), 1)
                if p["latency_ms"]
                else None
            )
            purposes.append(p)
        purposes.sort(key=lambda p: p["latency_ms"], reverse=True)
        slowest = sorted(
            (r for r in finished if r.latency_ms), key=lambda r: r.latency_ms, reverse=True
        )[:10]
        failed = sum(1 for r in finished if r.status in ("error", "timeout"))
        return {
            "since": since.isoformat(),
            "hours": hours,
            "calls": total,
            "failed": failed,
            "invalid": sum(1 for r in finished if r.status == "invalid"),
            "busy_ms": busy_ms,
            "queue_wait_ms": queue_ms,
            "utilization": round(busy_ms / span_ms, 4) if span_ms else 0.0,
            "prompt_tokens": sum(r.prompt_tokens or 0 for r in finished),
            "completion_tokens": sum(r.completion_tokens or 0 for r in finished),
            "categories": per_category,
            "purposes": purposes,
            "versions": sorted(versions),
            "config_hashes": sorted(config_hashes),
            "slowest": [
                {
                    "story_id": r.story_id,
                    "article_id": r.article_id,
                    "purpose": r.purpose,
                    "latency_ms": r.latency_ms,
                    "queue_wait_ms": r.queue_wait_ms,
                    "completion_tokens": r.completion_tokens,
                    "status": r.status,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                }
                for r in slowest
            ],
        }

    return _cached(("llm", hours), build)


# --- funnel ---------------------------------------------------------------------------------


def funnel(session, *, hours: float = 24 * 7) -> dict[str, Any]:
    """articles -> extracted -> stories -> 2+ publishers -> enrichment >=1 source ->
    briefs -> reports -> context, with per-step failure reasons and retries."""

    def build():
        since = datetime.now(UTC) - timedelta(hours=hours)
        articles = (
            session.scalar(
                select(func.count()).select_from(Article).where(Article.fetched_at >= since)
            )
            or 0
        )
        extracted = (
            session.scalar(
                select(func.count())
                .select_from(Article)
                .where(Article.fetched_at >= since, Article.text_status == "ok")
            )
            or 0
        )
        stories = (
            session.scalar(
                select(func.count()).select_from(Story).where(Story.first_seen_at >= since)
            )
            or 0
        )
        multi = (
            session.scalar(
                select(func.count())
                .select_from(Story)
                .where(Story.first_seen_at >= since, Story.source_count >= 2)
            )
            or 0
        )
        enriched = (
            session.scalar(
                select(func.count())
                .select_from(Story)
                .where(Story.first_seen_at >= since, Story.enrich_added >= 1)
            )
            or 0
        )
        briefs = (
            session.scalar(
                select(func.count(func.distinct(StorySummary.story_id))).where(
                    StorySummary.kind == "brief",
                    StorySummary.status == "ok",
                    StorySummary.created_at >= since,
                )
            )
            or 0
        )
        reports = (
            session.scalar(
                select(func.count(func.distinct(StorySummary.story_id))).where(
                    StorySummary.kind == "report",
                    StorySummary.status == "ok",
                    StorySummary.created_at >= since,
                )
            )
            or 0
        )
        contexted = (
            session.scalar(
                select(func.count())
                .select_from(Story)
                .where(Story.first_seen_at >= since, Story.context_status == "done")
            )
            or 0
        )
        steps = [
            ("articles ingested", articles),
            ("text extracted", extracted),
            ("stories formed", stories),
            ("stories with 2+ publishers", multi),
            ("enrichment added >= 1 source", enriched),
            ("briefs generated", briefs),
            ("reports generated", reports),
            ("context attached", contexted),
        ]

        events = _window(session, StageEvent, hours)
        reasons: dict[str, dict[str, int]] = {}
        for e in events:
            reason = (e.detail or {}).get("reason") or (e.detail or {}).get("error")
            if reason:
                bucket = reasons.setdefault(e.stage, {})
                key = str(reason)[:120]
                bucket[key] = bucket.get(key, 0) + 1
        ranked = {
            stage: sorted(rs.items(), key=lambda kv: kv[1], reverse=True)[:5]
            for stage, rs in reasons.items()
        }

        retries = session.execute(
            select(StageEvent.story_id, func.count())
            .where(StageEvent.stage.in_(("brief", "report")), StageEvent.started_at >= since)
            .group_by(StageEvent.story_id)
        ).all()
        dist: dict[int, int] = {}
        for _sid, count in retries:
            bucket = min(count, 4)
            dist[bucket] = dist.get(bucket, 0) + 1
        rates = {}
        prev = None
        for name, n in steps:
            rates[name] = round(n / prev, 2) if prev else None
            prev = n or None
        return {
            "since": since.isoformat(),
            "hours": hours,
            "steps": [{"name": n, "count": c, "rate": rates[n]} for n, c in steps],
            "failure_reasons": ranked,
            "retries_per_story": {str(k): v for k, v in sorted(dist.items())},
        }

    return _cached(("funnel", hours), build)


def data_span(session, *, hours: float = 24) -> dict[str, Any]:
    """How much of the requested window actually has telemetry rows — the Workflow
    table's label shows the real data span, not just the ``hours`` parameter."""

    def build():
        now = datetime.now(UTC)
        since = now - timedelta(hours=hours)
        oldest = min(
            (
                v
                for v in (
                    session.scalar(
                        select(func.min(StageEvent.started_at)).where(
                            StageEvent.started_at >= since
                        )
                    ),
                    session.scalar(
                        select(func.min(LlmCall.started_at)).where(LlmCall.started_at >= since)
                    ),
                )
                if v
            ),
            default=None,
        )
        if oldest is None:
            label = f"last {_fmt_span(hours)} (no events)"
            return {"hours": hours, "oldest_at": None, "label": label}
        span_h = (now - oldest).total_seconds() / 3600
        if span_h >= hours * 0.95:
            label = f"last {_fmt_span(hours)}"
        else:
            label = f"data spans {_fmt_span(span_h)} of {_fmt_span(hours)} window"
        return {"hours": hours, "oldest_at": oldest, "label": label}

    return _cached(("span", hours), build)


def _fmt_span(hours: float) -> str:
    if hours >= 48:
        return f"{hours / 24:g}d"
    if hours >= 1:
        return f"{hours:g}h"
    return f"{int(hours * 60)}m"


# --- story trace ----------------------------------------------------------------------------


def story_trace(session, story_id: int) -> dict[str, Any]:
    """Everything recorded for one story: stage events and LLM calls, oldest first."""
    events = list(
        session.scalars(
            select(StageEvent)
            .where(StageEvent.story_id == story_id)
            .order_by(StageEvent.started_at, StageEvent.id)
        )
    )
    calls = list(
        session.scalars(
            select(LlmCall)
            .where(LlmCall.story_id == story_id)
            .order_by(LlmCall.started_at, LlmCall.id)
        )
    )
    articles = list(
        session.scalars(
            select(Article).where(Article.story_id == story_id).order_by(Article.published_at)
        )
    )
    jobs = list(session.scalars(select(SummaryJob).where(SummaryJob.story_id == story_id)))
    story = session.get(Story, story_id)
    for e in events:
        # display value: canonical for rows written before the status normalization
        e.canonical = telemetry.canonical_status(e.status)

    first_at = None
    last_at = None
    for r in [*events, *calls]:
        if r.started_at and (first_at is None or r.started_at < first_at):
            first_at = r.started_at
        end = r.finished_at or r.started_at
        if end and (last_at is None or end > last_at):
            last_at = end
    active_ms = sum(c.latency_ms or 0 for c in calls)
    waiting_ms = sum(c.queue_wait_ms or 0 for c in calls)
    stage_ms = sum(int((e.detail or {}).get("duration_ms") or 0) for e in events if e.finished_at)
    wall_ms = None
    if first_at and last_at:
        wall_ms = int((last_at - first_at).total_seconds() * 1000)
    context_rejects = dict(
        session.execute(
            select(ContextCandidate.reason, func.count())
            .where(
                ContextCandidate.story_id == story_id,
                ContextCandidate.decision == "rejected",
                ContextCandidate.reason != "",
            )
            .group_by(ContextCandidate.reason)
        ).all()
    )
    return {
        "story": story,
        "events": events,
        "calls": calls,
        "articles": articles,
        "jobs": jobs,
        "context_rejects": context_rejects,
        "active_llm_ms": active_ms,
        "llm_wait_ms": waiting_ms,
        "stage_ms": stage_ms,
        "wall_ms": wall_ms,
        "trace_start": first_at,
        "trace_end": last_at,
    }


# --- panel summary (the old Pipeline panel data, kept working) -------------------------------


def telemetry_summary(session, *, hours: float = 24) -> dict[str, Any]:
    """Compact summary for the admin API and the markdown report."""
    flow = flow_overview(session, hours=hours)
    llm = llm_stats(session, hours=hours)
    fun = funnel(session, hours=hours)
    return {
        "since": (datetime.now(UTC) - timedelta(hours=hours)).isoformat(),
        "hours": hours,
        "app_version": next(iter(llm["versions"]), ""),
        "config_hash": next(iter(llm["config_hashes"]), ""),
        "stages": flow,
        "llm": {k: v for k, v in llm.items() if k not in ("purposes", "slowest")},
        "funnel": fun["steps"],
        "insights": insights(session, hours=hours),
    }


def insights(session, *, hours: float = 24) -> list[str]:
    """Rule-based one-liners: where time goes and what looks wrong."""
    out: list[str] = []
    llm = llm_stats(session, hours=hours)
    flow = flow_overview(session, hours=hours)
    if llm["calls"]:
        top = max(
            (p for p in llm["purposes"] if p["calls"]),
            key=lambda p: p["latency_ms"],
            default=None,
        )
        if top and llm["busy_ms"]:
            share = top["latency_ms"] / max(llm["busy_ms"], 1)
            out.append(
                f"Most LLM time goes to {top['purpose']} ({share:.0%} of active time, "
                f"{top['calls']} calls, median {int(top['median_ms'] or 0)} ms)."
            )
        if llm["queue_wait_ms"] > llm["busy_ms"]:
            out.append(
                "Calls spend more time waiting for the model slot than running "
                f"({llm['queue_wait_ms'] // 1000}s queued vs {llm['busy_ms'] // 1000}s busy)."
            )
        invalids = [p for p in llm["purposes"] if p["invalid"]]
        for p in invalids:
            out.append(f"{p['purpose']}: {p['invalid']} of {p['calls']} calls failed validation.")
    for s in flow:
        if s["failed"] and s["top_reason"]:
            out.append(f"{s['stage']}: {s['failed']} failures — {s['top_reason']}")
        if s["waiting"] >= 5 and s["oldest_wait_at"]:
            out.append(f"{s['stage']}: {s['waiting']} waiting, oldest from {s['oldest_wait_at']}")
    return out[:8]


# --- story step log -------------------------------------------------------------------------

LOG_LIMIT = 500
GAP_PROBLEM_MS = 30_000
"""``show="problems"`` also keeps entries that idled at least this long before starting."""

_PROBLEM_STATUSES = {"failed", "invalid", "timeout", "skipped"}
_RETRY_OUTCOMES = {"retried", "gave_up", "ok_after_retry"}


def _entry(
    *,
    ts: datetime | None,
    kind: str,
    name: str,
    status: str | None = None,
    duration_ms: int | None = None,
    tokens: dict[str, Any] | None = None,
    queue_wait_ms: int | None = None,
    attempt: int | None = None,
    reason: str | None = None,
    detail: dict[str, Any] | None = None,
    seq: int = 0,
    role: str | None = None,
) -> dict[str, Any] | None:
    if ts is None:
        return None
    return {
        "ts": ts,
        "seq": seq,
        "kind": kind,
        "name": name,
        "status": telemetry.canonical_status(status) if kind in ("STAGE", "STATE") else status,
        "duration_ms": duration_ms,
        "offset_ms": None,
        "gap_ms": None,
        "tokens": tokens or {},
        "queue_wait_ms": queue_wait_ms,
        "attempt": attempt,
        "reason": reason,
        "detail": detail or {},
        "role": role,
        "concurrent": {},
        "source": None,  # internal: (table, id) so a row never counts itself as concurrent
    }


def story_log(session, story_id: int, *, show: str = "all") -> dict[str, Any]:
    """One chronological step log for a story: stage events, LLM calls, db lock events
    (as victim or holder), generation attempts, saved summaries, lifecycle events,
    state timestamps and queued jobs — with offsets, idle gaps, what else ran
    alongside, and a summary block for the inspector strip and the text export."""
    from app.models import DbEvent, GenerationAttempt, StoryEventRow

    story = session.get(Story, story_id)
    stages = list(
        session.scalars(
            select(StageEvent)
            .where(StageEvent.story_id == story_id)
            .order_by(StageEvent.started_at, StageEvent.id)
        )
    )
    calls = list(
        session.scalars(
            select(LlmCall)
            .where(LlmCall.story_id == story_id)
            .order_by(LlmCall.started_at, LlmCall.id)
        )
    )
    db_rows = list(
        session.scalars(
            select(DbEvent)
            .where((DbEvent.story_id == story_id) | (DbEvent.holder_story_id == story_id))
            .order_by(DbEvent.started_at, DbEvent.id)
        )
    )
    attempts = list(
        session.scalars(
            select(GenerationAttempt)
            .where(GenerationAttempt.story_id == story_id)
            .order_by(GenerationAttempt.created_at, GenerationAttempt.id)
        )
    )
    summaries = list(
        session.scalars(
            select(StorySummary)
            .where(StorySummary.story_id == story_id)
            .order_by(StorySummary.created_at, StorySummary.id)
        )
    )
    lifecycle = list(
        session.scalars(
            select(StoryEventRow)
            .where(StoryEventRow.story_id == story_id)
            .order_by(StoryEventRow.created_at, StoryEventRow.id)
        )
    )
    jobs = list(session.scalars(select(SummaryJob).where(SummaryJob.story_id == story_id)))

    entries: list[dict[str, Any]] = []

    def put(**kw) -> None:
        row = _entry(**kw)
        if row is not None:
            entries.append(row)

    for e in stages:
        detail = dict(e.detail or {})
        dur = detail.get("duration_ms")
        if dur is None and e.finished_at and e.started_at:
            dur = int((e.finished_at - e.started_at).total_seconds() * 1000)
        status = detail.get("status") or e.status
        kind = "STATE" if e.stage == "publish_gate" else "STAGE"
        put(
            ts=e.started_at,
            seq=e.id,
            kind=kind,
            name=e.stage,
            status=status if e.status != "running" else "running",
            duration_ms=dur,
            reason=detail.get("reason") or detail.get("error"),
            detail=detail,
            role=e.stage if kind == "STATE" else None,
        )
        entries[-1]["source"] = ("stage", e.id)
    for c in calls:
        reason = c.validation_error or c.error_kind
        put(
            ts=c.started_at,
            seq=c.id,
            kind="LLM",
            name=c.purpose,
            status=c.status,
            duration_ms=c.latency_ms,
            tokens={
                "prompt": c.prompt_tokens,
                "completion": c.completion_tokens,
                "tok_per_s": c.tokens_per_second,
                "finish_reason": c.finish_reason,
                "reasoning_chars": c.reasoning_chars,
            },
            queue_wait_ms=c.queue_wait_ms,
            attempt=c.attempt,
            reason=reason,
            detail={
                "model": c.model,
                "role": c.role,
                "run_id": c.run_id,
                "call_ref": c.call_ref,
                "prompt": (c.payload or {}).get("prompt"),
                "response": (c.payload or {}).get("response"),
            },
        )
        entries[-1]["source"] = ("llm", c.id)
    for d in db_rows:
        role = "victim" if d.story_id == story_id else "holder"
        label = d.kind
        if d.op or d.table_name:
            label = f"{d.kind} {d.op or ''} {d.table_name or ''}".strip()
        detail = {
            "table": d.table_name,
            "waited_ms": d.waited_ms,
            "thread": d.thread,
            "caller": d.caller,
            "outcome": d.outcome,
            "holder_thread": d.holder_thread,
            "holder_caller": d.holder_caller,
            "holder_story_id": d.holder_story_id,
            "holder_age_ms": d.holder_age_ms,
            "duration_ms": d.duration_ms,
        }
        status = (
            "failed"
            if d.kind in ("lock_error", "lock_giveup")
            else "ok"
            if d.outcome == "ok_after_retry"
            else "retry"
        )
        put(
            ts=d.started_at,
            seq=d.id,
            kind="DB",
            name=label,
            status=status,
            duration_ms=d.waited_ms or d.duration_ms,
            attempt=d.attempt,
            reason=(f"{role}: waited {d.waited_ms}ms" if d.waited_ms else role),
            detail=detail,
            role=role,
        )
        entries[-1]["source"] = ("db", d.id)
    for a in attempts:
        verr = "; ".join(a.validation_errors or []) or None
        put(
            ts=a.created_at,
            seq=a.id,
            kind="LLM",
            name=f"{a.kind}·try{a.attempt or 1}",
            status="ok" if a.outcome == "ok" else "invalid" if verr else "failed",
            duration_ms=a.latency_ms,
            tokens={
                "prompt": a.prompt_tokens,
                "completion": a.completion_tokens,
                "tok_per_s": (
                    round(a.completion_tokens / (a.latency_ms / 1000), 1)
                    if a.completion_tokens and a.latency_ms
                    else None
                ),
                "finish_reason": a.finish_reason,
                "reasoning_chars": a.reasoning_length,
            },
            attempt=a.attempt,
            reason=a.reason or verr,
            detail={
                "model": a.model,
                "input_words": a.input_words,
                "validation_errors": a.validation_errors or [],
                "warnings": a.warnings or [],
                "run_id": a.run_id,
                "temperature": a.temperature,
                "prompt_kind": a.prompt,
            },
        )
        entries[-1]["source"] = ("attempt", a.id)
    for s in summaries:
        put(
            ts=s.created_at,
            seq=s.id,
            kind="SAVE",
            name=f"{s.kind} v{s.version} saved",
            status=s.status,
            tokens={
                "prompt": s.prompt_tokens,
                "completion": s.completion_tokens,
            },
            reason=s.reason,
            detail={"model": s.model, "basis": s.basis, "latency_ms": s.latency_ms},
        )
        entries[-1]["source"] = ("summary", s.id)
    for ev in lifecycle:
        put(
            ts=ev.created_at,
            seq=ev.id,
            kind="STATE",
            name=ev.type,
            status="ok",
            detail={"version": ev.version, "profiles": ev.profiles},
        )
        entries[-1]["source"] = ("event", ev.id)
    for j in jobs:
        put(
            ts=j.queued_at,
            seq=0,
            kind="STATE",
            name=f"{j.kind} queued",
            status="running",
            attempt=j.attempts or None,
            reason=j.last_error,
            detail={
                "kind": j.kind,
                "attempts": j.attempts,
                "next_attempt_at": j.next_attempt_at.isoformat() if j.next_attempt_at else None,
                "force": j.force,
            },
        )
        entries[-1]["source"] = ("job", j.kind)
    if story is not None:
        for label, ts in (
            ("first_seen", story.first_seen_at),
            ("clustered", story.clustered_at),
            ("enrich_started", story.enrich_started_at),
            ("enriched", story.enriched_at),
            ("brief_ready", story.brief_ready_at),
            ("published", story.published_at),
        ):
            put(ts=ts, seq=0, kind="STATE", name=label, status="ok")
            if entries and entries[-1]["name"] == label:
                entries[-1]["source"] = ("story", label)

    entries.sort(key=lambda e: (e["ts"], e["seq"]))
    first_ts = entries[0]["ts"] if entries else None
    last_end = None
    max_end = None
    for e in entries:
        e["offset_ms"] = int((e["ts"] - first_ts).total_seconds() * 1000)
        if max_end is not None and e["ts"] > max_end:
            e["gap_ms"] = int((e["ts"] - max_end).total_seconds() * 1000)
        end = e["ts"]
        if e["duration_ms"]:
            end = e["ts"] + timedelta(milliseconds=e["duration_ms"])
        last_end = end if last_end is None else max(last_end, end)
        max_end = end if max_end is None else max(max_end, end)

    # Concurrency: every stage event / llm call of any story overlapping the window,
    # counted per timed entry (its own row excluded).
    concurrent_rows: list[tuple[str, str, datetime, datetime]] = []
    if first_ts and last_end:
        for e in session.scalars(
            select(StageEvent).where(
                StageEvent.started_at <= last_end, StageEvent.started_at >= first_ts
            )
        ):
            end = e.finished_at or e.started_at
            concurrent_rows.append((f"stage:{e.id}", e.stage, e.started_at, end))
        for c in session.scalars(
            select(LlmCall).where(LlmCall.started_at <= last_end, LlmCall.started_at >= first_ts)
        ):
            start = c.started_at
            end = c.finished_at or (
                start + timedelta(milliseconds=c.latency_ms) if c.latency_ms else start
            )
            concurrent_rows.append((f"llm:{c.id}", c.purpose, start, end))
    for e in entries:
        if not e["duration_ms"]:
            continue
        start = e["ts"]
        end = start + timedelta(milliseconds=e["duration_ms"])
        counts: dict[str, int] = {}
        own = f"{e['source'][0]}:{e['source'][1]}" if e.get("source") else ""
        for src, name, s, f in concurrent_rows:
            if src == own:
                continue
            if s <= end and f >= start:
                counts[name] = counts.get(name, 0) + 1
        e["concurrent"] = counts

    def _is_problem(e: dict[str, Any]) -> bool:
        if e["kind"] == "DB":
            return True
        if e["status"] in _PROBLEM_STATUSES:
            return True
        if (e["attempt"] or 0) > 1 or (e["detail"].get("outcome") in _RETRY_OUTCOMES):
            return True
        return bool(e["gap_ms"] and e["gap_ms"] > GAP_PROBLEM_MS)

    kept = entries
    if show == "problems":
        kept = [e for e in entries if _is_problem(e)]
    elif show == "llm":
        kept = [e for e in entries if e["kind"] == "LLM"]
    truncated = len(kept) > LOG_LIMIT
    kept = kept[:LOG_LIMIT]
    for i, e in enumerate(kept):
        e["n"] = i + 1

    # --- summary ----------------------------------------------------------------------------
    llm_entries = [
        e for e in entries if e["kind"] == "LLM" and e["source"] and e["source"][0] == "llm"
    ]
    work_starts = [e["ts"] for e in entries if e["kind"] in ("STAGE", "LLM", "DB", "SAVE")]
    queued_at = min(
        (e["ts"] for e in entries if e["kind"] == "STATE" and e["name"].endswith("queued")),
        default=None,
    )
    published_at = story.published_at if story else None
    lock_kinds: dict[str, int] = {}
    lock_wait = 0
    longest_holder = None
    for d in db_rows:
        lock_kinds[d.kind] = lock_kinds.get(d.kind, 0) + 1
        lock_wait += d.waited_ms or 0
        if d.holder_age_ms and (
            longest_holder is None or d.holder_age_ms > longest_holder["age_ms"]
        ):
            longest_holder = {"caller": d.holder_caller, "age_ms": d.holder_age_ms}
    attempts_per_kind: dict[str, int] = {}
    for a in attempts:
        attempts_per_kind[a.kind] = attempts_per_kind.get(a.kind, 0) + 1
    invalid = sum(1 for e in llm_entries if e["status"] == "invalid")
    failed = sum(1 for e in llm_entries if e["status"] in ("failed", "error", "timeout"))
    reasons: dict[str, int] = {}
    for a in attempts:
        for v in a.validation_errors or []:
            key = str(v)[:80]
            reasons[key] = reasons.get(key, 0) + 1
    top_reason = max(reasons, key=reasons.get) if reasons else None
    prompt_toks = sum(c.prompt_tokens or 0 for c in calls)
    completion_toks = sum(c.completion_tokens or 0 for c in calls)
    active_ms = sum(c.latency_ms or 0 for c in calls)
    wait_ms = sum(c.queue_wait_ms or 0 for c in calls)
    final = "pending"
    if story is not None:
        if story.state == "published":
            final = "published"
        elif story.state == "merged":
            final = "skipped"
        elif story.state_reason and "failed" in story.state_reason:
            final = "failed"
    summary = {
        "wall_ms": int((last_end - first_ts).total_seconds() * 1000) if first_ts else None,
        "queued_to_first_start_ms": (
            int((min(work_starts) - queued_at).total_seconds() * 1000)
            if queued_at and work_starts
            else None
        ),
        "started_to_published_ms": (
            int((published_at - min(work_starts)).total_seconds() * 1000)
            if published_at and work_starts
            else None
        ),
        "llm_active_ms": active_ms,
        "llm_queue_wait_ms": wait_ms,
        "calls": len(calls),
        "attempts": attempts_per_kind,
        "prompt_tokens": prompt_toks,
        "completion_tokens": completion_toks,
        "tok_per_s": round(completion_toks / (active_ms / 1000), 1) if active_ms else None,
        "invalid": invalid,
        "failed": failed,
        "top_validation_reason": top_reason,
        "lock_events": lock_kinds,
        "total_lock_wait_ms": lock_wait,
        "longest_holder": longest_holder,
        "final_outcome": final,
    }
    # an entry still running, or anything new in the last two minutes -> keep polling
    now = datetime.now(UTC)
    active = any(e["status"] == "running" for e in entries) or bool(
        entries and (now - entries[-1]["ts"]).total_seconds() < 120
    )
    return {
        "story": story,
        "entries": kept,
        "total": len(entries),
        "truncated": truncated,
        "show": show,
        "summary": summary,
        "active": active,
    }


def _fmt_ms(v) -> str:
    if v is None:
        return "-"
    if v >= 60000:
        return f"{v / 60000:.1f}m"
    if v >= 1000:
        return f"{v / 1000:.1f}s"
    return f"{v}ms"


def story_log_text(log: dict[str, Any]) -> str:
    """The step log as a compact fixed-width trace (metadata only — no article text,
    prompts or payloads)."""
    story = log["story"]
    s = log["summary"]
    lines = [
        f"story #{log.get('story_id', story.id if story else '?')} "
        f"{story.title if story else '(gone)'}",
        f"state: {story.state if story else '?'}"
        + (f" ({story.state_reason})" if story and story.state_reason else ""),
        (
            f"wall {_fmt_ms(s['wall_ms'])} · llm {_fmt_ms(s['llm_active_ms'])} active "
            f"+{_fmt_ms(s['llm_queue_wait_ms'])} queued · calls {s['calls']} "
            f"tokens {s['prompt_tokens']}→{s['completion_tokens']}"
            + (f" ({s['tok_per_s']} tok/s)" if s["tok_per_s"] else "")
            + f" · invalid {s['invalid']} failed {s['failed']} · outcome {s['final_outcome']}"
        ),
    ]
    if s["attempts"]:
        lines.append("attempts: " + ", ".join(f"{k}x{n}" for k, n in sorted(s["attempts"].items())))
    if s["lock_events"]:
        holder = s["longest_holder"]
        lines.append(
            "locks: "
            + ", ".join(f"{k}x{n}" for k, n in sorted(s["lock_events"].items()))
            + f" · waited {_fmt_ms(s['total_lock_wait_ms'])}"
            + (
                f" · longest holder {holder['caller']} {_fmt_ms(holder['age_ms'])}"
                if holder
                else ""
            )
        )
    lines.append("")
    for e in log["entries"]:
        t = e["ts"].strftime("%H:%M:%S.") + f"{e['ts'].microsecond // 1000:03d}"
        tokens = e["tokens"]
        tok = ""
        if tokens.get("prompt") is not None or tokens.get("completion") is not None:
            tok = f" {tokens.get('prompt') or '?'}→{tokens.get('completion') or '?'}"
            if tokens.get("tok_per_s"):
                tok += f" {tokens['tok_per_s']}tok/s"
            if tokens.get("finish_reason"):
                tok += f" {tokens['finish_reason']}"
        gap = f" gap+{_fmt_ms(e['gap_ms'])}" if e.get("gap_ms") else ""
        reason = f" {e['reason']}" if e.get("reason") else ""
        side = ""
        if e.get("concurrent"):
            side = " alongside " + ",".join(f"{k}x{n}" for k, n in e["concurrent"].items())
        attempt = f" try{e['attempt']}" if e.get("attempt") else ""
        lines.append(
            f"{t} +{_fmt_ms(e['offset_ms']):>7} {e['kind']:<5} {e['name']}{attempt}"
            f" {_fmt_ms(e['duration_ms'])}{tok} {e['status'] or '-'}{reason}{gap}{side}"
        )
        if e["kind"] == "DB":
            d = e["detail"]
            holder = (
                f" holder={d.get('holder_caller')} age={_fmt_ms(d.get('holder_age_ms'))}"
                if d.get("holder_caller")
                else ""
            )
            outcome = f" {d['outcome']}" if d.get("outcome") else ""
            lines.append(
                f"{'':>12}DB  {e['name']} waited {_fmt_ms(e['duration_ms'])}"
                f" attempt {e['attempt'] or '-'}{outcome}{holder}"
            )
    if log["truncated"]:
        lines.append(f"… truncated to {LOG_LIMIT} of {log['total']} entries")
    return "\n".join(lines)
