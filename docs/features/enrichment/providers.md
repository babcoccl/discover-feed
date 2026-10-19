# Enrichment providers

Each provider in `providers` is asked in order until enough sources are found
(a failing or empty provider falls through to the next). The default is
`tinyfish` alone.

| Provider | Key | Notes |
| --- | --- | --- |
| `tinyfish` | `TINYFISH_API_KEY` | [TinyFish Search API](https://docs.tinyfish.ai/search-api/reference): titles, snippets, dates; free up to 12,000 requests a day; `search_type: news|web` |
| `gdelt` | none | Free, titles only. Opt-in: on a live eval it rate-limited 32 of 37 requests and found nothing; a TinyFish cooldown retries the story rather than handing it to a failing fallback |
| `searxng` | none | Your instance; `base_url` required, `json` in its `search.formats` |
| `guardian` | `GUARDIAN_API_KEY` | Opt-in only: its search is broad (many loosely related results for tech stories) and burns requests |
| `newsdata` | `NEWSDATA_API_KEY` |  |
| `brave` | `BRAVE_API_KEY` |  |
| `exa` | `EXA_API_KEY` | Also implements `find_similar()` for context links |

TinyFish specifics:

- It is sent `exclude_domains` with the story's own publishers plus the social
  networks (built-in list + `social_domains`) so their hits do not use up
  result slots — social links were 116 of 260 candidates on one live eval;
  fediverse hosts on arbitrary domains cannot be enumerated and stay
  post-filtered.
- Without `TINYFISH_API_KEY` TinyFish is asked keyless (no `X-API-Key` header;
  works through a keyless proxy `base_url`). The real API answers **401** then,
  which rests TinyFish for an hour (GDELT serves meanwhile if configured)
  instead of failing every story.
- When a result has no `snippet` the parser also accepts `content`,
  `description`, `summary`, `text` and `excerpt`; each search reports how many
  results carried each field ("result fields" in `evaluate`, `fields:` in
  `diagnose`) — known keys that come back blank count separately (e.g.
  `snippet:empty`) and every other raw key is counted by presence, so a
  missing, emptied or renamed field is visible instead of every snippet
  silently being `-`.

## Pacing, budgets and cooldowns

`min_interval_seconds` defaults to 2 for tinyfish (30 requests/minute; lower
values are raised to 2 unless `override_min_interval: true`), 10 for gdelt
(GDELT documents one request per 5 s but rate-limits at 6 s spacing in
practice, so it is an unreliable fallback; values below 5 are raised to 5 with
a warning) and 1 for the others.

- Pacing and cooldowns are stored per provider in `search_provider_state`
  (`last_request_at`, `cooldown_until`, `consecutive_errors`) and shared by the
  app and the CLI tools.
- Retry-After / HTTP 429 and GDELT's plain-text "limit requests" reply are
  honored: waits of 10 s or less are retried once, longer ones cool the
  provider down; during a cooldown no request is sent (counted as a *cooldown
  skip*, not an error).
- Results with hits are cached for `cache_hours`, empty results for at most
  `empty_cache_minutes` (default 10; 0 = never). An empty body, malformed JSON
  or an unknown plain-text reply (GDELT's is logged in full) is *transient*:
  retried once after 2 s, never cached, no cooldown.
- `daily_request_budget` / `monthly_budget_usd` are counted in `source_usage`
  as `search:<name>` (or `budget_source`, to share an ingestion source's
  budget).

Every key: [search providers](../../configuration/reference.md#search-providers).

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `401` from TinyFish | `TINYFISH_API_KEY` missing/invalid | Set the key; TinyFish rests 1 h, then retries |
| `429` / cooldown skips | Provider rate limit | Wait; `min_interval_seconds` higher, or a budget |
| `query_too_short` | GDELT rejected a short keyword | By design; the rebuilt query also failed |
| Provider error rows in Pipeline | Transient/network/auth | Exact error text is grouped per provider |
