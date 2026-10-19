# Briefs and detailed reports

## What it does

Stories get two LLM-written artifacts (Perplexity Discover style), both with
`[n]` citations that link to each article's own permalink (`Article.url`):

- **Brief** (feed cards and the top of the story page): one lead sentence (at
  most 30 words, what happened) plus exactly three bullets (at most 28 words
  each: what happened, why it matters or context, what's next or a key
  detail). Cards show the image, title, lead, the three bullets, source chips
  and relative time; `card_style: lead_only` hides the bullets.
- **Detailed report** (story page, below the brief): 4-6 paragraphs, about
  300-550 words, paraphrasing the sources, then the numbered **Sources** list.
  Every card opens its story page (single-source cards also have a "Read on
  <source>" link). `[n]` is the same article in the brief, the report and the
  Sources list. The page shows the model and when it was written.

Written by an OpenAI-compatible chat completions endpoint — see
[LLM endpoints](../configuration/llm.md). Only the numbered sources' text goes
into the prompt; extracted article text is never returned by the public API or
rendered in a page.

## How it works (the publish gate)

`app/lifecycle.py`, `app/summarize/worker.py`, `app/publish.py`. The app is
"always on": generation runs in the background and pages only read finished,
published story views. Opening a page never triggers, queues or waits on the
LLM.

- **Story states.** Every article gets a story at ingest, which moves
  `ingested → text_pending → text_final → clustered → brief_ready →
  report_final → published`. `text_final` means extracted text is available or
  unavailable: decided after extraction, or after `extraction_wait_seconds`
  (120).
- **One story at a time, newest first.** The worker takes the highest-priority,
  most recently updated story and runs its brief, then its report (if
  eligible), then the publish decision, before it starts the next story.
  `max_inflight` (1) caps all LLM requests process-wide. Unpublished stories
  older than `summarize_backfill_hours` (24) are skipped and at most
  `summarize_backfill_max` (40) unpublished stories are worked on; published
  stories that gained members are always refreshed.
- **Report eligibility** is decided without the LLM: at least one included
  member with `text_status=ok` and 150+ extracted words. Otherwise the report
  is final as `skipped(insufficient_text)` with each member's status and word
  count. Report failures back off their own job and never pause briefs; brief
  endpoint failures (`DISCOVER_SUMMARIZE_FAILURE_LIMIT`, 3 in a row) pause the
  worker for `DISCOVER_SUMMARIZE_PAUSE_MINUTES` (10).
- **Publish rule.** A story publishes once its brief is valid and its report
  is final (`ready`, `skipped(reason)` or `failed(reason)`). With
  `publish_requires_report: false` it publishes as soon as the brief is valid.
  After `publish_max_wait_minutes` (30) it publishes with the report pending
  ("Detailed report still generating"). If the brief fails for good, the story
  publishes after `brief_fallback_hours` (6) with the feed snippet, labelled
  `Summary unavailable: <reason>`.
- **Story views.** Every change writes a new version to `story_views
  (story_id, version, schema_version, state, published_at, doc JSON,
  updated_at)` in the same transaction as the summary row.
  `stories.published_version` moves only when the new version meets the publish
  rule, so the old version stays on screen while a replacement is generated.
  One document per story; the profile's sources are filtered when it is read.
  Existing databases are migrated in place on startup (idempotent).
- **Live arrivals.** Every publish, update and retraction is stored in
  `story_events` (kept `event_retention_days`, 7) and streamed by
  `GET /api/v1/stream` (see [Public API](../reference/api.md)); the pages still
  use the id-only `GET /api/profiles/{slug}/stream` (`story.published` /
  `story.updated` with `{story_id, version}` and a `heartbeat` event). The feed
  shows "N new stories — click to load" without moving the grid; without SSE it
  polls `GET /api/profiles/{slug}/published?since=<iso>` every 30 s. An open
  story page offers a reload when a newer version is published.
- **Before the first story.** The feed says "Preparing your first stories..."
  with live counts (ingested, extracting, clustering, summarizing N of M) and
  names what blocks generation: no endpoint set, endpoint unreachable, HTTP 401
  or the worker paused.
- **Generation attempts.** `generation_attempts` keeps the latest 500 LLM
  calls: story, kind, model, endpoint host (never the key), member text
  statuses and word counts, input words, estimated tokens, usage, finish
  reason, latency, content and reasoning length, the raw content (truncated),
  validation errors, outcome and reason (`empty_content`, `truncated`,
  `reasoning_consumed_budget`, `timeout`, `http_<status>`, `invalid_json`,
  `validation:<rule>`, `insufficient_text`). A warning is logged when thinking
  is disabled but the model still returns reasoning.
- **Prompt size.** `llm.summarizer.context_tokens` (16384) is the model's
  context window; report inputs are trimmed so the answer
  (`report_max_tokens`) still fits. After the usual validation retry, a last
  attempt asks for `structured_output: json_object` and extracts the JSON
  leniently; citation, number and verbatim-copy checks still apply. Retries
  quote the exact phrases that were copied, ask for a different sentence
  structure and run `retry_temperature_bump` (0.2) hotter. Raw newlines/tabs
  inside JSON strings become spaces (no glued words); suspected glued words
  (`tasksAI`, `AIworkloads`) are logged as attempt `warnings`, never rejected.
- **Input.** Briefs use up to `DISCOVER_SUMMARIZE_MAX_ARTICLES_PER_STORY` (5)
  members and the first `DISCOVER_SUMMARIZE_MAX_WORDS_PER_ARTICLE` (1200) words
  of each; reports use up to `report_max_articles` (4) members, one per source
  first, sharing `report_total_words` (2400) source words (a one-source story
  gets all of them; a short article's unused share goes to the others), keeping
  the brief's numbering.
- **Validation.** Briefs are rejected for a wrong bullet count, a lead that is
  not one sentence, length violations, bad citations, numbers not in a cited
  source, or near-duplicate bullets (`duplicate_threshold`, 0.6). Reports are
  rejected for fewer than 4 or more than 6 paragraphs, fewer than 270 or more
  than 605 words (the 300-550 target plus 10%), uncited paragraphs, bad
  citations, invented numbers, duplicate paragraphs, a run of
  `verbatim_max_run` (12) or more words copied from a source (case,
  punctuation and hyphens ignored), or more than `verbatim_max_ratio` (20%) of
  the report's 8-word phrases found in the sources. Copied runs made only of
  story-title words, numbers or `verbatim_allow` terms don't count. A brief
  that keeps failing falls back to a 2-3 sentence summary, then to the feed
  snippet.
- Every version is kept in `story_summaries`; `input_hash` covers member ids,
  source text hashes, prompt version, model and kind, so unchanged input means
  no LLM call.

## Operate

- **Pipeline panel** (Settings > Pipeline): every unpublished story with its
  state and reason and an ETA from the median brief/report time, **Prioritize**
  and **Retry** per story, **Pause/Resume generation**, the last 20 generation
  attempts, and **Generation quality** over the last 500 attempts: first-attempt
  pass rate per kind, median attempts per success, median end-to-end model
  seconds per story (brief + report) and the failure reasons.
- **Regenerate** and **Retry** queue a new version; the current one stays
  visible until replaced.
- `POST /api/admin/summarize` (`?story_id=`, `?kind=brief|report|both`,
  `?force=true`, `?wait=true`) queues generation.
- API: story feed items and `GET /api/stories/{id}` (published only) carry
  `version`, `published_at`, `brief` (`{lead, lead_citations, bullets, status,
  style, basis, model, generated_at, sources, label}`), `report`
  (`{paragraphs, state, status (deprecated), reason, label, details, model,
  generated_at, sources}`) and the deprecated `summary`.

### Diagnosing a slow or empty report

```bash
python -m app.summarize.diagnose --auto --kind both            # newest story waiting
python -m app.summarize.diagnose --story-id 42 --kind report --markdown
python -m app.summarize.diagnose --auto --dry-run              # prompt only, no LLM call
python -m app.summarize.diagnose --auto --kind report --full   # uncut prompts and answers
```

(Windows: `.venv\Scripts\python -m app.summarize.diagnose --auto`.) It prints
the members with text status and word counts, which are sent, the prompt size
against `context_tokens`, then each attempt's raw response, finish reason,
usage, tokens/second, validation result and warnings, then the validated text
exactly as it would be stored. Prompts and answers are cut to 1,500 characters
unless `--full`. Nothing is written to the database.

`python -m app.summarize.compare` reports the same quality metrics as the
Pipeline panel per endpoint.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "Summary unavailable" | Brief failed past `brief_fallback_hours` | Check `generation_attempts` reasons; Retry from Pipeline |
| "Report unavailable" / `skipped(insufficient_text)` | No member extracted 150+ words | Normal for thin sources; check `text_status` |
| Worker paused | `DISCOVER_SUMMARIZE_FAILURE_LIMIT` endpoint failures | Fix the endpoint; **Resume generation** |
| `validation:verbatim` retries | Model copies source wording | It usually recovers hotter; raise `verbatim_max_run` only if the model truly can't paraphrase |
| `reasoning_consumed_budget` | Thinking model ate the token budget | Keep `disable_thinking: true`, or raise `max_tokens` |
