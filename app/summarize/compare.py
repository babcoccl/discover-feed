"""Run the same stories' briefs and reports through several endpoints; print a Markdown comparison.

    python -m app.summarize.compare --stories 20 --markdown \\
        --endpoints a=http://host:8080/v1:qwen3,b=http://host2:8080/v1:gemma3

Each endpoint is ``name=base_url:model`` (the model is everything after the URL's path, so
``llama3.1:8b`` works). The API key comes from ``LLM_API_KEY_<NAME>`` (name upper-cased), else
``LOCAL_LLM_API_KEY``. Other settings (temperature, structured_output...) come from the
profile's ``llm.summarizer``. ``--kind brief|report|both`` (default both). Nothing is stored.
"""

import asyncio
import os
import re
import statistics
import sys

from pydantic import SecretStr

from app.summarize._cli import (
    brief_lines,
    cited,
    load,
    newest_stories,
    parser,
    summarizer,
    utf8_stdout,
)

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


def _median(values) -> str:
    values = [v for v in values if v]
    return f"{statistics.median(values):.1f}" if values else "-"


def _mean(values) -> str:
    values = list(values)
    return f"{statistics.mean(values):.1f}" if values else "-"


def _sample(gen, kind: str) -> str:
    if kind == "brief":
        return "<br>".join(_cell(line) for line in brief_lines(gen)) or _cell(
            f"**{gen.status}**: {gen.reason}"
        )
    if gen is None:
        return "*skipped (insufficient_text)*"
    if gen.report is None:
        return _cell(f"**{gen.status}**: {gen.reason}")
    return "<br><br>".join(_cell(p) for p in cited(gen.report.paragraphs))


async def run(env, endpoints, n_stories: int, kind: str = "both") -> int:
    stories = newest_stories(env, n_stories)
    kinds = ["brief", "report"] if kind == "both" else [kind]
    results: dict[tuple[str, str], list] = {}
    for name, url, model in endpoints:
        key = os.environ.get(f"LLM_API_KEY_{name.upper()}") or os.environ.get("LOCAL_LLM_API_KEY")
        role = env.role.model_copy(
            update={"base_url": url, "model": model, "api_key": SecretStr(key) if key else None}
        )
        summ = summarizer(env, env.client(role))
        print(f"{name}: {len(stories)} stories ...", file=sys.stderr)
        if "brief" in kinds:
            results[name, "brief"] = [await summ.generate_brief(s.sources) for s in stories]
        if "report" in kinds:
            gens = []
            for s in stories:
                sources = summ.sources_for(s.item, s.names, "report")
                skip = summ.insufficient_text(s.item, sources)
                gens.append(None if skip else await summ.generate_report(sources))
            results[name, "report"] = gens

    print(f"# Brief and report comparison ({len(stories)} stories)\n")
    print(
        "| Endpoint | Model | Kind | Pass rate | Fallback | Failed | Skipped | Median latency "
        "| Tokens/sec | Avg lead words | Avg bullet words | Avg report words |"
    )
    print("|---" * 12 + "|")
    for name, _url, model in endpoints:
        for k in kinds:
            all_gens = results[name, k]
            gens = [g for g in all_gens if g is not None]
            ok = sum(g.status == "ok" for g in gens)
            briefs = [g.brief for g in gens if getattr(g, "brief", None) is not None]
            reports = [g.report for g in gens if getattr(g, "report", None) is not None]
            rate = f"{ok}/{len(gens)} ({ok / len(gens):.0%})" if gens else "-"
            lead = _mean(len(b.lead.split()) for b in briefs) if k == "brief" else "-"
            bullet = (
                _mean(len(x.text.split()) for b in briefs for x in b.bullets)
                if k == "brief"
                else "-"
            )
            latency = f"{statistics.median(g.latency_ms for g in gens):.0f} ms" if gens else "-"
            print(
                f"| {name} | {_cell(model)} | {k} | {rate} | "
                f"{sum(g.status == 'fallback' for g in gens)} | "
                f"{sum(g.status == 'failed' for g in gens)} | {len(all_gens) - len(gens)} | "
                f"{latency} | {_median(g.tokens_per_second for g in gens)} | "
                f"{lead} | {bullet} | "
                f"{_mean(r.word_count for r in reports) if k == 'report' else '-'} |"
            )
    names = [e[0] for e in endpoints]
    for k in kinds:
        print(f"\n## {k.capitalize()}s\n")
        print("| Story | " + " | ".join(names) + " |")
        print("|---" * (len(names) + 1) + "|")
        for i, story in enumerate(stories):
            cells = [_sample(results[name, k][i], k) for name in names]
            print(f"| {_cell(story.title)} | " + " | ".join(cells) + " |")
    return 0


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    p = parser(__doc__.splitlines()[0])
    p.add_argument("--endpoints", required=True, help="name=base_url:model,...")
    p.add_argument("--stories", type=int, default=20)
    p.add_argument("--kind", choices=["brief", "report", "both"], default="both")
    p.add_argument("--markdown", action="store_true", help="Markdown output (the default)")
    args = p.parse_args(argv)
    try:
        endpoints = parse_endpoints(args.endpoints)
    except ValueError as exc:
        p.error(str(exc))
    return asyncio.run(run(load(args), endpoints, args.stories, args.kind))


if __name__ == "__main__":
    raise SystemExit(main())
