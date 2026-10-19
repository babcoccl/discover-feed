# Story Q&A

## What it does

Each story page has an **Ask a follow-up** panel (under the report) answered by
the profile's `llm.chat` role, only from the story's numbered sources, with
`[n]` citations. Without `llm.chat` (or while its endpoint is unreachable) the
panel is disabled with an explanation.

## How it works

- **Context**: up to 5 sources numbered like the brief, sharing
  `chat_context_words` (3000) words, plus the published brief and report and
  the last `chat_history_turns` (6) turns; trimmed to `llm.chat.context_tokens`
  (oldest turns, then the report, then the sources). Source text is wrapped in
  `<source>` blocks marked untrusted and is never returned by any endpoint.
- **Verification** (afterwards, no retries): `[n]` in range, numbers present in
  the sources, and (warning) uncited factual sentences; the page shows "Some
  details could not be verified".
- **Conversations** have no accounts: the id is the key (kept in localStorage
  per story); idle ones are deleted after `chat_retention_days` (30).
- **Priority**: while a chat request waits or streams, the generation worker
  starts no new LLM request (`chat_priority: true`; only when `llm.chat` uses
  the summarizer's endpoint); a running brief/report is never aborted.
  `chat_max_concurrent` (1) caps chats per profile. Closing the connection
  (Stop, closed tab) closes the upstream request so llama.cpp stops generating.
- **Suggested follow-ups** (`summaries.followups: true`, needs `llm.chat`): 3
  questions of at most 12 words, generated with the summarizer only when no
  brief or report is waiting; not part of the publish gate.

## llama-server sizing

`-np` slots share `-c`. With `-np 1` chat and generation take turns (chat
first). `-np 2 -c 32768` gives each 16k so chat never waits for a report; then
set `summaries.max_inflight: 2`. See [LLM endpoints](../configuration/llm.md).

## Operate

API (same auth as `/api/v1`):

- `POST /api/v1/stories/{id}/ask?profile=` with
  `{"question": "...", "conversation_id": null}` streams SSE events `meta`,
  `waiting` (model busy), `token`…, then `citations` + `done` (verification,
  tokens, latency, time to first token) or `error` (`timeout`, `model_busy`,
  `unreachable`, `upstream_error`, `empty_output`, `http_<status>`).
- `GET|DELETE /api/v1/conversations/{id}`
- `GET /api/v1/stories/{id}/followups` (3 suggested questions)
- `GET /api/v1/chat` (availability)

Example client: `examples/ask.py`.

Try it with the real model (never in CI):

```bash
python -m app.chat.smoke --story-id N --question "..." [--history Q A ...] [--db .demo/demo.db]
python -m app.summarize.compare --chat --endpoints ... --stories 5
```

(Windows: `.venv\Scripts\python -m app.chat.smoke ...`.) `smoke` prints time to
first token, tokens/s, the answer, citations and checks; `compare --chat` asks
5 canned questions per story (the last is unanswerable) across endpoints.
Demo states: `make demo DEMO_ARGS="--fake-chat-mode stream_error"` (or
`invented_number`, `stall`, ...).
