# Statuses and reason codes

## Story lifecycle (`stories.state`)

`ingested` → `text_pending` → `text_final` → `clustered` → `brief_ready` →
`report_final` → `published`; `merged` redirects to the kept story id.
`text_final` means extraction finished (ok or not); decided after
`summaries.extraction_wait_seconds` (120) if pending.

## `articles.text_status`

| Status | Meaning |
| --- | --- |
| `pending` | Not fetched yet |
| `ok` | Text extracted |
| `failed` | HTTP error, too big, not HTML, < `DISCOVER_EXTRACT_MIN_WORDS` |
| `skipped` | robots.txt disallowed |
| `none` | Link-only enrichment member (never summarized) |

## `stories.enrich_status` / `enrich_reason`

| Value | Meaning |
| --- | --- |
| `pending` | Admitted; being searched (brief waits up to `wait_seconds`) |
| `done` | Searched (with or without additions) |
| `retry` | No provider answered — `enrich_next_attempt_at` backoff |
| `skipped:pattern` / `skipped:no_signal` / `skipped:url_pattern` | Not searched ([rules](../features/enrichment/queries.md#skips)) |
| `provider_unavailable: <error>` | All providers failed/cooling down |
| `degraded: <provider> unavailable: <error>` | Only a fallback answered; retried (max 3) |
| `late_regen_deferred` | Extra sources kept for the next regeneration |
| `recovered after N failed attempt(s)` | Succeeded after retries |

## `stories.context_status` / `context_reason`

`NULL → pending → done | retry`. Reasons for empty results: `no_angles`,
`skipped_cooldown`, `provider_errors:<kind>`, `no_hits`, `all_rejected` —
see the [context reasons table](../features/context/operating.md#reasons-table).

## Candidate rejection reasons

`enrichment_candidates` / `context_candidates`: `same_domain`, `social`,
`aggregator`, `denied`, `already_ingested`/`duplicate_url`, `bad_url`, `time`,
`language`, `below_threshold` (`snippet`/`title`/`text`), `near_duplicate`
/`duplicate`, `robots`, `fetch`, `paywalled`, `off_topic`, `same_event`
(+ `same_event_signal`: `ceiling`, `numbers`, `date_band`, `entities_verb`,
`judge`), `flagged`, `domain_cap`, `opinion_cap`, `not_article`,
`judge_rejected`, `judge_same_event`, `judge_failed`.

## Enrichment `via` (acceptance rule)

`text` (rule A) / `snippet+b` / `entities+c` / `title+d` / `link` (link-only).

## Report `state` / `status`

`ready` | `pending` | `skipped(reason)` | `failed(reason)`;
`insufficient_text`, `reports_off`. Attempt `reason`s: `empty_content`,
`truncated`, `reasoning_consumed_budget`, `timeout`, `http_<status>`,
`invalid_json`, `validation:<rule>`, `insufficient_text`.

## `image_source`

`feed | page | member | logo | none` on articles and stories. Image rejection
`reason`s in `images`: `too_large`, `not_image`, `svg`, `ico`,
`tracking_pixel`, `too_small`, `decompression_bomb`, `private_address`,
`bad_scheme`, `too_many_redirects`, `http_<status>`.

## Alert notification `status`

`queued`, `sent`, `held` (quiet hours/rate limit → digest), `failed`
(`max_attempts` reached; **Retry** in the log).

## `story_events.type`

`story.published`, `story.updated`, `story.retracted`, `heartbeat`.
