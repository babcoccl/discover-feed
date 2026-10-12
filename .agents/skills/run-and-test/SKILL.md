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
Override with `make demo PORT=8001` / `DEMO_HOST=0.0.0.0`. Card images point at picsum.photos;
without internet the cards fall back to gradient placeholders.

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
