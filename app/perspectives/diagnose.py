"""Show one story's per-source digests (with their verified quotes), the synthesis and
any validation errors — ``python -m app.perspectives.diagnose --story-id N``.

``--dry-run`` generates without storing anything (digests, synthesis or skipped
markers). Without it the outcome is stored exactly like the worker would store it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import select

from app.models import ArticleDigest, Story, StorySummary
from app.perspectives.cli import build


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.perspectives.diagnose")
    parser.add_argument("--story-id", type=int, required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate but write nothing to the database.",
    )
    args = parser.parse_args()
    factory, service = build()
    if service is None or service.client is None:
        print("perspectives is not configured (no llm role).", file=sys.stderr)
        return 2
    outcome = asyncio.run(service.run_story(story_id=args.story_id, dry_run=args.dry_run))
    print(
        f"story {outcome.story_id}: {outcome.status}"
        + (f" ({outcome.reason})" if outcome.reason else "")
    )
    print(f"digests: {outcome.digests}")
    if outcome.detail:
        digests = outcome.detail.get("digests") or []
        for n, d in enumerate(digests, 1):
            print(f"\n[{n}] {d['label']} (article {d['article_id']})")
            print(f"  stance: {d['stance']} · kind: {d['kind']} · framing: {d['framing']}")
            for c in d["claims"]:
                print(f'  - {c["text"]}\n      "{c["quote"]}"')
        synthesis = outcome.detail.get("synthesis")
        if synthesis:
            print("\nsynthesis:")
            print(json.dumps(synthesis, indent=2)[:4000])
        errors = outcome.detail.get("errors") or {}
        for aid, err in errors.items():
            print(f"\narticle {aid} digest dropped: {err}")
    else:
        with factory() as session:
            story = session.get(Story, args.story_id)
            if story is not None:
                print(f"\nstored digests for story {args.story_id}:")
                for d in session.scalars(
                    select(ArticleDigest).where(ArticleDigest.story_id == args.story_id)
                ):
                    print(f"  article {d.article_id}: {d.status} {d.reason or ''}")
            row = session.scalar(
                select(StorySummary)
                .where(
                    StorySummary.story_id == args.story_id,
                    StorySummary.kind == "perspectives",
                )
                .order_by(StorySummary.id.desc())
                .limit(1)
            )
            if row is not None:
                reason = f": {row.reason}" if row.reason else ""
                print(f"\nstored synthesis ({row.status}{reason}):")
                print(json.dumps(row.content_json, indent=2)[:4000])
            else:
                print("\nno stored synthesis")
    return 0 if outcome.status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
