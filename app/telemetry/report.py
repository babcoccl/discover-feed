"""``python -m app.telemetry.report`` — a markdown (or plain text) dump of the workflow
telemetry: LLM usage, pipeline flow, funnel and insights for a time window, or one
story's trace with ``--story-id``. Reads the app database directly."""

import argparse
import sys


def _since(spec: str) -> float:
    spec = spec.strip().lower()
    for suffix, mult in (("h", 1.0), ("d", 24.0), ("m", 1 / 60)):
        if spec.endswith(suffix):
            return float(spec[: -len(suffix)]) * mult
    return float(spec)


def _ms(v) -> str:
    if v is None:
        return "—"
    if v >= 60000:
        return f"{v / 60000:.1f}m"
    if v >= 1000:
        return f"{v / 1000:.1f}s"
    return f"{int(v)}ms"


def story_markdown(trace: dict) -> str:
    story = trace["story"]
    out = [
        f"# Story {trace.get('story_id', story.id if story else '?')} trace",
        "",
        f"- Title: {story.title if story else '(gone)'}",
        f"- State: {story.state if story else '?'} {story.state_reason or '' if story else ''}",
        f"- Active LLM: {_ms(trace['active_llm_ms'])} · "
        f"waiting for LLM: {_ms(trace['llm_wait_ms'])}"
        f" · stage work: {_ms(trace['stage_ms'])} · wall: {_ms(trace['wall_ms'])}",
        "",
        "## Stage events",
        "",
        "| time | stage | duration | status | detail |",
        "|---|---|---|---|---|",
    ]
    for e in trace["events"]:
        d = e.detail or {}
        detail = ", ".join(f"{k}={v}" for k, v in d.items() if k != "duration_ms")[:120]
        out.append(
            f"| {e.started_at:%H:%M:%S} | {e.stage} | {_ms(d.get('duration_ms'))} | "
            f"{e.status} | {detail} |"
        )
    out += [
        "",
        "## LLM calls",
        "",
        "| time | purpose | attempt | latency | queue | status |",
        "|---|---|---|---|---|---|",
    ]
    for c in trace["calls"]:
        err = c.validation_error or c.error_kind or ""
        out.append(
            f"| {c.started_at:%H:%M:%S} | {c.purpose} | {c.attempt} | {_ms(c.latency_ms)} | "
            f"{_ms(c.queue_wait_ms)} | {c.status}{f' ({err[:60]})' if err else ''} |"
        )
    return "\n".join(out)


def summary_markdown(data: dict) -> str:
    llm = data["llm"]
    out = [
        f"# Workflow report — last {data['hours']:g}h",
        "",
        f"Version `{data['app_version']}` · config `{data['config_hash']}`",
        "",
        "## LLM usage",
        "",
        f"- {llm['calls']} calls, {llm['failed']} failed, {llm['invalid']} validation rejects",
        f"- LLM busy {_ms(llm['busy_ms'])}, queued {_ms(llm['queue_wait_ms'])}, "
        f"utilization {llm['utilization']:.1%}",
        f"- tokens: {llm['prompt_tokens']} in / {llm['completion_tokens']} out",
        "",
        "| purpose | calls | err | invalid | median | p95 | queue wait |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in llm["purposes"]:
        out.append(
            f"| {p['purpose']} | {p['calls']} | {p['errors']} | {p['invalid']} | "
            f"{_ms(p['median_ms'])} | {_ms(p['p95_ms'])} | {_ms(p['queue_wait_ms'])} |"
        )
    out += [
        "",
        "## Pipeline flow",
        "",
        "| stage | waiting | running | done | failed | median | p95 | top failure |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in data["stages"]:
        out.append(
            f"| {s['stage']} | {s['waiting']} | {s['in_progress']} | {s['done']} | "
            f"{s['failed']} | {_ms(s['median_ms'])} | {_ms(s['p95_ms'])} | "
            f"{(s['top_reason'] or '—')[:60]} |"
        )
    out += ["", "## Funnel", "", "| step | count | rate |", "|---|---|---|"]
    for f in data["funnel"]:
        rate = f"{f['rate']:.0%}" if f.get("rate") is not None else "—"
        out.append(f"| {f['name']} | {f['count']} | {rate} |")
    if data["insights"]:
        out += ["", "## Insights", ""]
        out += [f"- {i}" for i in data["insights"]]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.telemetry.report")
    parser.add_argument("--since", default="24h", help="window: e.g. 1h, 24h, 7d")
    parser.add_argument("--markdown", action="store_true", help="markdown tables (default text)")
    parser.add_argument("--story-id", type=int, default=None, help="trace one story")
    parser.add_argument("--db", default=None, help="DISCOVER_DATABASE_URL override")
    args = parser.parse_args(argv)

    import os

    if args.db:
        os.environ["DISCOVER_DATABASE_URL"] = args.db
    from app.db import init_db, make_engine, make_session_factory
    from app.settings import get_settings
    from app.telemetry import queries

    settings = get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    hours = _since(args.since)
    from app import telemetry
    from app.config import load_config

    try:
        config = load_config(settings.config_path)
    except Exception:
        config = None
    telemetry.configure(factory, config)
    with factory() as session:
        if args.story_id is not None:
            trace = queries.story_trace(session, args.story_id)
            trace["story_id"] = args.story_id
            print(story_markdown(trace))
            return 0
        data = {
            "hours": hours,
            "app_version": telemetry.app_version(),
            "config_hash": telemetry.config_hash(),
            "llm": queries.llm_stats(session, hours=hours),
            "stages": queries.flow_overview(session, hours=hours),
            "funnel": queries.funnel(session, hours=hours)["steps"],
            "insights": queries.insights(session, hours=hours),
        }
    print(summary_markdown(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
