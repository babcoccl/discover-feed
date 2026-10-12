"""Run the same stories' briefs and reports through several endpoints; print a Markdown comparison.

    python -m app.summarize.compare --stories 20 --markdown \\
        --endpoints a=http://host:8080/v1:qwen3,b=http://host2:8080/v1:gemma3

Each endpoint is ``name=base_url:model`` (the model is everything after the URL's path, so
``llama3.1:8b`` works). The API key comes from ``LLM_API_KEY_<NAME>`` (name upper-cased), else
``LOCAL_LLM_API_KEY``. Other settings (temperature, structured_output...) come from the
profile's ``llm.summarizer``. ``--kind brief|report|both`` (default both). Nothing is stored.

``--chat`` instead asks the same 5 questions (``CHAT_QUESTIONS``; the last one is not answerable
from any story, so a good model refuses) about each story through every endpoint, using the
profile's ``llm.chat`` settings, and prints time to first token, tokens/sec, verification and
refusal rates and the answers.
"""

import asyncio
import os
import re
import statistics
import sys

from pydantic import SecretStr

from app.quality import percent, quality
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
                gens.append(None if skip else await summ.generate_report(sources, s.title))
            results[name, "report"] = gens

    print(f"# Brief and report comparison ({len(stories)} stories)\n")
    print(
        "| Endpoint | Model | Kind | Pass rate | First-attempt pass | Attempts/success "
        "| Fallback | Failed | Skipped | Median latency | Tokens/sec | Avg lead words "
        "| Avg bullet words | Avg report words | Failure reasons |"
    )
    print("|---" * 15 + "|")
    metrics = {}
    for name, _url, _model in endpoints:
        log = [
            {**a, "story_id": s.story_id}
            for k in kinds
            for s, g in zip(stories, results[name, k], strict=True)
            if g is not None
            for a in g.attempt_log
        ]
        metrics[name] = quality(log)
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
            q = metrics[name].kinds[k]
            reasons = ", ".join(f"{r} x{n}" for r, n in q.top_failures(4)) or "-"
            print(
                f"| {name} | {_cell(model)} | {k} | {rate} | {percent(q.first_pass_rate)} | "
                f"{q.median_attempts if q.median_attempts is not None else '-'} | "
                f"{sum(g.status == 'fallback' for g in gens)} | "
                f"{sum(g.status == 'failed' for g in gens)} | {len(all_gens) - len(gens)} | "
                f"{latency} | {_median(g.tokens_per_second for g in gens)} | "
                f"{lead} | {bullet} | "
                f"{_mean(r.word_count for r in reports) if k == 'report' else '-'} | "
                f"{_cell(reasons)} |"
            )
    print()
    for name, _url, _model in endpoints:
        seconds = metrics[name].story_seconds
        print(
            f"- {name}: median end-to-end {seconds if seconds is not None else '-'} s per story "
            "(brief + report, all attempts)"
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


CHAT_QUESTIONS = (
    "What happened?",
    "Who is involved?",
    "Why does it matter?",
    "What happens next?",
    "What did the sources say about the weather on Mars?",
)


async def run_chat(env, endpoints, n_stories: int) -> int:
    from app.chat.smoke import answer_once, chat_role

    stories = newest_stories(env, n_stories)
    base = chat_role(env)
    results: dict[str, list] = {}
    for name, url, model in endpoints:
        key = os.environ.get(f"LLM_API_KEY_{name.upper()}") or os.environ.get("LOCAL_LLM_API_KEY")
        role = base.model_copy(
            update={"base_url": url, "model": model, "api_key": SecretStr(key) if key else None}
        )
        print(f"{name}: {len(stories)} stories x {len(CHAT_QUESTIONS)} questions", file=sys.stderr)
        results[name] = [
            (s, q, (await answer_once(env, role, s.story_id, q, [], echo=False))[0])
            for s in stories
            for q in CHAT_QUESTIONS
        ]
    print(f"# Chat comparison ({len(stories)} stories x {len(CHAT_QUESTIONS)} questions)\n")
    print(
        "| Endpoint | Model | Complete | Verified | Refused unanswerable | Refused answerable "
        "| Median first token | Median total | Tokens/sec | Avg words | Errors |"
    )
    print("|---" * 11 + "|")
    for name, _url, model in endpoints:
        rows = results[name]
        done = [a for _s, _q, a in rows if a.status == "complete"]
        verified = sum(bool(a.verification and a.verification["verified"]) for a in done)
        last = CHAT_QUESTIONS[-1]
        refusals = [
            (q == last, bool(a.verification and a.verification["refusal"]))
            for _s, q, a in rows
            if a.status == "complete"
        ]
        unanswerable = sum(r for u, r in refusals if u), sum(u for u, _ in refusals)
        answerable = sum(r for u, r in refusals if not u), sum(not u for u, _ in refusals)
        errors = {}
        for _s, _q, a in rows:
            if a.error:
                errors[a.error] = errors.get(a.error, 0) + 1
        tps = [a.done_event()["tokens_per_second"] for a in done]
        print(
            f"| {name} | {_cell(model)} | {len(done)}/{len(rows)} | {verified}/{len(done)} | "
            f"{unanswerable[0]}/{unanswerable[1]} | {answerable[0]}/{answerable[1]} | "
            f"{_median(a.time_to_first_token_ms for a in done)} ms | "
            f"{_median(a.latency_ms for a in done)} ms | {_median(tps)} | "
            f"{_mean(len(a.text.split()) for a in done)} | "
            f"{_cell(', '.join(f'{k} x{v}' for k, v in errors.items()) or '-')} |"
        )
    names = [e[0] for e in endpoints]
    print("\n## Answers\n")
    print("| Story | Question | " + " | ".join(names) + " |")
    print("|---" * (len(names) + 2) + "|")
    for i, (story, question, _a) in enumerate(results[names[0]]):
        cells = []
        for name in names:
            a = results[name][i][2]
            flag = "" if a.verification and a.verification["verified"] else " (unverified)"
            cells.append(_cell((a.text or f"[{a.error}]")[:300] + flag))
        print(f"| {_cell(story.title[:60])} | {question} | " + " | ".join(cells) + " |")
    return 0


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    p = parser(__doc__.splitlines()[0])
    p.add_argument("--endpoints", required=True, help="name=base_url:model,...")
    p.add_argument("--stories", type=int, default=20)
    p.add_argument("--kind", choices=["brief", "report", "both"], default="both")
    p.add_argument("--markdown", action="store_true", help="Markdown output (the default)")
    p.add_argument("--chat", action="store_true", help="compare story Q&A answers instead")
    args = p.parse_args(argv)
    try:
        endpoints = parse_endpoints(args.endpoints)
    except ValueError as exc:
        p.error(str(exc))
    if args.chat:
        return asyncio.run(run_chat(load(args), endpoints, args.stories))
    return asyncio.run(run(load(args), endpoints, args.stories, args.kind))


if __name__ == "__main__":
    raise SystemExit(main())
