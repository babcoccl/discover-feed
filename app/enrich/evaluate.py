"""Dry-run enrichment over recent stories and report how it would do (nothing is added):
how many stories gain a source, how many sources, why candidates were rejected, and every
accepted page so a person can spot false matches.

    python -m app.enrich.evaluate --stories 30 --markdown
"""

import argparse
import asyncio
import sys
from collections import Counter

from sqlalchemy import select

from app.enrich.cli import build
from app.enrich.service import StoryEnrichment
from app.lifecycle import PRE_CLUSTER
from app.models import Story, StoryState


def summary(results: list[tuple[str, StoryEnrichment]], *, markdown: bool) -> str:
    n = len(results) or 1
    gained = [r for _, r in results if r.accepted]
    reasons = Counter(
        c.reason for _, r in results for c in r.candidates if c.decision != "accepted"
    )
    errors = Counter(s["provider"] for _, r in results for s in r.searches if s.get("error"))
    lines = []
    h = "## " if markdown else ""
    lines.append(f"{h}Enrichment evaluation ({len(results)} stories, dry run)")
    lines.append("")
    lines.append(f"- stories with ≥1 accepted source: {len(gained)} ({len(gained) / n:.0%})")
    lines.append(
        f"- accepted sources per story: {sum(len(r.accepted) for _, r in results) / n:.2f}"
    )
    lines.append(f"- candidates judged: {sum(len(r.candidates) for _, r in results)}")
    lines.append(f"- provider errors: {dict(errors) or 'none'}")
    lines.append("")
    lines.append(f"{h}Rejections by reason")
    lines += [f"- `{k}`: {v}" for k, v in reasons.most_common()] or ["- none"]
    lines.append("")
    lines.append(f"{h}Per story (check accepted pages for false matches)")
    if markdown:
        lines.append("| Story | Publishers | Queries | Candidates | Accepted |")
        lines.append("|---|---|---|---|---|")
    for title, r in results:
        accepted = (
            "<br>".join(
                f"{c.hit.domain}: {c.hit.title[:70]} ({c.text_similarity or 0:.2f})"
                for c in r.accepted
            )
            or "-"
        )
        queries = "; ".join(r.queries)
        row = [
            f"{r.story_id}: {title[:60]}",
            str(r.publishers_before),
            queries,
            str(len(r.candidates)),
            accepted,
        ]
        if markdown:
            lines.append("| " + " | ".join(x.replace("|", "/") for x in row) + " |")
        else:
            lines.append(" · ".join(row).replace("<br>", "; "))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.enrich.evaluate")
    parser.add_argument("--stories", type=int, default=30)
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)
    factory, enricher = build()
    with factory() as session:
        rows = session.execute(
            select(Story.id, Story.title)
            .where(Story.state.not_in([s.value for s in PRE_CLUSTER] + [StoryState.MERGED.value]))
            .order_by(Story.last_updated_at.desc())
            .limit(args.stories)
        ).all()
    if not rows:
        print("no clustered stories", file=sys.stderr)
        return 1

    async def run() -> list[tuple[str, StoryEnrichment]]:
        return [(t or "", await enricher.enrich_story(i, dry_run=True)) for i, t in rows]

    print(summary(asyncio.run(run()), markdown=args.markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
