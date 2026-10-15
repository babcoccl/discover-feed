"""Show what enrichment does for one story: the queries, each provider's (truncated) raw
response, and every candidate's decision, reason and similarity.

    python -m app.enrich.diagnose --story-id 42 --dry-run
    python -m app.enrich.diagnose --auto            # the newest eligible story; persists
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.enrich.cli import build
from app.enrich.service import StoryEnrichment
from app.models import Story


def report(result: StoryEnrichment, title: str = "") -> str:
    lines = [f"# Story {result.story_id}: {title}".rstrip(": ")]
    lines.append(f"publishers before: {result.publishers_before}")
    lines.append(f"queries ({result.query_mode}):")
    lines += [f"  - {q}" for q in result.queries]
    for s in result.searches:
        state = "cached" if s["cached"] else ("error: " + s["error"] if s["error"] else "ok")
        lines.append(f"\n## {s['provider']} · {s['query']!r} · {s['hits']} hit(s) · {state}")
        if s["raw"]:
            lines.append("raw: " + s["raw"].replace("\n", " ")[:600])
    lines.append("\n## Candidates")
    for c in result.candidates:
        sim = f"{c.similarity:.2f}" if c.similarity is not None else "-"
        tsim = f"{c.text_similarity:.2f}" if c.text_similarity is not None else "-"
        lines.append(
            f"- [{c.decision}] {c.reason:<20} snippet {sim} text {tsim}  "
            f"{c.hit.domain or '?'} · {c.hit.title[:90]}\n    {c.hit.url}"
        )
    if not result.candidates:
        lines.append("(none)")
    lines.append(
        f"\nresult: {result.status}, {len(result.accepted)} accepted"
        + (f", {result.added} added" if result.added else "")
        + (f" ({result.reason})" if result.reason else "")
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.enrich.diagnose")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--story-id", type=int)
    which.add_argument("--auto", action="store_true", help="the newest eligible story")
    parser.add_argument("--dry-run", action="store_true", help="search and judge; add nothing")
    args = parser.parse_args(argv)
    factory, enricher = build()
    with factory() as session:
        if args.auto:
            ids = enricher.eligible(session) or list(
                session.scalars(
                    select(Story.id)
                    .where(Story.state != "merged")
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
    result = asyncio.run(enricher.enrich_story(story_id, dry_run=args.dry_run))
    print(report(result, title))
    return 0 if result.status != "failed" else 2


if __name__ == "__main__":
    sys.exit(main())
