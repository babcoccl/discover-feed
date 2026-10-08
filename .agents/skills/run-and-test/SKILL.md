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
`config/profiles.example.yaml`, loads `tests/fixtures/demo/*.xml` (42 articles, served via
`httpx.MockTransport`, no network), then runs the app with the scheduler off. It never touches
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
- Profiles, sources and topics live in the DB. They are seeded from the YAML only when the
  `profiles` table is empty; after that, edit topics in the UI/API (YAML changes are ignored
  until you drop the DB: `docker compose down -v` or delete `data/discover.db`).
- UI: `/` → `/p/{slug}` (topic tabs + card grid, HTMX), `/p/{slug}/settings` (topic CRUD, source status).
- Endpoints: `/health`, `/docs`, `/openapi.json`,
  `GET /api/profiles`, `GET /api/profiles/{slug}`,
  `GET /api/profiles/{slug}/feed?topic=&limit=&cursor=&time_field=`,
  `POST /api/profiles/{slug}/topics`, `PUT|DELETE /api/profiles/{slug}/topics/{id}`,
  `GET /api/articles?limit=&source_id=&since=&time_field=published|fetched`, `POST /api/admin/refresh[?source_id=]`.
- `DISCOVER_CONTACT_EMAIL` is appended to the fetch User-Agent; startup logs a warning if an
  SEC source is configured without it.
- With the scheduler enabled the app fetches every configured feed within
  `DISCOVER_REFRESH_JITTER_SECONDS` (60) of startup. Tests never hit the network: they use
  `httpx.MockTransport` + `tests/fixtures/*.xml`; keep it that way.
