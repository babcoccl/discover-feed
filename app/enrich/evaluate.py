"""Dry-run enrichment over recent stories and report how it would do (nothing is added):
how many stories gain a source, how many sources, why candidates were rejected, and every
accepted page so a person can spot false matches.

    python -m app.enrich.evaluate --stories 30 --markdown
    python -m app.enrich.evaluate --story-ids 631,632,637
    python -m app.enrich.evaluate --stories 20 --provider gdelt
    python -m app.enrich.evaluate --backtest --stories 30 --markdown
    docker exec discover-feed python -m app.enrich.evaluate --stories 20 --markdown

``--backtest`` is the recall measurement: it picks stories that already have >= 3 feed
publishers, hides every member but the anchor, runs the real search and filters (still a
dry run — nothing is written), and reports the share of the real other publishers'
domains the search found, plus any extra domains it found. Per-story and overall.

Writes nothing (no status, candidates or search-cache entries); only the shared provider
pacing / cooldown state and request counts are updated. Refuses while every provider is
cooling down (exit 3) unless ``--wait``, which also waits whenever a provider starts
cooling down mid-run ("waiting for gdelt (cooling down)"). Expect ~``min_interval_seconds``
per query (tinyfish 2 s, GDELT 5 s or more; up to ``max_queries_per_story`` per provider).
"""

import argparse
import asyncio
import sys
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from sqlalchemy import select

from app.enrich.cli import build, only_providers, respect_cooldowns, wait_for_cooldowns
from app.enrich.domains import canonical_domain
from app.enrich.service import SKIPPED, Candidate, StoryEnrichment
from app.lifecycle import PRE_CLUSTER
from app.models import ENRICHMENT, Article, Story, StoryState


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


VIA = {"": "text", "b": "snippet+b", "c": "entities+c", "d": "title+d"}


def _via(c: Candidate) -> str:
    return "link" if c.decision == "link_only" else VIA.get(c.rule, f"rule_{c.rule}")


def _fetch_cause(detail: str) -> str:
    """A ``fetch_failed`` / ``blocked_url`` / ``robots`` candidate's page failure class:
    ``http_503``, ``timeout``, ``size_cap``, ``extract_empty``, ``not_html``, ``blocked``,
    ``robots``, ``paywalled`` or ``other``."""
    if not detail:
        return "unknown"
    status, _, reason = detail.partition(":")
    reason = reason.strip().lower()
    if status == "skipped":
        return "robots"
    if status == "paywalled":
        return "paywalled"
    if status == "blocked":
        return "blocked"
    if reason.startswith("http "):
        code = reason.split()[1].split(")")[0]
        return f"http_{code}"
    if "timeout" in reason or "timed out" in reason:
        return "timeout"
    if reason.startswith("larger than"):
        return "size_cap"
    if reason.startswith("extracted"):
        return "extract_empty"
    if reason.startswith("not html"):
        return "not_html"
    if "blocked" in reason:
        return "blocked"
    return "other"


