"""Dry-run the context search over recent published stories and report how it would do
(nothing is added): how many stories gain a link, how many links, why candidates were
rejected, and every accepted page so a person can spot false matches.

    python -m app.context.evaluate --stories 30 --markdown
    python -m app.context.evaluate --story-ids 631,632,637
    python -m app.context.evaluate --stories 20 --provider tinyfish
    docker exec discover-feed python -m app.context.evaluate --stories 20 --markdown

Writes nothing (no status, candidates, links or search-cache entries); only the shared
provider pacing / cooldown state and request counts are updated. Refuses while every
provider is cooling down (exit 3) unless ``--wait``, which also waits whenever a provider
starts cooling down mid-run ("waiting for tinyfish (cooling down)"). Expect
~``min_interval_seconds`` per angle query (tinyfish 2 s; ``max_angles`` per provider).
"""

import argparse
import asyncio
import sys
from collections import Counter

from sqlalchemy import select

from app.context.cli import build, only_providers, respect_cooldowns, wait_for_cooldowns
from app.context.service import ContextResult
from app.models import Story


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


def _summary(results: list[ContextResult], markdown: bool) -> str:
    lines = [
        f"# Context search evaluation — {len(results)} stories",
        "",
        f"- stories with links: {sum(1 for r in results if r.accepted)}",
        f"- links accepted: {sum(len(r.accepted) for r in results)}",
        f"- candidates considered: {sum(len(r.candidates) for r in results)}",
        f"- stories with 0 candidates: {sum(1 for r in results if not r.candidates)}",
        "- providers unanswered (retried): "
        f"{sum(1 for r in results if not r.answered and r.status != 'done')}",
        f"- failed: {sum(1 for r in results if r.status == 'failed')}",
    ]
    judged = [c for r in results for c in r.candidates if c.judge is not None]
    if judged:
        kept = sum(1 for c in judged if c.judge == 2)
        lines.append(f"- judge accept rate: {kept}/{len(judged)} ({kept / len(judged):.0%})")
    reasons = Counter(c.reason for r in results for c in r.candidates if c.decision == "rejected")
    if reasons:
        lines.append("\n## Rejection reasons")
        lines += _table(
            ["reason", "count"],
            [[k or "unknown", str(n)] for k, n in reasons.most_common()],
            markdown,
        )
    same_event = [
        (r, c)
        for r in results
        for c in r.candidates
        if c.decision == "rejected" and c.reason == "same_event"
    ]
    if same_event:
        lines.append("\n## Same-event rejections")
        lines += _table(
            ["story", "judge", "domain", "title / url"],
            [
                [
                    str(r.story_id),
                    str(c.judge) if c.judge is not None else "-",
                    c.hit.domain or "?",
                    f"{(c.hit.title or '')[:70]} — {c.hit.url}",
                ]
                for r, c in same_event
            ],
            markdown,
        )
    accepted = [c for r in results for c in r.accepted]
    if accepted:
        lines.append("\n## Accepted pages")
        lines += _table(
            ["story", "angle", "judge", "opinion", "flagged", "domain", "note", "title / url"],
            [
                [
                    str(r.story_id),
                    c.angle or "-",
                    str(c.judge) if c.judge is not None else "-",
                    "yes" if c.is_opinion else "-",
                    c.flagged or "-",
                    c.hit.domain or "?",
                    (c.note or "-")[:60],
                    f"{(c.hit.title or '')[:60]} — {c.hit.url}",
                ]
                for r in results
                for c in r.accepted
            ],
            markdown,
        )
        domains = Counter(c.hit.domain or "?" for c in accepted)
        repeat = [(d, n) for d, n in domains.most_common() if n >= 2]
        if repeat:
            lines.append("\n## Domains accepted 2+ times")
            lines += _table(
                ["domain", "accepted"],
                [[d, str(n)] for d, n in repeat],
                markdown,
            )
    lines.append("\n## Per story")
    lines += _table(
        ["story", "angles", "candidates", "accepted", "top rejections"],
        [
            [
                str(r.story_id),
                str(len(r.angles)),
                str(len(r.candidates)),
                str(len(r.accepted)),
                ", ".join(
                    f"{k}:{n}"
                    for k, n in Counter(
                        c.reason for c in r.candidates if c.decision == "rejected"
                    ).most_common(3)
                )
                or r.reason
                or "-",
            ]
            for r in results
        ],
        markdown,
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.context.evaluate")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--stories", type=int, default=30, help="newest N published stories")
    which.add_argument("--story-ids", help="comma-separated story ids")
    parser.add_argument("--provider", action="append", help="only this provider (repeatable)")
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--wait", action="store_true", help="wait out provider cooldowns")
    args = parser.parse_args(argv)
    factory, finder = build()
    only_providers(finder, args.provider)
    if not respect_cooldowns(finder, wait=args.wait):
        return 3
    with factory() as session:
        if args.story_ids:
            ids = [int(x) for x in args.story_ids.split(",") if x.strip()]
        else:
            ids = list(
                session.scalars(
                    select(Story.id)
                    .where(Story.published_version.is_not(None), Story.state != "merged")
                    .order_by(Story.last_updated_at.desc())
                    .limit(args.stories)
                )
            )
    if not ids:
        print("no published stories", file=sys.stderr)
        return 1

    async def run() -> list[ContextResult]:
        out = []
        for sid in ids:
            if args.wait:
                wait_for_cooldowns(finder)
            out.append(await finder.find_story(sid, dry_run=True))
        return out

    print(_summary(asyncio.run(run()), args.markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
