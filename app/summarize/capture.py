"""Save one raw chat/completions response from the configured endpoint as a test fixture.

    python -m app.summarize.capture --out tests/fixtures/llm/real_response.json

Sends the summary prompt for the newest multi-source story. The file holds the request body
(no API key) and the endpoint's raw JSON response; ``tests/test_summarizer.py`` checks that
it parses.
"""

import asyncio
import json
from pathlib import Path

from app.llm import build_payload
from app.summarize._cli import load, newest_stories, parser, utf8_stdout
from app.summarize.prompt import RESPONSE_SCHEMA, build_messages
from app.summarize.validate import SummaryInvalid, validate_summary


async def run(env, out: Path) -> int:
    story = newest_stories(env, 1)[0]
    messages = build_messages(story.sources)
    client = env.client()
    result = await client.chat(messages, json_schema=RESPONSE_SCHEMA, schema_name="story_summary")
    request = build_payload(
        env.role, messages, json_schema=RESPONSE_SCHEMA, schema_name="story_summary"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"request": request, "response": result.raw}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    try:
        validate_summary(result.content, [s.grounding for s in story.sources])
        verdict = "passes validation"
    except SummaryInvalid as exc:
        verdict = f"fails validation: {exc}"
    print(f"Saved {out} ({result.latency_ms} ms, model {result.model}); the answer {verdict}")
    return 0


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    p = parser(__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, default=Path("tests/fixtures/llm/real_response.json"))
    args = p.parse_args(argv)
    return asyncio.run(run(load(args), args.out))


if __name__ == "__main__":
    raise SystemExit(main())