def summary(results: list[tuple[str, StoryEnrichment]], *, markdown: bool) -> str:
    """Precision first: every accepted page with its scores (check them for false matches),
    then rejections by reason with their average scores, then per-story query results."""
    n = len(results) or 1
    gained = [r for _, r in results if r.accepted]
    rejected: dict[str, list[Candidate]] = {}
    for _, r in results:
        for c in r.candidates:
            if c.decision == "rejected":
                rejected.setdefault(c.reason or "unknown", []).append(c)
    searches = [s for _, r in results for s in r.searches]
    skipped = Counter(s["skipped"] for s in searches if s.get("skipped"))
    errors: dict[tuple[str, str], list[str]] = {}
    for s in searches:
        if s.get("error") and not s.get("skipped"):
            errors.setdefault((s["provider"], s["error"]), []).append(s["query"])
    story_skips = Counter(
        r.reason.removeprefix(SKIPPED)
        for _, r in results
        if r.reason and r.reason.startswith(SKIPPED)
    )
    h = "## " if markdown else ""
    lines = [f"{h}Enrichment evaluation ({len(results)} stories, dry run)", ""]
    lines.append(f"- stories with >=1 accepted source: {len(gained)} ({len(gained) / n:.0%})")
    lines.append(
        f"- accepted sources per story: {sum(len(r.accepted) for _, r in results) / n:.2f}"
    )
    judged = sum(len(r.candidates) for _, r in results)
    accepted = sum(len(r.accepted) for _, r in results)
    lines.append(f"- candidates judged: {judged}, accepted: {accepted}")
    link_only = sum(len(r.link_only) for _, r in results)
    if link_only:
        lines.append(f"- link-only members (enrichment.link_only): {link_only}")
    flagged = [c for _, r in results for c in [*r.accepted, *r.link_only] if c.flag]
    if flagged:
        flag_domains = Counter(c.hit.domain or "?" for c in flagged)
        lines.append(
            f"- flagged sources (domain looks like a content farm; review for "
            f"enrichment.quality.deny_domains): {len(flagged)} "
            f"({', '.join(f'{d} x{n}' for d, n in flag_domains.most_common())})"
        )
    by_rule = Counter(_via(c) for _, r in results for c in r.accepted)
    extra = {k: v for k, v in sorted(by_rule.items()) if k not in ("text", "link")}
    if extra:
        lines.append(f"- newly accepted by extra rules (review for false matches): {extra}")
    lines.append(
        f"- stories not searched: {dict(sorted(story_skips.items())) or 'none'}"
        " (skip_title_patterns / skip_url_patterns / min_signal_tokens)"
    )
    lines.append(
        f"- searches: {len(searches)} ({sum(1 for s in searches if s['cached'])} cached, "
        f"{sum(1 for s in searches if not s['hits'] and not s.get('error'))} with 0 hits)"
    )
    providers = list(dict.fromkeys(s["provider"] for s in searches))
    for name in providers:
        mine = [s for s in searches if s["provider"] == name]
        lines.append(
            f"- {name}: {sum(s.get('requests') or 0 for s in mine)} request(s) sent, "
            f"{sum(1 for s in mine if s.get('ok', not s.get('error')))} answered, "
            f"{sum(1 for s in mine if s.get('skipped') == 'cooldown')} cooldown skip(s), "
            f"{sum(1 for s in mine if s.get('error') and not s.get('skipped'))} error(s), "
            f"{sum(s['hits'] for s in mine)} hit(s)"
        )
        fields = Counter()
        for s in mine:
            fields.update(s.get("fields") or {})
        if fields:
            lines.append(
                "  - result fields: " + ", ".join(f"{k}={v}" for k, v in fields.most_common())
            )
    lines.append(f"- skipped searches: {dict(skipped) or 'none'}")
    lines.append(f"- provider errors: {'none' if not errors else ''}")
    for (provider, error), queries in sorted(errors.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"  - {provider} x{len(queries)}: {error} (e.g. query {queries[0]!r})")
    retry = [r.story_id for _, r in results if r.status == "retry"]
    if retry:
        lines.append(f"- no provider answered (would retry): {len(retry)} stories")
    lines += ["", f"{h}Accepted (check each for false matches)"]
    rows = [
        [
            str(r.story_id),
            c.provider,
            c.hit.domain or "?",
            _via(c),
            _f(c.similarity),
            _f(c.title_similarity),
            _f(c.text_similarity),
            c.flag or "-",
            c.hit.title[:70],
        ]
        for _, r in results
        for c in r.accepted
    ]
    lines += _table(
        ["story", "provider", "domain", "via", "snippet", "title", "text", "flag", "headline"],
        rows,
        markdown,
    )
    lines += ["", f"{h}Rejected by reason"]
    rows = [
        [
            reason,
            str(len(cs)),
            _avg([c.similarity for c in cs]),
            _avg([c.title_similarity for c in cs]),
            _avg([c.text_similarity for c in cs]),
            _avg([float(c.shared_entities) if c.shared_entities is not None else None for c in cs]),
        ]
        for reason, cs in sorted(rejected.items(), key=lambda kv: -len(kv[1]))
    ]
    lines += _table(
        ["reason", "count", "avg snippet", "avg title", "avg text", "avg entities"],
        rows,
        markdown,
    )
    fetch_failed = [
        c
        for _, r in results
        for c in r.candidates
        if c.decision == "rejected" and c.reason in ("fetch_failed", "blocked_url")
    ]
    if fetch_failed:
        lines += ["", f"{h}Fetch failures by cause (fetch_failed / blocked_url)"]
        causes: dict[str, list[str]] = defaultdict(list)
        for c in fetch_failed:
            causes[_fetch_cause(c.fetch_detail)].append(
                canonical_domain(c.hit.url) or (urlsplit(c.hit.url).hostname or "?")
            )
        rows = [
            [cause, str(len(domains)), ", ".join(sorted(set(domains))[:8])]
            for cause, domains in sorted(causes.items(), key=lambda kv: -len(kv[1]))
        ]
        lines += _table(["cause", "count", "domains"], rows, markdown)
    lines += ["", f"{h}Per story"]
    rows = []
    for title, r in results:
        found = "; ".join(
            f"{s['provider']} {s['query']!r}: "
            + (
                f"{s['skipped']}"
                if s.get("skipped")
                else s["error"]
                if s.get("error")
                else f"{s['hits']} hit(s)" + (" (cached)" if s["cached"] else "")
            )
            for s in r.searches
        )
        top = sorted((c for c in r.candidates if c.decision == "rejected"), key=lambda c: -c.score)[
            :3
        ]
        rejected_top = "; ".join(
            f"{c.hit.domain or '?'} {c.reason} "
            f"{_f(c.similarity)}/{_f(c.title_similarity)}/{_f(c.text_similarity)}"
            f" e{c.shared_entities if c.shared_entities is not None else '-'}"
            for c in top
        )
        rows.append(
            [
                f"{r.story_id}: {title[:50]}",
                str(r.publishers_before),
                found or (r.reason or "-"),
                str(len(r.candidates)),
                str(len(r.accepted)),
                rejected_top or "-",
            ]
        )
    lines += _table(
        ["story", "publishers", "searches", "candidates", "accepted", "top rejected"],
        rows,
        markdown,
    )
    return "\n".join(lines)


