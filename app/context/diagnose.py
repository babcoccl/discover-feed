"""Show what the context search does for one story: the angles, each provider's
(truncated) raw response, and every candidate's decision, reason, similarity and judge
score.

    python -m app.context.diagnose --story-id 42 --dry-run
    python -m app.context.diagnose --auto            # the newest eligible story; persists
    docker exec discover-feed python -m app.context.diagnose --story-id 42 --dry-run

``--dry-run`` writes nothing (no status, candidates, links or search-cache entries); only
the shared provider pacing / cooldown state and request counts are updated. A
cooling-down provider's searches are skipped (its fallback answers); with every provider
cooling down it refuses (exit 3) unless ``--wait``.
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.context.cli import build, only_providers, respect_cooldowns
from app.context.service import ContextResult
from app.models import Story


def report(result: ContextResult, title: str = "") -> str:
    lines = [f"# Story {result.story_id}: {title}".rstrip(": ")]
    if result.planner is not None:  # planner mode: show tasks instead of angles
        planner = result.planner
        lines.append(
            "planner: "
            + ("(fallback) " if planner.get("fallback") else "")
            + (planner.get("error") or "ok")
        )
        for t in planner.get("tasks") or []:
            lines.append(
                f"  - [{t['type']}] subject={t['subject']!r} p{t['priority']} {t['question']}"
            )
        if planner.get("raw"):
            lines.append(f"  raw: {planner['raw'][:400]}")
    lines.append("angles:")
    lines += [
        f"  - [{a.label}] {a.query}" + (" (heuristic)" if a.heuristic else "")
        for a in result.angles
    ]
    if not result.searches and result.reason:
        lines.append(f"not searched: {result.reason}")
    for s in result.searches:
        if s.get("cached"):
            state = "cached"
        elif s.get("error"):
            state = "error: " + s["error"]
        else:
            state = "ok"
        if s.get("skipped"):
            state = f"skipped ({s['skipped']}): {s['error']}"
        sent = f" · {s['requests']} request(s)" if s.get("requests") else ""
        angle = f" · angle {s['angle']!r}" if s.get("angle") else ""
        lines.append(
            f"\n## {s['provider']} · {s['query']!r}{angle} · {s['hits']} hit(s){sent} · {state}"
        )
        if s.get("fields"):
            lines.append(
                "fields: "
                + ", ".join(
                    f"{k}={v}" for k, v in sorted(s["fields"].items(), key=lambda kv: -kv[1])
                )
            )
        if s.get("raw"):
            lines.append("raw: " + s["raw"].replace("\n", " ")[:600])
    lines.append("\n## Candidates")
    for c in result.candidates:
        tsim = f"{c.text_similarity:.2f}" if c.text_similarity is not None else "-"
        ents = str(c.shared_entities) if c.shared_entities is not None else "-"
        judge = str(c.judge) if c.judge is not None else "-"
        reason = c.reason + (f"({c.same_event_signal})" if c.same_event_signal else "")
        lines.append(
            f"- [{c.decision}] {reason:<22} sim {tsim} ents {ents} judge {judge}  "
            f"angle {c.angle or '-'} · {c.hit.domain or '?'}\n"
            f"    {(c.hit.title or '')[:90]}\n    {c.hit.url}"
        )
        if c.note:
            lines.append(f"    note: {c.note}")
        if c.fact:
            lines.append(f"    fact: {c.fact}")
        if c.quote:
            lines.append(f"    quote: {c.quote}" + (f" — {c.speaker}" if c.speaker else ""))
        if c.extract_raw:
            lines.append(f"    extract: {c.extract_raw[:300]}")
        if c.attribution:
            lines.append(f"    attribution: {c.attribution} · origin {c.web_origin or '-'}")
        if c.page is not None and c.page.status != "ok" and getattr(c.page, "reason", ""):
            lines.append(f"    fetch: {c.page.status} ({c.page.reason})")
    if not result.candidates:
        lines.append("(none)")
    if result.composed is not None:
        lines.append("\n## Composed")
        for item in result.composed.get("items") or []:
            lines.append(f"- [{item['id']}] {item['type']} · {item['note']}")
        tan = result.composed.get("then_and_now")
        if tan:
            lines.append(
                f"- then_and_now: {tan['relation']} "
                f"({'shown' if tan.get('show') else 'timeline only'}): {tan['summary']}"
            )
    if result.timings:
        lines.append("\ntimings: " + ", ".join(f"{k} {v}s" for k, v in result.timings.items()))
    if result.waiting:
        lines.append(
            "waiting: " + ", ".join(f"{w['reason']} {w.get('seconds', 0)}s" for w in result.waiting)
        )
    lines.append(
        f"\nresult: {result.status}, {len(result.accepted)} accepted"
        + (f", {result.added} added" if result.added else "")
        + (f" ({result.reason})" if result.reason else "")
        + (f"; retry at {result.retry_at:%H:%M:%S} UTC" if result.retry_at else "")
    )
    return "\n".join(lines)


def _latest_trace(factory, story_id: int) -> str | None:
    """The newest persisted ``context_traces`` row for the story, formatted — the
    durable record of the last real run (planner output, queries, decisions, timings)."""
    import json

    from app.models import ContextTrace

    with factory() as session:
        row = session.scalar(
            select(ContextTrace)
            .where(ContextTrace.story_id == story_id)
            .order_by(ContextTrace.id.desc())
            .limit(1)
        )
    if row is None:
        return None
    lines = [f"## Persisted trace (run {row.run_id[:8]}, {row.created_at})"]
    lines.append("```json")
    lines.append(json.dumps(row.trace, indent=1, default=str)[:6000])
    lines.append("```")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.context.diagnose")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--story-id", type=int)
    which.add_argument("--auto", action="store_true", help="the newest eligible story")
    parser.add_argument("--dry-run", action="store_true", help="search and judge; add nothing")
    parser.add_argument("--provider", action="append", help="only this provider (repeatable)")
    parser.add_argument("--wait", action="store_true", help="wait out a provider cooldown")
    args = parser.parse_args(argv)
    factory, finder = build()
    only_providers(finder, args.provider)
    if not respect_cooldowns(finder, wait=args.wait):
        return 3
    with factory() as session:
        if args.auto:
            ids = finder.eligible(session) or list(
                session.scalars(
                    select(Story.id)
                    .where(Story.published_version.is_not(None), Story.state != "merged")
                    .order_by(Story.last_updated_at.desc())
                    .limit(1)
                )
            )
            if not ids:
                print("no stories", file=sys.stderr)
                return 1
            story_id = ids[0]
        else:
            story_id = args.story_id
        story = session.get(Story, story_id)
        if story is None:
            print(f"no story {story_id}", file=sys.stderr)
            return 1
        title = story.title or ""
    result = asyncio.run(finder.find_story(story_id, dry_run=args.dry_run))
    print(report(result, title))
    trace = _latest_trace(factory, story_id)
    if trace:
        print()
        print(trace)
    return 0 if result.status != "failed" else 2


if __name__ == "__main__":
    sys.exit(main())
