---
name: run-and-test
description: How to install, lint, test and run discover-feed locally and in Docker.
---

# Run and test discover-feed

Requires Python 3.12+ (or just Docker).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
make install          # pip install -e ".[dev]"  (re-run after pulling: deps change)
make lint             # ruff check + ruff format --check
make test             # pytest
make format           # auto-fix lint/format issues
make run              # uvicorn with reload on :8000
make demo             # throwaway demo on :8000 (see below)
```

`make demo` deletes and rebuilds `.demo/demo.db`, seeds both example profiles from
`config/profiles.example.yaml`, loads `tests/fixtures/demo/*.xml` (66 articles, served via
`httpx.MockTransport`, no network), extracts text from `tests/fixtures/demo/pages/` and clusters
stories (4 multi-source stories), then runs the app with the scheduler off. It never touches
`./data` or the Docker volume. Open http://localhost:8000/ (redirects to the first profile).
Override with `make demo PORT=8001` / `DEMO_HOST=0.0.0.0`. Card images are generated locally
(`app/demo_images.py`, served by the mock transport and `/demo-image/...`), validated and cached
under `.demo/demo-images/`; no internet needed. `python -m app.demo --no-image-style text_only`
(or `?no_image=logo|text_only|gradient` on any feed page) previews the no-image fallbacks.

Without make (Windows): `demo.cmd` (double-click or `demo.cmd --port 8001 --no-open`) finds
Python 3.12+ via `py`/`python` and runs `scripts/demo.py`, a stdlib-only launcher that creates
`.venv`, `pip install -e .` (only when `pyproject.toml` changed; stamp in
`.venv/.discover-feed-installed`), then `python -m app.demo --open`. `python scripts/demo.py`
works on any OS. CI's `windows` job runs pytest and smoke-tests `demo.cmd` on windows-latest;
keep `demo.cmd` CRLF (enforced by `.gitattributes`).

Docker:

```bash
docker compose up --build -d
curl -fsS localhost:8000/health   # {"status":"ok",...}
curl -X POST localhost:8000/api/admin/refresh      # fetch all configured feeds now
curl 'localhost:8000/api/articles?limit=5'         # newest normalized articles
docker compose down               # add -v to drop the SQLite volume
```

If the build fails with `429 Too Many Requests` from Docker Hub, pull the base image from
the GCR mirror first:
`docker pull mirror.gcr.io/library/python:3.12-slim && docker tag mirror.gcr.io/library/python:3.12-slim python:3.12-slim`

Notes:
- Config: `DISCOVER_CONFIG_PATH` (default `config/profiles.yaml`, compose falls back to
  `config/profiles.example.yaml`). Missing file = app starts with no profiles.
- DB: `DISCOVER_DATABASE_URL` (default `sqlite:///./data/discover.db`; `/data` volume in Docker).
- YAML values support `${VAR}` / `${VAR:-default}`; keep API keys in env / `.env`, never in the repo.
  `app/settings.py` loads `.env` (cwd, or `DISCOVER_ENV_FILE`) into `os.environ` at import
  without overriding the shell; tests point `DISCOVER_ENV_FILE` at a missing file.
- Profiles, sources and topics live in the DB. They are seeded from the YAML only when the
  `profiles` table is empty; after that, edit topics in the UI/API (YAML changes are ignored
  until you drop the DB: `docker compose down -v` or delete `data/discover.db`).
- UI: `/` → `/p/{slug}` (topic tabs + story cards, `?view=articles` for single articles, HTMX),
  `/story/{id}`, `/p/{slug}/settings` (topic CRUD, source status, Pipeline panel).
- Public API `/api/v1` (contract `docs/openapi.v1.json`; `make openapi` / `python -m app.openapi`
  regenerates it and `tests/test_api_v1.py` fails while it is stale; additive changes only in v1):
  `GET /api/v1/profiles[/{slug}[/topics]]`, `/profiles/{slug}/feed?topic=&limit=&cursor=&since=&updated_since=&include=card|full`,
  `/profiles/{slug}/feed.atom`, `/stories/{id}`, `/health`, `/version`,
  `/stream?profile=&topic=&include=id|card|full&since_id=` (+ `Last-Event-ID`; events stored in
  `story_events`, `app/events.py::record`; `app/api_v1.py::stream_events` is unit-testable without
  HTTP). Examples: `examples/client.py`, `examples/stream.js`. The legacy GET `/api/...` routes
  below are deprecated aliases (`Deprecation` header) for one release.
- Auth (`app/auth.py`, off by default): `DISCOVER_API_KEYS` (comma-separated; Bearer or
  `X-API-Key`) guards `/api/v1` + legacy reads (not the pages' `/stream` and `/published`);
  `DISCOVER_ADMIN_KEY` guards `/api/admin/*`, topic writes, Settings and web POSTs (`/login`
  sets a signed HttpOnly cookie). Set both before exposing the port on the NAS.
- Legacy endpoints: `/health`, `/docs`, `/openapi.json`,
  `GET /api/profiles`, `GET /api/profiles/{slug}`,
  `GET /api/profiles/{slug}/feed?group=stories|articles&topic=&limit=&cursor=&time_field=`,
  `GET /api/stories/{id}[?profile=]`, `POST /api/admin/extract`, `POST /api/admin/cluster[?rebuild=true]`,
  `GET /api/admin/articles/{id}?include_text=true` (only place extracted text is exposed),
  `POST /api/profiles/{slug}/topics`, `PUT|DELETE /api/profiles/{slug}/topics/{id}`,
  `GET /api/articles?limit=&source_id=&since=&time_field=published|fetched`, `POST /api/admin/refresh[?source_id=]`.
- `DISCOVER_CONTACT_EMAIL` is appended to the fetch User-Agent; startup logs a warning if an
  SEC source is configured without it.
- With the scheduler enabled the app fetches every configured feed within
  `DISCOVER_REFRESH_JITTER_SECONDS` (60) of startup. Tests never hit the network: they use
  `httpx.MockTransport` + `tests/fixtures/*.xml`; keep it that way.

## Tune clustering

```bash
python -m app.cluster.evaluate --threshold 0.35 0.45 0.55   # precision/recall sweep on labelled fixtures
python -m app.cluster.evaluate --markdown                    # join scores per member + near-miss cosines
python -m app.cluster --rebuild                              # re-cluster the configured DB after changing DISCOVER_CLUSTER_*
```

Labels: `tests/fixtures/demo/stories.yaml` (stories + near-miss pairs). Keep precision at 100%
and the threshold clearly above the highest near-miss cosine. Add new live false merges or misses
as fixtures (feed item + page + label). `tests/test_cluster.py` asserts the exact groups, so
update it when fixtures or defaults change. README "Tuning clustering" has the full procedure.

## Summaries and the LLM

- Publish gate: pages and read APIs only show published `story_views`; no GET route may queue,
  wait for or call generation (`tests/test_architecture.py` enforces imports and behaviour).
  Story states `ingested -> text_pending -> text_final -> clustered -> brief_ready ->
  report_final -> published` (`merged` redirects to the kept id). The worker does one story at
  a time, newest first: brief, report (if 150+ extracted words in some member, else
  `skipped(insufficient_text)`), publish decision. Everything unpublished is only visible in
  Settings > Pipeline (queue, ETA, Prioritize/Retry, Pause/Resume, last 20 attempts, and
  Generation quality from `app/quality.py`: first-attempt pass rate, attempts per success,
  end-to-end seconds per story, failure reasons; `compare` prints the same).
- Per-profile `summaries:` keys: `card_style`, `report_mode` (auto|off; on_demand = auto),
  `publish_requires_report` (true), `publish_max_wait_minutes` (30), `brief_fallback_hours` (6),
  `extraction_wait_seconds` (120), `max_inflight` (1, all LLM calls), `summarize_backfill_hours`
  (24), `summarize_backfill_max` (40), `report_max_articles` (4), `report_total_words` (2400,
  shared by the report's articles, one per source first), `report_max_tokens` (1400),
  `report_timeout_seconds`, `duplicate_threshold`, `verbatim_max_run` (12), `verbatim_max_ratio`
  (0.20 of 8-word phrases), `verbatim_allow` (terms; title words always allowed),
  `retry_temperature_bump` (0.2), `event_retention_days` (7); `report_max_words_per_article` is
  deprecated/ignored; `llm.summarizer.context_tokens` (16384). Reports: 4-6 paragraphs, 300-550
  words (validator 270-605). `DISCOVER_REPORT_MODE` overrides report_mode.
  `POST /api/admin/summarize?story_id=&kind=brief|report|both&force=true`.
- Fixture/old articles are outside the 24 h backfill: the demo build and
  `tests/fixtures/docker/profiles.yaml` widen it, otherwise nothing gets generated.
- SSE: `GET /api/profiles/{slug}/stream` (`story.published`/`story.updated`, `heartbeat`);
  polling fallback `GET /api/profiles/{slug}/published?since=`.
- Diagnose one story against the configured endpoint (read-only):
  `python -m app.summarize.diagnose --auto|--story-id N [--kind brief|report|both] [--dry-run] [--markdown] [--full]`
  (Windows: `.venv\Scripts\python -m app.summarize.diagnose ...`). `generation_attempts` keeps the
  last 500 calls with reasons (`empty_content truncated reasoning_consumed_budget timeout
  http_<status> invalid_json validation:<rule> insufficient_text`).
- Docker E2E (fresh volume, fixture feed server, fake LLM; stdlib only, works on Windows):
  `python scripts/docker_e2e.py [--timeout 900] [--keep] [--out timeline.txt]` uses
  `docker-compose.yml` + `docker-compose.fake-llm.yml`, waits for published stories, asserts no
  unpublished story reaches the feed, prints a timeline, tears down. CI job `docker-e2e`.
- Demo options: `--report-mode off`, `--fake-llm-delay <s>`, `?cards=lead_only` on a feed URL.
- Tests and the demo never call a real LLM: `app/summarize/fake.py` (`FakeLLM(mode)`, modes
  `ok prose malformed out_of_range invented_number empty http_500 timeout wrong_bullet_count
  duplicate_bullets copied_report short_report truncated reasoning_only http_401 demo`; HTTP
  server form for Docker: `python -m app.summarize.fake_server --port 8090 --mode demo --delay 2`; kind detected from the system prompt) is an
  `httpx.MockTransport`; pass it as `LLMClient(role, transport=...)` or
  `create_app(settings, llm_transport=...)`. `tests/conftest.py` makes any LLMClient without a
  transport fail the test.
- Real endpoint: set `LOCAL_LLM_BASE_URL` (e.g. `http://<host>:8080/v1`), `LOCAL_LLM_API_KEY`,
  then `make demo DEMO_ARGS="--real-llm --model <m>"` / `demo.cmd --real-llm --model <m>`.
  Never hardcode the LAN IP.
- Evaluate (Windows: `.venv\Scripts\python`; add `--fake` to run offline):
  `python -m app.summarize.smoke --profile personal-reader --limit 5 --kind both --db .demo/demo.db`,
  `python -m app.summarize.compare --endpoints a=http://h:8080/v1:m1,b=http://h2:8080/v1:m2 --stories 20 --kind both --markdown --db .demo/demo.db`,
  `python -m app.summarize.capture --kind brief --out tests/fixtures/llm/real_response.json --db .demo/demo.db`.
- llama-server: `--host 0.0.0.0 --port 8080 --api-key <key> -c <ctx> -np <slots>`; `-c` is split
  across slots, so give each slot ~16k for 5 articles x 1200 words; match `-np` to
  `summaries.max_inflight`.
- Bump `BRIEF_PROMPT_VERSION` / `REPORT_PROMPT_VERSION` in `app/summarize/prompt.py` whenever
  that prompt changes (regenerates that kind for every story).

## Story Q&A (chat)

- Code: `app/chat/` (`context`, `prompt` with `CHAT_PROMPT_VERSION`, `service` streaming +
  priority + cancellation, `verify`, `store`, `api`); streaming client `LLMClient.stream_chat`.
- Profile keys in `summaries:`: `chat_context_words` (3000), `chat_history_turns` (6),
  `chat_retention_days` (30), `chat_priority` (true), `chat_max_concurrent` (1), `followups` (true).
  Source keys: `aggregator`, `first_party`.
- Endpoints: `POST /api/v1/stories/{id}/ask` (SSE), `GET|DELETE /api/v1/conversations/{id}`,
  `GET /api/v1/stories/{id}/followups`, `GET /api/v1/chat`. Example `examples/ask.py`.
- Fake LLM chat modes: `stream_error stall empty invalid_citation invented_number injection_echo
  http_500 timeout` (`FakeLLM(mode, chat_mode=..., token_delay=...)`; records
  `cancelled_streams`). Demo: `--fake-chat-mode <mode>`. Tests: `tests/test_chat.py`.
- Real model: `python -m app.chat.smoke --story-id N --question "..."`,
  `python -m app.summarize.compare --chat --endpoints a=http://h:8080/v1:m --stories 5`.
- llama-server: `-np` slots split `-c`; chat has priority over background generation with `-np 1`.

## Keyword alerts

- Code: `app/alerts/` (`match` rules, `engine` evaluation + exactly-once ledger
  `alert_firings`, `sender` persistent queue, `channels` webhook/ntfy/SMTP, `sink` fake
  receiver, `api` v1 routes, `web` Alerts page + `/demo/sink`). Hooks: `Ingestor.on_new_articles`
  (title + summary at ingest), `Pipeline.alerts` (`on_extracted` for `match_in: text`,
  `on_clustered` for `notify_on_update`). Scheduler jobs `alerts-send` (15 s) and
  `alerts-maintenance` (digests after quiet hours / rate limit, retention).
- YAML rules/channels are seeded into the DB once per profile (`profiles.alerts_seeded_at`);
  afterwards the DB (UI/API) is the source of truth. Channel secrets must be `${ENV}` refs;
  only the env var name is stored.
- Tests: `tests/test_alerts.py` uses `FakeSink` (real local HTTP + SMTP on random ports,
  `sink.fail = {"webhook": n}`, `sink.delay`) and fixes `app.state.alerts.clock` /
  `alert_sender.clock`; never real ntfy/SMTP. Only articles newer than
  `alert_max_age_hours` (6) fire, so use fresh timestamps.
- Demo: `make demo` starts a `FakeSink`, repoints market-monitor channels to it and ingests
  `tests/fixtures/demo/live/*.xml` (`{{ago:N}}` = N minutes before start). Check
  `/demo/sink` and `/p/market-monitor/alerts` (expect sent, 1 held, 1 failed "pager").
- Gotcha: suppression of a not-yet-clustered article uses the clusterer with a looser
  threshold (`PREDICT_MARGIN`); similar titles in tests may be suppressed as "same story".

## Sources, adapters and budgets

- Code: `app/source_admin.py` (CRUD/sharing, soft delete, health, autodiscovery, read-only
  `test_fetch`, OPML, catalog, `sync_from_yaml`, `reschedule`), `app/sources_api.py` (v1, router
  -level `require_admin`), `app/sources_web.py` + `source_*.html`/`_sources.html` templates,
  adapters `app/sources/{rss,guardian,newsdata,x}.py` on `app/sources/api.py` (`JsonApiAdapter`:
  key in headers only, `scrub()`, 429/`Retry-After` -> `RateLimited`, quota -> `QuotaExhausted`).
- Budgets: `Quota` in `FetchState`; counters in `source_usage` (UTC day/month), loaded by
  `ingest.load_quota`. Auto-disable: `Ingestor(auto_disable_failures=...)` /
  `DISCOVER_SOURCE_AUTO_DISABLE_FAILURES`. Secret warnings: `app/secret_warnings.py`.
- Env: `GUARDIAN_API_KEY`, `NEWSDATA_API_KEY`, `X_BEARER_TOKEN` (see `.env.example`); compose
  passes all of `.env` via `env_file` (optional), uvicorn `--timeout-graceful-shutdown 5`.
- Tests: `tests/test_source_adapters.py` (fake APIs via `httpx.MockTransport`, budgets, X cap,
  restart persistence), `tests/test_source_management.py` (swap
  `app.state.ingestor.transport` for discovery/test fetch; admin auth over every write route),
  `tests/test_ops.py` (compose/.env.example, SIGINT with an open SSE stream; POSIX only).
  Never call real providers. New v1 routes: `make openapi`.
- Demo: `python scripts/demo_api_sources.py` (fake Guardian/NewsData items join stories).
  Catalog: `python scripts/verify_catalog.py --out catalog.md` (network) before editing
  `config/source_catalog.yaml`.

## Web-search enrichment

- Code: `app/search/` (providers `tinyfish` (`app/search/tinyfish.py`, default primary,
  `TINYFISH_API_KEY`) then `gdelt` (fallback), plus `searxng|guardian|newsdata|brave`;
  errors: `RateLimited` cools down, `TransientError` (empty / malformed / unknown text) is
  retried once and never cools down, `QueryRejected` (GDELT "too short") makes the enricher
  rebuild the query once via `query.drop_short_terms`, `NotAuthorized` (401) rests the
  provider 1 h; `SearchOutcome.kind` / `.requests` feed diagnose/evaluate; `SearchService`:
  cache in `search_cache`, budgets in `source_usage` as `search:<name>`, in-memory health and
  cooldown), `app/enrich/` (`query`, `filters`, `fetch` = Extractor + SSRF `Guard`,
  `service.Enricher`). Pipeline: `Pipeline.cluster()` calls `enrich()` before queuing
  summaries; the worker skips `enrich_status = pending|retry` stories until
  `enrich_wait_until` (first admission + `wait_seconds`). `schedule()` admits only what
  the first provider can serve in `wait_seconds`; no provider answering means
  `enrich_status = retry` + `enrich_next_attempt_at` (2 min doubling to 60), never `done`.
  A degraded pass (the first provider never answered, only a fallback did:
  `Enricher._primary_down`) keeps the fallback's members but stays `retry`
  (`enrich_reason = "degraded: ..."`, at most `DEGRADED_MAX_ATTEMPTS` = 3 in a row, while
  under `target_sources`); tests that make the primary fail must expect `retry`, not `done`.
  Guardian is opt-in only; GDELT is opt-in too now (default chain `[tinyfish]` — it
  rate-limits in practice, and a TinyFish cooldown retries the story instead).
  TinyFish is sent `exclude_domains` with the story's own publishers plus the social
  networks (built-in `SOCIAL` + `social_domains`; `SearchProvider.supports_exclude`) —
  they were 116/260 candidates on one live eval and always reject anyway; `mastodon.`/
  `mstdn.` substring hosts can't be enumerated and stay post-filtered. The looser query
  runs when a query leaves too few usable candidates after the cheap rejects, not only
  on 0 hits.
- Provider pacing/cooldown is shared DB state (`search_provider_state`): tests that expect
  a fresh provider after a 429 must delete that row (or use a new DB). GDELT
  `min_interval_seconds` defaults to 10 and is floored at 5 (tinyfish at 2), so fake-provider tests pass
  `sleep=no_sleep` (and a `now=` clock to step through cooldowns/backoffs).
- Skips: `Enricher.skip_reason` (`skip_title_patterns`, `min_signal_tokens` = 1 via
  `query.signal_tokens`) marks stories `done` with `skipped:pattern|no_signal` in
  `schedule()`. Test headlines need 1+ name/number (capitalized words count, the first
  word too) or they are skipped, e.g. "Fresno council backs new water plant" is skipped.
- Enrichment members: `Article.origin = "enrichment"`, `source_id = web:<domain>` (no
  `sources` row; `SourceNames.get` falls back to the domain), `anchor_article_id` re-pins
  them after `--rebuild`. Queries over members that feed alerts, cards or profile visibility
  must filter `origin != "enrichment"`.
- Debug: `python -m app.enrich.diagnose --auto|--story-id N [--dry-run]`,
  `python -m app.enrich.evaluate --stories 30 --markdown [--provider gdelt] [--wait]
  [--story-ids 1,2,3] [--backtest]`, in
  Docker `docker exec discover-feed python -m app.enrich.evaluate ...` (both read-only;
  exit 3 during a provider cooldown) (Windows:
  `.venv\Scripts\python -m app.enrich.diagnose ...`). `--backtest` needs stories with >= 3
  feed publishers: it hides all but the anchor, runs the real search + filters (no writes)
  and prints recall = share of the other publishers' canonical domains found. Admin:
  `POST /api/admin/enrich`, `POST /story/{id}/members/{article_id}/block-domain` (persisted
  `blocked_domains` deny list; removes the domain's members, requeues each story once).
  `evaluate` also prints fetch-failure causes with their domains and per-provider result
  fields (snippet/date/publisher coverage).
- Tests: `tests/test_tinyfish.py` (TinyFish provider, fallback to GDELT, real-headline
  queries, skips, GDELT too-short; its `Router` sends `*tinyfish*` hosts to a fake TinyFish
  and the rest to `FakeSearch`). `tests/test_enrich.py` (fake GDELT/SearXNG/TinyFish via `httpx.MockTransport`, a fake page
  transport + `Guard(resolver)` for SSRF; never call real providers). Docker E2E serves a
  TinyFish-shaped `/tinyfish` (primary, keyless, hits on host `tinywire`) and a
  GDELT-shaped `/gdelt` (fallback, hits on `otherwire`) from the fixture server
  (`tests/fixtures/docker/profiles.yaml`). `--tinyfish-faults
  malformed,malformed,empty,empty,429` (transient ones twice: the client retries once)
  pushes the first stories to GDELT; `--tinyfish-own-first 1` makes the first good answer
  return only same-domain hits so the looser query must find the wire pages;
  `/tinyfish-stats` must show all served and `ok > 0`, and
  at least one story must get a `tinywire` source.
  The fixture server's `--gdelt-faults 429,limit,timeout,empty,429` serves those failure
  modes first; `scripts/docker_e2e.py` requires all of them served (`/gdelt-stats` on
  :8081) and stories still enriched afterwards. Expect a longer run (5 s GDELT pacing, 2 min
  first retry).
- Thresholds (`min_snippet_similarity` 0.25, `min_text_similarity` 0.35) are starting points
  from one live run (Ubuntu 26.10); tune with `evaluate` on live data. Rule B accepts a
  candidate whose snippet >= `min_snippet_strong` (0.45) + `min_shared_entities` + text >=
  `min_text_floor` (0.25) even below `min_text_similarity` (marker `b` in the `via`
  column). `extra_rules` (on by default) add rule C (entities >= `rule_c_entities` 5 and
  text >= `rule_c_text` 0.25 -> `entities+c`) and rule D (title >= `rule_d_title` 0.55 and
  entities >= `rule_d_entities` 3 and text >= `rule_d_text` 0.22 -> `title+d`).
  `link_only: true` stores strong-snippet hits whose page
  cannot be fetched (not robots) as `text_status = none` members: "also reported by
  (link only)", never summarized/cited/counted. Threshold tests pin
  the live scores by monkeypatching `app.enrich.filters.similarity` (and
  `shared_entities` for entity-dependent paths; rule D's title check is
  `similarity(hit.title, headline)` so mock it separately). `diagnose` prints a
  `fetch: <status> (<reason>)` line under candidates whose page was not fetched.
  `publisher_aliases` collapses editions (bbc.co.uk = bbc.com) and `www.`/`amp.`/`m.`/`uk.`
  prefixes in the same-domain and publisher-count checks; `social_domains` /
  `aggregator_domains` extend the built-in lists (threads.*, x.com, fediverse, daily.dev,
  flipboard, msn ...); `skip_url_patterns` skips anchor URLs by path
  (/opinion/, /commentisfree/ ...). TinyFish snippet parsing falls back to
  content/description/summary/text/excerpt and reports per-search field coverage:
  known keys count non-empty (`key:empty` for blank values), every other raw key counts
  by presence, so a missing/emptied/renamed field is visible.
- Quality gate (`enrichment.quality`, `app/enrich/quality.py` `flag_reason`): accepted /
  link-only candidates whose domain looks like a content farm are FLAGGED, never
  rejected — `suspicious_tlds` (edu.pl, club, shop, online, site, xyz, top, icu, info,
  biz, click, link, work, buzz, vip, website, space, today, live) and
  `suspicious_patterns` (a `news|wire|daily|hub|insider|feed` token on the registrable
  domain; fused names like thenews.com.pk / wired.com / superpowerdaily.com / hyper.ai
  do NOT match — checked 2026-10-17, they are legitimate). `Candidate.flag` ->
  `enrichment_candidates.flagged` + member `raw_json["flag"]` -> publish doc `flag` ->
  `Member.flag` -> story.html `data-quality-flag` badge ("source quality uncertain",
  still shown and cited); `Enricher.stats()` returns `flagged` / `flagged_domains` for
  the pipeline panel; `evaluate` prints the flagged count + domains and a `flag` column.
  Confirmed farms go in `enrichment.quality.deny_domains` (tech-insider.org, qwe.edu.pl,
  datagrom.com, relvehq.com in the example profile) — hard-denied in `_denied()` like
  `deny_domains` / the `blocked_domains` table, for enrichment and context links alike.
  The `wire.example` test fixture legitimately flags as `name:wire` (still accepted).
  9to5linux.com's robots.txt allows named crawlers only (`User-agent: * Disallow: /`,
  confirmed live), so its pages are `robots`, not an extractor bug — and not link_only
  either (do not spoof a crawler User-Agent).
- Real-provider relevance is unverified until someone runs `diagnose`/`evaluate` on live data.

## Background and context links

- Code: `app/context/` (`angles.py` = LLM angles + `heuristic_angles` fallback pairing the
  top entity with history/explained/background, `filters.py` = `context_pre_filter` +
  `context_text_check` + `StoryContext`, `service.py` = `ContextFinder` + `ContextResult` +
  `ContextCandidate` rows, `cli.py`, `diagnose.py`, `evaluate.py`). Config `context:` under
  each profile (`ContextConfig`, off by default); per-profile toggle = `profiles.context_enabled`
  (Settings > Pipeline). Runs inside `Pipeline.cluster()` right after `enrich()`; never
  blocks briefs; once per story via `context_status|context_at|context_attempts|
  context_next_attempt_at`; `POST /api/admin/context[?story_id=N]`. The worker waits
  for the enrichment pass (`peer_running`, wired by `attach_context`) and for shared
  provider cooldowns to end before touching a story — up to `WAIT_CAPACITY_SECONDS`
  (15 min), else the pass pauses leaving stories `pending`. Stories that kept nothing
  get a `context_reason` (`no_angles`/`skipped_cooldown`/`provider_errors:<kind>`/
  `no_hits`/`all_rejected`), counted in `last_run.zero_candidates`.
- Eligibility: published story (`published_version` set, state `published`) AND
  (`priority > min_priority` OR distinct publishers >= `min_publishers`), newest first.
  Provider failures => `retry` with backoff, never `done`. Skipped stories
  (`no_angles`, `story gone`) are `done`.
- Accepted pages: `Article.origin = "context"`, `source_id = web:<domain>`,
  `discovered_for_story_id` + `anchor_article_id` pins, `context_note` /
  `context_angle`. They are NOT members: `profiles.get_story` filters them out, so
  briefs/reports/citations/word budget/Q&A/alerts/counts never see them; the story
  page and API v1 `story.context[]` fetch them by `origin`. `NON_FEED =
  (ENRICHMENT, CONTEXT)` covers the shared exclusion spots (clustering counts,
  alert candidate updates, `refresh_stories` rep/counts/timestamps — context pages can
  be years old and must not move `first_seen_at`/`last_updated_at`).
- Acceptance band: `similarity_floor` 0.10 <= TF-IDF score < `similarity_ceiling` 0.45
  (below = `off_topic`, at/above = `same_event`); `min_shared_entities` 1 (2 when the
  angle query was heuristic); `min_words` 300; shingle `near_duplicate` vs members and
  already-accepted pages; `max_per_domain` (1) caps accepted links from one domain
  (`domain_cap`, first judge-passing hit wins). Optional `llm_judge` (default on with
  a summarizer role) keeps only `adds_context: 2` + `is_substantive_article: 1`
  (`not_article` = link list / landing / product page); the judge also returns
  `same_event` (same specific event incl. follow-up details — forces rejection even
  when adds_context is 2) and `is_opinion`; `judge_failed`/`judge_rejected` reject —
  never fabricate notes. Cheap rejects before any fetch:
  `same_domain` (vs ALL member domains incl. enrichment members), `social`/
  `aggregator`/`denied`/`bad_url`/`duplicate_url`/`already_ingested`/`language`.
- Context quality (phase 11.3): `same_event_check` in `filters.py` rejects a DATED
  candidate published inside [story first_at - 12h, now] when title similarity >= 0.45
  OR (shared entities >= 4 AND text similarity >= 0.35); undated candidates are
  exempt; the date comes from the hit or `app.page_meta` (og:article:published_time,
  JSON-LD datePublished, `<time>` on `FetchedPage.meta`). Prior-date preference:
  pages published BEFORE the story rank first among accepted (bounded by
  `OVERSHOOT` = 3 extra fetch+judge evaluations past `max_items`). The shared
  source-quality gate applies too: `_flag()` = `quality.flag_reason` +
  `quality.seo_title_reason` (`[20xx]` tail + comparison/specs/guide) -> `flagged`
  rejected by default via `context.reject_flagged` (enrichment keeps-and-marks;
  context drops). Deny lists are shared with enrichment (`enrichment.deny_domains`,
  `enrichment.quality.deny_domains` — defaults seed the eval farms shattered.io,
  finance.biggo.com, vocal.media, newscord.org via `DEFAULT_DENY_DOMAINS` — and the
  `blocked_domains` table; "Block this domain" on a context link writes there,
  deleting context links without requeueing since they never feed briefs).
- Opinion labels: `is_opinion_page` in `app/page_meta.py` (URL path /opinion/,
  /columnists/, /mnenia/, /blog/…, newsletter hosts substack.com & co., og:type /
  article:section) OR the judge's `is_opinion` -> `context_kind = "opinion"` on the
  Article (badge in the context section, `kind` in API v1), capped at
  `context.max_opinion` (default 1, `opinion_cap` beyond); `ContextCandidate` rows
  record `flagged` + `is_opinion`.
- Budget: shared `search:<name>` usage via `SearchService` plus context's own
  `daily_request_budget` counted as `context:<name>` `source_usage` rows.
- Providers: reuse `SearchService`; `SearchProvider.supports_find_similar` + default
  `find_similar()` raising `FetchError`; `exa` (`app/search/exa.py`, `EXA_API_KEY`)
  implements it — hits merge into the candidate pool under a `related` angle.
- Debug: `python -m app.context.diagnose --story-id N [--dry-run]`,
  `python -m app.context.evaluate --stories 30 --markdown`
  (Windows: `.venv\Scripts\python -m app.context.evaluate ...`). Dry runs write nothing.
- Tests: `tests/test_context.py` (fakes everywhere — `FakeSearch` (hits may carry a
  3rd tuple element = ISO date; None = undated), `page_transport`,
  `Guard(resolver)`, no real LLM; judge fakes must emit `is_substantive_article` too).
  Docker E2E: fixture server `--context-wire-base
  http://contextwire:8081` serves a TinyFish-shaped `/context-search` whose hits are
  generated `/context/<id>` backgrounder pages dated ~30 days back (a real
  backgrounder predates the event — keeps them out of the same-event window AND
  exercises the prior-date preference); `llm_judge: false` in
  `tests/fixtures/docker/profiles.yaml` because the fake LLM cannot emit
  `{"adds_context":2}`); `scripts/docker_e2e.py` asserts at least one story gets
  `context[]` and that no context link ever appears in `sources`.

## Card images

- Code: `app/images/` — `meta.py` (og/twitter/JSON-LD/icon parsing, called by
  `Extractor._fetch_one` on the HTML it already has), `safety.py` (`Guard`: public IPs only,
  re-checked per redirect and at connect time via `_GuardedBackend`), `fetch.py` (`Rejected`
  reasons: `too_large`, `not_image`, `svg`, `ico`, `tracking_pixel`, `too_small`,
  `decompression_bomb`, `private_address`, `bad_scheme`, `too_many_redirects`, `http_NNN`),
  `cache.py` (`image_key` = hash of the URL, WebP <= 640 px, two-level dirs), `select.py`
  (`story_image`, generic = same URL on 3+ articles of a domain), `service.py` (`ImageWorker`:
  `run` after clustering, `backfill` job `images-backfill`, `evict` LRU, `image_stats`).
- Config: top-level `images:` in profiles.yaml (`ImagesConfig`); env `DISCOVER_IMAGES_DIR`
  (`/data/images` in compose), `DISCOVER_NO_IMAGE_STYLE`, `DISCOVER_IMAGES_ALLOW_HOSTS`
  (the e2e override sets `fixtures`, which is on a private compose network).
- Templates: `_media.html` macro (photo / `data-fallback="logo|gradient"` / `data-compact`)
  used by `_story_card.html`, `_card.html`; story page; Pipeline panel `data-image-stats`.
- Tests: `tests/test_images.py`. Build workers with `transport=httpx.MockTransport(...)`,
  `resolver=lambda host: ["93.184.216.34"]` (any public IP) and an instant `sleep`; never the
  system resolver. Freeze `now` per step when testing LRU order. Fallback styles: the logo
  fallback only shows on `/p/market-monitor` in the demo. Adding a scheduler job changes the
  job-count assertion in `tests/test_api.py`. New API fields: `make openapi`.
- Demo pages use `{{demo_base}}` for image URLs (substituted by `app/demo_pages.py` and
  `app/fixture_server.py`).