def _feed_domains(session, story_ids: list[int] | None, limit: int):
    """Stories with their feed members' domains, for the backtest's ground truth."""
    stmt = (
        select(
            Article.story_id,
            Article.id,
            Article.url,
            Article.published_at,
            Story.title,
            Story.representative_article_id,
        )
        .join(Story, Story.id == Article.story_id)
        .where(
            Article.origin != ENRICHMENT,
            Story.state.not_in([s.value for s in PRE_CLUSTER] + [StoryState.MERGED.value]),
        )
        .order_by(Story.last_updated_at.desc(), Article.published_at, Article.id)
    )
    if story_ids:
        stmt = stmt.where(Story.id.in_(story_ids))
    members: dict[int, list] = {}
    titles: dict[int, str] = {}
    reps: dict[int, int | None] = {}
    for story_id, art_id, url, _at, title, rep_id in session.execute(stmt):
        titles.setdefault(story_id, title or "")
        reps.setdefault(story_id, rep_id)
        members.setdefault(story_id, []).append((art_id, url))
    return members, titles, reps


def _backtest_stories(session, cfg, story_ids, limit):
    """Stories with >= 3 distinct feed publishers: (id, title, anchor domain, expected)."""
    members, titles, reps = _feed_domains(session, story_ids, limit)
    out = []
    for story_id, arts in members.items():
        domains = {canonical_domain(u, cfg.publisher_aliases) for _i, u in arts if u}
        if len(domains) < 3:
            continue
        anchor_id = reps[story_id]
        anchor_url = next((u for i, u in arts if i == anchor_id), arts[0][1] if arts else "")
        anchor_domain = canonical_domain(anchor_url, cfg.publisher_aliases)
        expected = domains - {anchor_domain}
        if expected:
            out.append((story_id, titles[story_id], anchor_domain, expected))
    return out[:limit]


