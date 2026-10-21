# Workflow diagnostics

**Settings > Workflow** (admin only) shows what the app actually does at every stage —
system-wide and per story — without changing pipeline behavior. It answers "what is the
app doing right now?", "where does time go?", "what is failing and retrying?", and
"which stages produce weak results?". Rows are written by a batched background writer
that can never block or break the pipeline; a DB hiccup just drops telemetry rows.

## The page

The overview polls every 6s and covers five sections:

- **Pipeline flow** — per stage (ingest, extract, cluster, enrich, context,
  perspectives, brief, report, followups, publish_gate, regenerate, images, alerts):
  items waiting, currently running, finished/failed in the window, median and p95
  duration, the oldest waiter, and the top failure reason. Waiting counts come from the
  real work queues (summary jobs, enrichment/context/text status), not from events.
- **LLM usage (1h / 24h / 7d)** — calls, failures, validation rejects, LLM-busy vs
  queue-wait time, utilization against the concurrency slot, tokens in/out, plus a
  per-`purpose` table (median/p95 latency, queue wait, tokens/sec) and the slowest
  individual calls.
- **Backlogs** — oldest item per queue with its age, so a stuck queue stands out.
- **Story funnel** — ingest → published conversion with per-stage failure reasons and
  the distribution of attempts per story (retries).
- **Insights** — rule-based one-liners: where LLM time goes, stages that fail a lot,
  heavy validation-reject purposes, stories needing many attempts. The LLM-busy
  breakdown is also drawn as a stacked bar chart (inline SVG, no JS dependencies).

Every `llm_calls`/`stage_events` row records the app version (git SHA) and a config
hash so you can correlate a slowdown with a deploy or a settings change.

## Story inspector

**Settings > Workflow > Story** (or "inspect workflow" from an admin story page, or
search by id, headline or URL) shows one story's timeline: a Gantt of every stage
event and LLM call, queue-wait bars before each call, and expandable rows with the
model, attempt, tokens and — per `telemetry.store_payloads` — prompt/response
previews. A summary bar splits wall time into active LLM work, waiting-for-LLM and
other stage work, so "the LLM is slow" vs "the story waited in a queue" is
distinguishable at a glance.

## Configuration

See [Telemetry](../configuration/reference.md#telemetry). The Settings > Workflow page
has a toggle for `telemetry.enabled` that persists in the database.

## CLI and API

- `python -m app.telemetry.report --since 24h --markdown` — the same summary as a
  markdown report; `--story-id N` traces one story. Inside Docker:
  `docker exec discover-feed python -m app.telemetry.report --since 24h --markdown`.
- `GET /api/v1/admin/telemetry/{summary,llm,funnel,stories/{id}/trace}` — admin key.

## Overhead and privacy

Emitting a row is a queue put (microseconds); a daemon thread batches inserts. The
target is <1% pipeline overhead. `telemetry.retention_days` (14) prunes both tables
daily. Payload previews hold article text: they stay local, are admin-only in the UI,
and are never exposed by the public API — `store_payloads: none` disables them.
