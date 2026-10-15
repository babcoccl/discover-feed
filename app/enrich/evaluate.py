"""Dry-run enrichment over recent stories and report how it would do (nothing is added):
how many stories gain a source, how many sources, why candidates were rejected, and every
accepted page so a person can spot false matches.

    python -m app.enrich.evaluate --stories 30 --markdown
    python -m app.enrich.evaluate --stories 20 --provider gdelt
    docker exec discover-feed python -m app.enrich.evaluate --stories 20 --markdown

Writes nothing (no status, candidates or search-cache entries); only the shared provider
pacing / cooldown state and request counts are updated. Refuses during a provider
cooldown (exit 3) unless ``--wait``. Expect ~``min_interval_seconds`` per query (GDELT:
5 s or more, up to ``max_queries_per_story`` queries per story).
"""

import argparse
import asyncio
import sys
from collections import Counter

from sqlalchemy import select

from app.enrich.cli import build, only_providers, respect_cooldowns
from app.enrich.service import Candidate, StoryEnrichment
from app.lifecycle import PRE_CLUSTER
from app.models import Story, StoryState


def _f(x: float | None) -> str:
    return f"{x:.2f}" if x is not None else "-"


def _table(head: list[str], rows: list[list[str]], markdown: bool) -> list[str]:
    if not rows:
        return ["(none)"]
    if markdown:
        out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        return out + ["| " + " | ".join(c.replace("|", "/") for c in r) + " |" for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(head)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return [fmt.format(*head)] + [fmt.format(*r) for r in rows]


def _avg(values: list[float | None]) -> str:
    vals = [v for v in values if v is not None]
    return f"{sum(vals) / len(vals):.2f}" if vals else "-"


def summary(results: list[tuple[str, StoryEnrichment]], *, markdown: bool) -> str:
    """Precision first: every accepted page with its scores (check them for false matches),
    then rejections by reason with their average scores, then per-story query results."""
    n = len(results) or 1
    gained = [r for _, r in results if r.accepted]
    rejected: dict[str, list[Candidate]] = {}
    for _, r in results:
        for c in r.candidates:
            if c.decision != "accepted":
                rejected.setdefault(c.reason or "unknown", []).append(c)
    searches = [s for _, r in results for s in r.searches]
    errors = Counter(s["provider"] for s in searches if s.get("error") and not s.get("skipped"))
    skipped = Counter(s["skipped"] for s in searches if s.get("skipped"))
    h = "## " if markdown else ""
    lines = [f"{h}Enrichment evaluation ({len(results)} stories, dry run)", ""]
    lines.append(f"- stories with >=1 accepted source: {len(gained)} ({len(gained) / n:.0%})")
    lines.append(
        f"- accepted sources per story: {sum(len(r.accepted) for _, r in results) / n:.2f}"
    )
    judged = sum(len(r.candidates) for _, r in results)
    accepted = sum(len(r.accepted) for _, r in results)
    lines.append(f"- candidates judged: {judged}, accepted: {accepted}")
    lines.append(
        f"- searches: {len(searches)} ({sum(1 for s in searches if s['cached'])} cached, "
        f"{sum(1 for s in searches if not s['hits'] and not s.get('error'))} with 0 hits)"
    )
    lines.append(f"- provider errors: {dict(errors) or 'none'}; skipped: {dict(skipped) or 'none'}")
    retry = [r.story_id for _, r in results if r.status == "retry"]
    if retry:
        lines.append(f"- no provider answered (would retry): {len(retry)} stories")
    lines += ["", f"{h}Accepted (check each for false matches)"]
    rows = [
        [
            str(r.story_id),
            c.provider,
            c.hit.domain or "?",
            _f(c.similarity),
            _f(c.title_similarity),
            _f(c.text_similarity),
            c.hit.title[:70],
        ]
        for _, r in results
        for c in r.accepted
    ]
    lines += _table(
        ["story", "provider", "domain", "snippet", "title", "text", "headline"], rows, markdown
    )
    lines += ["", f"{h}Rejected by reason"]
    rows = [
        [
            reason,
            str(len(cs)),
            _avg([c.similarity for c in cs]),
            _avg([c.title_similarity for c in cs]),
            _avg([c.text_similarity for c in cs]),
        ]
        for reason, cs in sorted(rejected.items(), key=lambda kv: -len(kv[1]))
    ]
    lines += _table(["reason", "count", "avg snippet", "avg title", "avg text"], rows, markdown)
    lines += ["", f"{h}Per story"]
    rows = []
    for title, r in results:
        found = "; ".join(
            f"{s['provider']} {s['query']!r}: "
            + (s["error"] if s.get("error") else f"{s['hits']} hit(s)")
            for s in r.searches
        )
        rows.append(
            [
                f"{r.story_id}: {title[:50]}",
                str(r.publishers_before),
                found or "-",
                str(len(r.candidates)),
                str(len(r.accepted)),
            ]
        )
    lines += _table(["story", "publishers", "searches", "candidates", "accepted"], rows, markdown)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.enrich.evaluate")
    parser.add_argument("--stories", type=int, default=30)
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--provider", action="append", help="only this provider (repeatable)")
    parser.add_argument("--wait", action="store_true", help="wait out a provider cooldown")
    args = parser.parse_args(argv)
    factory, enricher = build()
    only_providers(enricher, args.provider)
    if not respect_cooldowns(enricher, wait=args.wait):
        return 3
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
