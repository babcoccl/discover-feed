"""Ask one question about a stored story with the profile's chat role and print the result.

    python -m app.chat.smoke --story-id 12 --question "When does it ship?" [--history Q A ...]

Nothing is stored. Prints time to first token, tokens/sec, the streamed answer, the
citations and the verification checks. Add --fake to run offline, --db .demo/demo.db for the
demo's stories.
"""

import asyncio
import sys
import time

from app.chat.context import chat_context
from app.chat.prompt import build_chat_messages
from app.chat.service import Answer, stream_answer
from app.llm import LLMClient
from app.summarize._cli import load, parser, utf8_stdout
from app.summarize.fake import FakeLLM


def chat_role(env):
    """The profile's ``llm.chat`` role, else its summarizer."""
    return env.chat or env.role


async def answer_once(env, role, story_id: int, question: str, history, *, echo=True):
    """``(Answer, ChatContext)`` for one question (used by compare --chat)."""
    with env.session_factory() as session:
        ctx = chat_context(
            session, story_id, question, role=role, options=env.options, history=history
        )
    if ctx is None:
        sys.exit(f"story {story_id} not found in {env.settings.database_url}")
    transport = FakeLLM().transport() if env.fake else None
    client = LLMClient(role, transport=transport)
    answer = Answer(model=role.model)
    started = time.perf_counter()
    async for name, data in stream_answer(
        client, build_chat_messages(ctx, question), ctx.sources, question, answer,
        shared_slot=False, started=started,
    ):  # fmt: skip
        if echo and name == "token":
            print(data["text"], end="", flush=True)
        elif echo and name == "waiting":
            print("[waiting for the model...]", flush=True)
    return answer, ctx


async def run(env, story_id: int, question: str, history) -> int:
    role = chat_role(env)
    print(f"Endpoint {role.base_url}  model {role.model}  story {story_id}\n")
    print(f"Q: {question}\nA: ", end="", flush=True)
    answer, ctx = await answer_once(env, role, story_id, question, history)
    print("\n")
    done = answer.done_event()
    print(
        f"status: {answer.status}{f' ({answer.error}: {answer.detail})' if answer.error else ''}\n"
        f"time to first token: {answer.time_to_first_token_ms} ms  total: {answer.latency_ms} ms  "
        f"tokens: {answer.tokens}  tokens/sec: {done['tokens_per_second'] or '-'}\n"
        f"context: {len(ctx.sources)} sources, {ctx.source_words} words, "
        f"{len(ctx.history)} earlier turns ({ctx.dropped_turns} dropped)"
    )
    for c in answer.citations:
        via = f" via {c['via']}" if c["via"] else ""
        print(f"  [{c['index']}] {c['publisher']}{via}: {c['headline']}\n      {c['url']}")
    if answer.verification:
        v = answer.verification
        print(f"verified: {v['verified']}  refusal: {v['refusal']}")
        for check in answer.verification["checks"]:
            mark = "ok" if check["ok"] else check["level"].upper()
            print(f"  {mark:<8}{check['name']} {check['detail']}")
    return 0 if answer.status == "complete" else 1


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    p = parser(__doc__.splitlines()[0])
    p.add_argument("--story-id", type=int, required=True)
    p.add_argument("--question", required=True)
    p.add_argument(
        "--history", nargs="*", default=[], help="earlier turns: question answer question ..."
    )
    args = p.parse_args(argv)
    if len(args.history) % 2:
        p.error("--history takes question/answer pairs")
    env = load(args)
    pairs = list(zip(args.history[::2], args.history[1::2], strict=True))
    return asyncio.run(run(env, args.story_id, args.question, pairs))


if __name__ == "__main__":
    raise SystemExit(main())
