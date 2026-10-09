"""Generate briefs and/or reports for a few current stories and print the results.

    python -m app.summarize.smoke --profile personal-reader --limit 5 --kind both

Nothing is stored. Prints each brief (lead + three bullets) and report (paragraphs) with
their [n] citations, latency, tokens/sec, word counts and the validation result (ok /
fallback or failed with the reason / skipped when there is not enough text for a report).
"""

import asyncio

from app.summarize._cli import (
    brief_lines,
    cited,
    load,
    newest_stories,
    parser,
    summarizer,
    utf8_stdout,
)


def _result(gen) -> str:
    tps = f"{gen.tokens_per_second:.1f}" if gen.tokens_per_second else "-"
    return (
        f"result: {gen.status}  attempts: {gen.attempts}  latency: {gen.latency_ms} ms  "
        f"tokens/sec: {tps}  model: {gen.model}"
    )


async def run(env, limit: int, kind: str = "both") -> int:
    summ = summarizer(env)
    stories = newest_stories(env, limit)
    print(f"Endpoint {env.client().url}  model {env.role.model}  ({len(stories)} stories)\n")
    kinds = ["brief", "report"] if kind == "both" else [kind]
    passed = {k: 0 for k in kinds}
    for n, story in enumerate(stories, 1):
        sources = summ.sources_for(
            story.item, story.names, "report" if "report" in kinds else "brief"
        )
        print(f"## {n}. {story.title}  (story {story.story_id}, {len(sources)} sources)")
        if "brief" in kinds:
            gen = await summ.generate_brief(story.sources)
            passed["brief"] += gen.status == "ok"
            print("### Brief")
            for line in brief_lines(gen):
                print(f"  {line}")
            words = ""
            if gen.brief is not None:
                bullets = [len(b.text.split()) for b in gen.brief.bullets]
                words = f"  lead words: {len(gen.brief.lead.split())}  bullet words: {bullets}"
            print(f"  {_result(gen)}{words}")
            if gen.reason:
                print(f"  reason: {gen.reason}")
        if "report" in kinds:
            print("### Report")
            if summ.insufficient_text(story.item, sources):
                passed["report"] += 0
                print("  result: skipped (insufficient_text)")
            else:
                gen = await summ.generate_report(sources)
                passed["report"] += gen.status == "ok"
                if gen.report is not None:
                    for paragraph in cited(gen.report.paragraphs):
                        print(f"  {paragraph}\n")
                words = f"  words: {gen.report.word_count}" if gen.report is not None else ""
                print(f"  {_result(gen)}{words}")
                if gen.reason:
                    print(f"  reason: {gen.reason}")
        for s in sources:
            print(f"  [{s.index}] {s.source}: {s.headline}\n      {s.url}")
        print()
    for k in kinds:
        print(f"{k}: {passed[k]}/{len(stories)} passed validation")
    return 0


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    p = parser(__doc__.splitlines()[0])
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--kind", choices=["brief", "report", "both"], default="both")
    args = p.parse_args(argv)
    return asyncio.run(run(load(args), args.limit, args.kind))


if __name__ == "__main__":
    raise SystemExit(main())
