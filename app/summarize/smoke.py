"""Summarize a few current stories with the configured endpoint and print the results.

    python -m app.summarize.smoke --profile personal-reader --limit 5

Nothing is stored. Prints each summary with its [n] citations, latency, tokens/sec and the
validation result (ok / fallback with the reason / failed with the endpoint error).
"""

import asyncio

from app.summarize._cli import load, marked, newest_stories, parser, utf8_stdout
from app.summarize.service import Summarizer


async def run(env, limit: int) -> int:
    summarizer = Summarizer(env.client())
    stories = newest_stories(env, limit)
    print(f"Endpoint {env.client().url}  model {env.role.model}  ({len(stories)} stories)\n")
    passed = 0
    for n, story in enumerate(stories, 1):
        gen = await summarizer.generate(story.sources)
        passed += gen.status == "ok"
        tps = f"{gen.tokens_per_second:.1f}" if gen.tokens_per_second else "-"
        print(f"## {n}. {story.title}  (story {story.story_id}, {len(story.sources)} sources)")
        if gen.summary:
            print(marked(gen.summary, story.sources))
        for s in story.sources:
            print(f"  [{s.index}] {s.source}: {s.headline}\n      {s.url}")
        print(
            f"  result: {gen.status}  attempts: {gen.attempts}  latency: {gen.latency_ms} ms  "
            f"tokens/sec: {tps}  model: {gen.model}"
        )
        if gen.reason:
            print(f"  reason: {gen.reason}")
        print()
    print(f"{passed}/{len(stories)} passed validation")
    return 0


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    p = parser(__doc__.splitlines()[0])
    p.add_argument("--limit", type=int, default=5)
    args = p.parse_args(argv)
    return asyncio.run(run(load(args), args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
