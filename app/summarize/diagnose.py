"""Diagnose brief/report generation for one story against the configured endpoint.

    python -m app.summarize.diagnose [--story-id N | --auto] [--kind brief|report|both]
                                     [--dry-run] [--markdown]

Prints the story's members (text status, word counts, which are sent), the prompt size, and
for every attempt the raw response, finish reason, usage, tokens/second and validation
result. ``--dry-run`` stops before calling the model. Nothing is written to the database.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

import httpx
from sqlalchemy import select

from app import profiles as repo
from app.config import load_config
from app.db import init_db, make_engine, make_session_factory
from app.models import Story, StoryState
from app.pipeline import summary_worker_from_settings
from app.settings import Settings, get_settings
from app.summarize.prompt import build_messages
from app.summarize.service import estimate_tokens, report_eligibility

RAW_CHARS = 1500


def _pick(session, story_id: int | None) -> int | None:
    if story_id is not None:
        return story_id
    waiting = (StoryState.CLUSTERED, StoryState.BRIEF_READY, StoryState.REPORT_FINAL)
    for where in (Story.state.in_([s.value for s in waiting]), Story.source_count >= 2):
        found = session.scalar(
            select(Story.id).where(where).order_by(Story.last_updated_at.desc()).limit(1)
        )
        if found is not None:
            return found
    return session.scalar(select(Story.id).order_by(Story.last_updated_at.desc()).limit(1))


async def diagnose(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    parser = argparse.ArgumentParser(prog="python -m app.summarize.diagnose")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--story-id", type=int)
    which.add_argument("--auto", action="store_true", help="newest story waiting for generation")
    parser.add_argument("--kind", choices=("brief", "report", "both"), default="both")
    parser.add_argument("--dry-run", action="store_true", help="show the prompt; no LLM call")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)
    settings = settings or get_settings()
    config = load_config(settings.config_path)
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    worker = summary_worker_from_settings(factory, settings, config, transport=transport)
    summarizer = worker.summarizer
    md = args.markdown
    out: list[str] = []

    def head(text: str) -> None:
        out.append(f"\n## {text}\n" if md else f"\n=== {text} ===")

    def line(text: str = "") -> None:
        out.append(text)

    def block(text: str) -> None:
        out.append(f"```\n{text}\n```" if md else "\n".join("    " + t for t in text.splitlines()))

    if summarizer is None:
        engine.dispose()
        return "No summarizer endpoint is configured (profile llm.summarizer)."
    with factory() as session:
        story_id = _pick(session, args.story_id)
        item = repo.get_story(session, story_id) if story_id is not None else None
        names = repo.source_names(session)
        story = session.get(Story, story_id) if story_id is not None else None
    if item is None:
        engine.dispose()
        return f"No story {story_id} with members."
    head(f"Story {story_id}: {item.title}")
    line(f"state: {story.state}{f' ({story.state_reason})' if story.state_reason else ''}")
    line(f"model: {summarizer.client.model} at {summarizer.client.endpoint_host}")
    kinds = ["brief", "report"] if args.kind == "both" else [args.kind]
    for kind in kinds:
        sources = summarizer.sources_for(item, names, kind)
        included = {s.article_id: s for s in sources}
        head(f"{kind}: members")
        for m in item.members:
            sent = included.get(m.id)
            line(
                f"- [{sent.index if sent else ' '}] {names.get(m.source_id, m.source_id)}: "
                f"text_status={m.text_status}, words={m.word_count or 0}"
                + (f", sending {len(sent.text.split())} words" if sent else ", not sent")
            )
        messages = build_messages(sources, kind=kind)
        words = sum(len(str(m["content"]).split()) for m in messages)
        role = summarizer.report_client.role if kind == "report" else summarizer.client.role
        line(
            f"prompt: {words} words, ~{estimate_tokens(words)} tokens; "
            f"context {role.context_tokens} tokens, max_tokens {role.max_tokens}"
        )
        if kind == "report" and not report_eligibility(item, sources):
            line("report: skipped(insufficient_text) - no member has 150+ extracted words")
            continue
        if args.dry_run:
            block(str(messages[-1]["content"])[:RAW_CHARS])
            continue
        gen = await (summarizer.generate_report if kind == "report" else summarizer.generate_brief)(
            sources
        )
        for n, a in enumerate(gen.attempt_log, 1):
            head(f"{kind}: attempt {n} ({a.get('structured_output')})")
            tps = a.get("tokens_per_second")
            line(
                f"outcome={a.get('outcome')} reason={a.get('reason')} "
                f"finish_reason={a.get('finish_reason')} "
                f"usage=prompt {a.get('prompt_tokens')} / completion {a.get('completion_tokens')} "
                f"latency={a.get('latency_ms')}ms"
                + (f" ({tps:.1f} tokens/s)" if tps else "")
                + f" content={a.get('content_length', 0)} chars"
                + f" reasoning={a.get('reasoning_length', 0)} chars"
            )
            for err in a.get("validation_errors") or []:
                line(f"validation: {err}")
            if a.get("outcome") == "ok":
                line("validation: passed")
            block((a.get("raw_content") or "")[:RAW_CHARS] or "(empty)")
        line(f"result: {gen.status}{f' ({gen.reason})' if gen.reason else ''}")
    engine.dispose()
    return "\n".join(out).strip() + "\n"


def main() -> None:
    sys.stdout.write(asyncio.run(diagnose()))


if __name__ == "__main__":
    main()
