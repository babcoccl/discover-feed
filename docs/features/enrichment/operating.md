# Operating and tuning

## Pipeline panel

Settings > Pipeline shows eligible / waiting / enriched stories, the retry
queue (and when the next retry is due), "gdelt cooling down for N s, M stories
waiting", average sources added, rejection reasons, stories not searched
(`pattern` / `no_signal`) and, separately for each provider, requests today,
then requests, successes, rate limits, timeouts, cooldown skips, transient
(empty / malformed) replies, rejected queries and errors since start, the
current cooldown and failures in a row.

Buttons: **Enrich now** (`POST /api/admin/enrich[?story_id=N]`, admin key; a
story id ignores `reenrich_hours`) and the one-time **Re-enrich stories that
ended with provider errors** (stories that failed, are waiting to retry, or
were marked done by older versions with nothing found and no candidate
recorded; they are retried at the providers' pace, briefs do not wait).

## Scheduling and the worker

Enrichment runs on its own scheduler job, `enrich` (every
`DISCOVER_ENRICH_INTERVAL_SECONDS`, default 60 s), as well as after each
pipeline run. Each sweep admits newly eligible stories (as many as the
providers can serve within `wait_seconds`) and starts the background worker
whenever stories are `pending` or retries are due — including stories left
pending by a restart, which fill the admission capacity on their own. The
`enrich-watchdog` job (every `DISCOVER_ENRICH_WATCHDOG_SECONDS`, default
120 s) logs a warning and restarts the worker thread if it is not alive while
work waits. An unexpected error in the worker loop is logged and retried after
5 s; the thread does not exit.

Every SQLite connection (stories, `source_usage`, `search_provider_state`,
telemetry) waits up to 15 s for a lock (`PRAGMA busy_timeout=15000`).

`GET /api/v1/admin/enrich/status` (admin key) and the **Enrichment worker**
box on Settings > Workflow show whether the thread is alive, the last error,
the newest `enrich` stage event, pending / retries due, provider requests
since start, and when the scheduled job and the watchdog last ran.

```bash
curl -s -H "x-api-key: $DISCOVER_ADMIN_KEY" localhost:8000/api/v1/admin/enrich/status
```

```powershell
Invoke-RestMethod -Headers @{ "x-api-key" = $env:DISCOVER_ADMIN_KEY } http://localhost:8000/api/v1/admin/enrich/status
```

## Retry and degraded passes

**Provider failure is not completion.** If no provider answered for a story
(errors, timeouts, rate limits, cooldowns, budgets), it is not marked done and
`enriched_at` stays unset: `enrich_status = retry`, `enrich_next_attempt_at`
after a backoff of 2 minutes doubling to 60 (`enrich_reason =
"provider_unavailable: <error>"`). While a provider only cools down, the story
waits for the cooldown end without counting a failed attempt. A story that
later succeeds records `recovered after N failed attempt(s)`.

A **degraded pass** is not completion either: when the first provider
(TinyFish) never answered (auth, rate limit, cooldown, timeout, budget) and
only a fallback did, the fallback's sources are kept but the story stays
`retry` (`enrich_reason = "degraded: tinyfish unavailable: <error>"`) and is
searched again after the backoff or the primary's cooldown, whichever is
later, while it is under `target_sources`, for at most 3 degraded passes in a
row. A primary that answers with zero hits is not degraded: the fallback's
result counts as done.

**Second pass:** stories still under `target_sources` are searched once more
`second_pass_hours` (default 3; 0 = off) after the first pass.

## Evaluate and diagnose

Diagnose and evaluate against real providers before trusting it:

```bash
python -m app.enrich.diagnose --story-id 42 --dry-run   # queries, raw responses, decisions
python -m app.enrich.diagnose --auto                     # newest eligible story; adds pages
python -m app.enrich.evaluate --stories 30 --markdown    # dry run over 30 recent stories
python -m app.enrich.evaluate --story-ids 631,632,637    # chosen stories only
python -m app.enrich.evaluate --backtest                 # ground-truth recall (see below)
python -m app.enrich.evaluate --stories 20 --provider gdelt   # only the fallback
python -m app.enrich.evaluate --stories 20 --wait             # wait out cooldowns
# in the Docker container (same database, so the same pacing and cooldowns as the app):
docker exec discover-feed python -m app.enrich.evaluate --stories 20 --markdown
docker exec discover-feed python -m app.enrich.diagnose --story-id 42 --dry-run
```

=== "Windows"

    ```powershell
    .venv\Scripts\python -m app.enrich.diagnose --auto --dry-run
    docker exec discover-feed python -m app.enrich.evaluate --stories 20
    ```

`evaluate` and `diagnose --dry-run` write nothing (no `enrich_status`, no
candidates, no search-cache entries; only the shared pacing/cooldown state and
request counts). `evaluate` prints:

- every accepted page with its snippet / title / text scores and the `via`
  rule (check them for false matches);
- rejections by reason with average scores;
- fetch failures broken down by cause (HTTP status, timeout, size cap, empty
  extraction, blocked, robots) with the domains behind each;
- per provider, the result fields its responses carried plus requests sent vs
  cooldown skips and errors grouped by exact text (count and an example
  query);
- per story, the queries sent, hits per query and the top three rejected
  candidates with scores and reasons.

`--backtest` gives a ground-truth recall number: on stories that already have
3+ feed publishers it hides all but the anchor member, runs the real search
and filters (no writes) and reports recall — the share of the other real
publishers' domains the search found — per story and overall, plus the extra
domains found.

A provider that is cooling down (shared with the app) is skipped and its
fallback answers; with every provider cooling down both refuse (exit 3).
`--wait` waits instead, and `evaluate --wait` also waits whenever a provider
starts cooling down mid-run ("waiting for gdelt (cooling down)"). Runs take at
least `min_interval_seconds` per query (tinyfish 2 s, GDELT 10 s by default).
