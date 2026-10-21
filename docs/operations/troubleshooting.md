# Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Feed stuck on "Preparing your first stories" | Endpoint unset/unreachable, HTTP 401, or worker paused — the banner names it | Fix `llm.summarizer` or `.env`; **Resume generation** in Pipeline |
| HTTP 401 from TinyFish | `TINYFISH_API_KEY` missing or invalid | Set the key in `.env`; TinyFish rests 1 h then retries — enrichment keeps working via fallbacks or retries the story |
| Stories `enrich_reason = provider_unavailable` / lots of 429s | Provider rate limit or cooldown | Wait for the cooldown (Pipeline shows it); raise `min_interval_seconds`, set `daily_request_budget`; `evaluate --wait` waits it out |
| `skipped:no_signal` on real news | Headline has no names/numbers | Rare; lower `min_signal_tokens` to 0 to disable |
| `skipped_context_budget`-style reasons | `context.daily_request_budget` spent | Raise it or wait for the next UTC day; context uses `context:<name>` usage rows |
| Report rejected for copied text ("verbatim") | Model paraphrases poorly (`verbatim_max_run` 12 / `verbatim_max_ratio` 0.20) | Retries run hotter automatically; check `generation_attempts` for `validation:*` reasons; tune `verbatim_*` or the model |
| Config change ignored | Sources/topics/rules live in the DB after first seed | Edit in Settings/API, or reseed (`docker compose down -v`) — blocks like `summaries:`/`enrichment:` re-read every start |
| `docker compose restart` didn't pick up changes | `restart` doesn't rebuild the image or re-read the compose file | `docker compose up -d --build` for upgrades; `restart` suffices for mounted `profiles.yaml` edits |
| `UnicodeDecodeError` at startup | `profiles.yaml` saved/rewritten in a non-UTF-8 encoding (PowerShell `Set-Content`) | Copy with `cp`/`scp`; save as UTF-8 |
| Unrelated articles merged | `DISCOVER_CLUSTER_THRESHOLD` too loose | Raise it / `MIN_SHARED_TOKENS`; add the pair to fixtures; `--rebuild` |
| Related articles split | Threshold too strict | Lower it; check near-miss cosines in `evaluate --markdown` |
| `DISCOVER_EXTRACT_INTERVAL_MINUTES` has no effect | Renamed: it's `DISCOVER_PIPELINE_INTERVAL_SECONDS` (60) now | Use the new name |
| Source auto-disabled | `DISCOVER_SOURCE_AUTO_DISABLE_FAILURES` (10) consecutive failures | Fix the feed, then **Re-enable** in Settings > Sources |
| `429 Too Many Requests` building the image | Docker Hub rate limit | `docker pull mirror.gcr.io/library/python:3.12-slim && docker tag mirror.gcr.io/library/python:3.12-slim python:3.12-slim`, rebuild |

More per feature: [sources](../features/sources.md#troubleshooting) ·
[extraction](../features/text-extraction.md#troubleshooting) ·
[clustering](../features/clustering.md#troubleshooting) ·
[summaries](../features/summaries.md#troubleshooting) ·
[enrichment](../features/enrichment/providers.md#troubleshooting) ·
[alerts](../features/alerts.md#troubleshooting) ·
[images](../features/images.md#troubleshooting).

Workflow questions ("what is the app doing?", "why is this story slow?") have their own
page: [Settings > Workflow](workflow.md) shows per-stage queues and durations, the story
inspector shows one story's LLM calls and stage timeline.
