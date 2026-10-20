# Background and context links — overview

## What it does

Separate from enrichment: for each eligible published story the context finder
finds pages from *other* sources that add background — **related, never
coverage of the same event**. Two modes:

- **`query_mode: planner` (default)** — a planner LLM picks up to
  `context.max_tasks` (3) research tasks from a fixed catalog (history,
  quotes, financials, related developments, background), each naming a story
  entity; deterministic retrieval recipes turn each task into dated window
  queries (news first, then web; older windows for history mark items
  `origin = archive`); a verified per-page extractor produces a fact + a
  verbatim evidence quote (rejected when the quote isn't on the page, its
  numbers aren't, or a dated page is too recent to be history); one composer
  call writes a ≤30-word note per item and, for history items with verified
  earlier + current quotes, a "then and now" comparison (`changed` |
  `expanded` only — `consistent` stays timeline-only and loaded words like
  "reversal" are rejected unless they're in a cited quote). The story page
  shows them as **Additional detail** grouped by type, and API v1 exposes
  `story.context[]` (type/subject/fact/quote/date/origin) +
  `story.then_and_now`.
- **`query_mode: angles`** — the Phase-14 pipeline (queries per angle,
  similarity band, same-event signals, optional LLM judge) surfacing 1-3
  "Background and context" links.

They are stored as `Article` rows with `origin = "context"` (plus
`context_task` / `context_fact` / `context_quote` / `context_origin` in
planner mode, `context_note` / `context_angle`) and are excluded everywhere
enrichment is excluded — source counts, clustering, alerts, brief/report
inputs, citations, word budget, Q&A — *and additionally from the story's
member set itself*, so they can never influence the published document.

## How it works

`app/context/`:

1. Once a story's brief is published, the story becomes eligible
   (`context_status` NULL → `pending` → `done`): `priority >
   context.min_priority` **or** at least `context.min_publishers` distinct
   publishers. The finder runs in a background thread after the enrichment
   pass inside `cluster()` — it never blocks briefs and runs once per story
   (`context_attempts`, `context_next_attempt_at`); provider failures leave it
   in `retry` with backoff (2 min → 60 min), never `done`. The worker waits
   for the enrichment pass to finish and for provider cooldowns to end
   (provider slots and cooldowns are shared state) before touching a story —
   up to 15 minutes, otherwise the pass pauses and stories stay `pending`
   rather than burning into `retry`. Every story that kept nothing gets a
   `context_reason` (`no_angles`, `skipped_cooldown`, `provider_errors:<kind>`,
   `no_hits`, `all_rejected`), logged per story and counted in
   `last_run.zero_candidates`.
2. `app/context/angles.py` asks the summarizer LLM for up to `max_angles` (3)
   search angles — `label` + a 3-5 term keyword query (entities quoted). If
   the LLM is absent or answers unparseably, the heuristic fallback pairs the
   top story entity with "history" / "explained" / "background" (queries
   marked heuristic).
3. Each provider × angle is searched through the shared `SearchService` (same
   cooldowns, pacing and `search:<name>` budgets as enrichment); context
   additionally enforces its own `context.daily_request_budget` (tracked as
   `context:<name>` usage rows). Providers that implement `find_similar()`
   (the optional `exa` provider, `EXA_API_KEY`) also pull semantically-similar
   pages for the anchor URL.
4. `app/context/filters.py` rejects before and after fetching — see
   [configuration](configuration.md#filters).
5. With `llm_judge` on, an LLM judges each surviving page — see
   [configuration](configuration.md#the-llm-judge).
6. Accepted pages become `origin="context"` articles pinned to the story
   (`discovered_for_story_id` + `anchor_article_id`), shown on the story page
   with their domain, angle label and note. Pages published before the story
   rank first among accepted links (a real backgrounder wins over newer
   pages). API v1 exposes them as `GET /api/v1/stories/{id}` → `context[]`
   (url, title, domain, note, angle, kind). Every decision is persisted in
   `context_candidates` (provider, query, angle, decision, reason, flagged,
   is_opinion, scores, judge verdict).

## Operate

Settings > Pipeline shows the context toggle plus live stats (eligible /
waiting / done / stories with links / links added / judge accept rate /
rejections by reason) and a "Find context now" button.
`POST /api/admin/context[?story_id=N]` triggers a run for all eligible
stories or a single one (admin key).
