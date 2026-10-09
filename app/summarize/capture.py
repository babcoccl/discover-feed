"""Save one raw chat/completions response from the configured endpoint as a test fixture.

    python -m app.summarize.capture --out tests/fixtures/llm/real_response.json

Sends the ``--kind brief|report|summary`` prompt (default brief) for the newest multi-source
story. The file holds the kind, the request body (no API key) and the endpoint's raw JSON
response; ``tests/test_summarizer.py`` checks that it parses.
"""

import asyncio
import json
from pathlib import Path

from app.llm import build_payload
from app.summarize._cli import load, newest_stories, parser, summarizer, utf8_stdout
from app.summarize.prompt import PROMPTS, build_messages
from app.summarize.validate import (
    SummaryInvalid,
    validate_brief,
    validate_report,
    validate_summary,
)

VALIDATORS = {"brief": validate_brief, "report": validate_report, "summary": validate_summary}


async def run(env, out: Path, kind: str = "brief") -> int:
    story = newest_stories(env, 1)[0]
    sources = story.sources
    role = env.role
    if kind == "report":
        sources = summarizer(env).sources_for(story.item, story.names, "report")
        role = role.model_copy(update={"max_tokens": env.options.report_max_tokens})
    messages = build_messages(sources, kind=kind)
    schema, name = PROMPTS[kind][1], f"story_{kind}"
    client = env.client(role)
    result = await client.chat(messages, json_schema=schema, schema_name=name)
    request = build_payload(role, messages, json_schema=schema, schema_name=name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"kind": kind, "request": request, "response": result.raw}, indent=2, ensure_ascii=False
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        VALIDATORS[kind](result.content, [s.grounding for s in sources])
        verdict = "passes validation"
    except SummaryInvalid as exc:
        verdict = f"fails validation: {exc}"
    print(f"Saved {out} ({result.latency_ms} ms, model {result.model}); the answer {verdict}")
    return 0


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    p = parser(__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, default=Path("tests/fixtures/llm/real_response.json"))
    p.add_argument("--kind", choices=["brief", "report", "summary"], default="brief")
    args = p.parse_args(argv)
    return asyncio.run(run(load(args), args.out, args.kind))


if __name__ == "__main__":
    raise SystemExit(main())