def backtest_summary(
    rows: list[tuple[int, str, str, set[str], StoryEnrichment]],
    *,
    markdown: bool,
    aliases: dict[str, str] | None = None,
) -> str:
    """Recall against ground truth: each row is
    ``(story_id, title, anchor_domain, expected_domains, result)``."""
    canon = lambda u: canonical_domain(u, aliases)  # noqa: E731
    h = "## " if markdown else ""
    lines = [f"{h}Enrichment backtest ({len(rows)} stories, dry run)", ""]
    total_expected = sum(len(e) for _i, _t, _a, e, _r in rows)
    hits = sum(len(e & {canon(c.hit.url) for c in r.accepted}) for _i, _t, _a, e, r in rows)
    lines.append(
        f"- recall: {hits}/{total_expected} real publishers' domains found "
        f"({(hits / total_expected) if total_expected else 0:.0%})"
    )
    extras = [
        canon(c.hit.url)
        for _i, _t, a, e, r in rows
        for c in r.accepted
        if canon(c.hit.url) not in e and canon(c.hit.url) != a
    ]
    lines.append(
        f"- extra domains found (not in the story's feed publishers): "
        f"{dict(Counter(extras).most_common(15)) or 'none'}"
    )
    lines += ["", f"{h}Per story"]
    rows_out = []
    for story_id, title, anchor, expected, r in rows:
        found = {canon(c.hit.url) for c in r.accepted}
        got = sorted(expected & found)
        missed = sorted(expected - found)
        recall = f"{len(got)}/{len(expected)} ({len(got) / len(expected):.0%})"
        rows_out.append(
            [
                f"{story_id}: {title[:45]}",
                anchor,
                ", ".join(sorted(expected))[:60],
                ", ".join(got) or "-",
                ", ".join(missed) or "-",
                recall,
            ]
        )
    lines += _table(
        ["story", "anchor", "expected", "found", "missed", "recall"], rows_out, markdown
    )
    lines += ["", f"{h}Search detail"]
    lines.append(summary([(t, r) for _i, t, _a, _e, r in rows], markdown=markdown))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.enrich.evaluate")
    parser.add_argument("--stories", type=int, default=30)
    parser.add_argument(
        "--story-ids",
        type=str,
        default=None,
        help="comma-separated story ids to evaluate (overrides --stories selection)",
    )
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--provider", action="append", help="only this provider (repeatable)")
    parser.add_argument(
        "--wait", action="store_true", help="wait out provider cooldowns (also mid-run)"
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="hide all but the anchor member of >=3-publisher stories and measure recall",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="always on: evaluate never writes status, candidates or cache entries",
    )
    args = parser.parse_args(argv)
    factory, enricher = build()
    only_providers(enricher, args.provider)
    if not respect_cooldowns(enricher, wait=args.wait):
        return 3
    story_ids = [int(x) for x in args.story_ids.split(",") if x.strip()] if args.story_ids else None
    if args.backtest:
        with factory() as session:
            picks = _backtest_stories(session, enricher.config, story_ids, args.stories)
        if not picks:
            print("no stories with >= 3 feed publishers", file=sys.stderr)
            return 1

        async def run_backtest():
            out = []
            for story_id, title, anchor, expected in picks:
                if args.wait:
                    await asyncio.to_thread(wait_for_cooldowns, enricher)
                result = await enricher.enrich_story(story_id, dry_run=True, anchor_only=True)
                out.append((story_id, title or "", anchor, expected, result))
            return out

        print(
            backtest_summary(
                asyncio.run(run_backtest()),
                markdown=args.markdown,
                aliases=enricher.config.publisher_aliases,
            )
        )
        return 0

    with factory() as session:
        stmt = (
            select(Story.id, Story.title)
            .where(Story.state.not_in([s.value for s in PRE_CLUSTER] + [StoryState.MERGED.value]))
            .order_by(Story.last_updated_at.desc())
        )
        stmt = stmt.where(Story.id.in_(story_ids)) if story_ids else stmt.limit(args.stories)
        rows = session.execute(stmt).all()
    if not rows:
        print("no clustered stories", file=sys.stderr)
        return 1

    async def run() -> list[tuple[str, StoryEnrichment]]:
        out = []
        for story_id, title in rows:
            if args.wait:
                await asyncio.to_thread(wait_for_cooldowns, enricher)
            out.append((title or "", await enricher.enrich_story(story_id, dry_run=True)))
        return out

    print(summary(asyncio.run(run()), markdown=args.markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
