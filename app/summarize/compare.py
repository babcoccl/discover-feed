"""Run the same stories through several endpoints and print a Markdown comparison.

    python -m app.summarize.compare --stories 20 --markdown \\
        --endpoints a=http://host:8080/v1:qwen3,b=http://host2:8080/v1:gemma3

Each endpoint is ``name=base_url:model`` (the model is everything after the URL's path, so
``llama3.1:8b`` works). The API key comes from ``LLM_API_KEY_<NAME>`` (name upper-cased), else
``LOCAL_LLM_API_KEY``. Other settings (temperature, structured_output...) come from the
profile's ``llm.summarizer``. Nothing is stored.
"""

import asyncio
import os
import re
import statistics
import sys

from pydantic import SecretStr

from app.summarize._cli import load, marked, newest_stories, parser, utf8_stdout
from app.summarize.service import Summarizer

_ENDPOINT = re.compile(r"^(https?://[^/\s]+(?:/[^:\s]*)?):([^/\s]+)$")


def parse_endpoints(spec: str) -> list[tuple[str, str, str]]:
    endpoints = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        name, sep, rest = part.partition("=")
        match = _ENDPOINT.match(rest)
        if not sep or not name or match is None:
            raise ValueError(f"bad endpoint {part!r}; expected name=http://host:port/v1:model")
        endpoints.append((name, match[1], match[2]))
    return endpoints


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


async def run(env, endpoints, n_stories: int) -> int:
    stories = newest_stories(env, n_stories)
    results = {}
    for name, url, model in endpoints:
        key = os.environ.get(f"LLM_API_KEY_{name.upper()}") or os.environ.get("LOCAL_LLM_API_KEY")
        role = env.role.model_copy(
            update={"base_url": url, "model": model, "api_key": SecretStr(key) if key else None}
        )
        summarizer = Summarizer(env.client(role))
        print(f"{name}: {len(stories)} stories ...", file=sys.stderr)
        results[name] = [await summarizer.generate(s.sources) for s in stories]

    print(f"# Summary comparison ({len(stories)} stories)\n")
    print("| Endpoint | Model | Pass rate | Fallback | Failed | Median latency | Tokens/sec |")
    print("|---|---|---|---|---|---|---|")
    for name, _url, model in endpoints:
        gens = results[name]
        ok = sum(g.status == "ok" for g in gens)
        latency = statistics.median(g.latency_ms for g in gens) if gens else 0
        speeds = [g.tokens_per_second for g in gens if g.tokens_per_second]
        tps = f"{statistics.median(speeds):.1f}" if speeds else "-"
        print(
            f"| {name} | {_cell(model)} | {ok}/{len(gens)} ({ok / len(gens):.0%}) | "
            f"{sum(g.status == 'fallback' for g in gens)} | "
            f"{sum(g.status == 'failed' for g in gens)} | {latency:.0f} ms | {tps} |"
        )
    names = [e[0] for e in endpoints]
    print("\n## Summaries\n")
    print("| Story | " + " | ".join(names) + " |")
    print("|---" * (len(names) + 1) + "|")
    for i, story in enumerate(stories):
        cells = []
        for name in names:
            gen = results[name][i]
            if gen.summary:
                cells.append(_cell(marked(gen.summary, story.sources)))
            else:
                cells.append(_cell(f"**{gen.status}**: {gen.reason}"))
        print(f"| {_cell(story.title)} | " + " | ".join(cells) + " |")
    return 0


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    p = parser(__doc__.splitlines()[0])
    p.add_argument("--endpoints", required=True, help="name=base_url:model,...")
    p.add_argument("--stories", type=int, default=20)
    p.add_argument("--markdown", action="store_true", help="Markdown output (the default)")
    args = p.parse_args(argv)
    try:
        endpoints = parse_endpoints(args.endpoints)
    except ValueError as exc:
        p.error(str(exc))
    return asyncio.run(run(load(args), endpoints, args.stories))


if __name__ == "__main__":
    raise SystemExit(main())
