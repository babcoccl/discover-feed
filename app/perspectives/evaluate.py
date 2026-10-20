"""Review 'how outlets differ' output on live stories — digests and the synthesis for
each eligible published story, so false balance and misquotes are easy to spot.

``python -m app.perspectives.evaluate --stories 20 --markdown`` prints markdown;
``--json`` prints the raw structures; ``--store`` writes the artifacts like the worker
(default is a dry run that writes nothing)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta

from sqlalchemy import select

from app.models import Story, StoryState, utcnow
from app.perspectives.cli import build


def _markdown(story, outcome) -> str:
    lines = [f"## Story {story.id}: {story.title}\n"]
    lines.append(
        f"status: **{outcome.status}**" + (f" ({outcome.reason})" if outcome.reason else "")
    )
    detail = outcome.detail or {}
    digests = detail.get("digests") or []
    if digests:
        lines.append("")
        for n, d in enumerate(digests, 1):
            lines.append(
                f"**[{n}] {d['label']}** — stance: `{d['stance']}`, kind: `{d['kind']}`, "
                f"framing: {d['framing']}"
            )
            for c in d["claims"]:
                lines.append(f"- {c['text']} — “{c['quote']}”")
        synthesis = detail.get("synthesis")
        if synthesis:
            lines.append("\n**synthesis**")
            for a in synthesis.get("agreement") or []:
                lines.append(f"- agree: {a['text']} `{a['citations']}`")
            for d_ in synthesis.get("disagreements") or []:
                lines.append(f"- **disagree: {d_['topic']}**")
                for p in d_["positions"]:
                    lines.append(
                        f"  - {p['label']}: {p['summary']} `{p['citations']}` — “{p['quote']}”"
                    )
            for a in synthesis.get("different_angles") or []:
                lines.append(f"- angle: {a['text']} `{a['citations']}`")
        for aid, err in (detail.get("errors") or {}).items():
            lines.append(f"- digest dropped for article {aid}: {err}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.perspectives.evaluate")
    parser.add_argument("--stories", type=int, default=20)
    parser.add_argument("--markdown", action="store_true", help="Markdown report (default).")
    parser.add_argument("--json", action="store_true", help="Raw JSON instead.")
    parser.add_argument(
        "--store",
        action="store_true",
        help="Store the digests/synthesis like the worker (default: dry run).",
    )
    parser.add_argument(
        "--max-age-hours", type=float, default=168, help="Only stories updated this recently."
    )
    args = parser.parse_args()
    factory, service = build()
    if service is None or service.client is None:
        print("perspectives is not configured (no llm role).", file=sys.stderr)
        return 2
    with factory() as session:
        ids = list(
            session.scalars(
                select(Story.id)
                .where(
                    Story.state == StoryState.PUBLISHED.value,
                    Story.last_updated_at >= utcnow() - timedelta(hours=args.max_age_hours),
                )
                .order_by(Story.last_updated_at.desc())
                .limit(args.stories)
            )
        )
    if not ids:
        print("no published stories in range", file=sys.stderr)
        return 0
    report = []
    for sid in ids:
        try:
            outcome = asyncio.run(
                service.run_story(story_id=sid, dry_run=not args.store)
            )
        except Exception as exc:  # transport failures: report, keep going
            from app.perspectives.service import PerspectiveOutcome

            outcome = PerspectiveOutcome(sid, "failed", f"{type(exc).__name__}: {exc}")
        with factory() as session:
            story = session.get(Story, sid)
        if args.json:
            report.append(
                {
                    "story_id": sid,
                    "title": story.title if story else "",
                    "status": outcome.status,
                    "reason": outcome.reason,
                    "detail": outcome.detail,
                }
            )
        else:
            print(_markdown(story, outcome) if story else f"## Story {sid}\n{outcome.status}")
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
