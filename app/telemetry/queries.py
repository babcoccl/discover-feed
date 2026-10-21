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
