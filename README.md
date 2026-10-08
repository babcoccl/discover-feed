# discover-feed

My own attempt at building a discovery news feed: a personal, "Discover-style"
news-feed monitor driven by **profiles** (topics, sources, keywords, alert rules
and an OpenAI-compatible LLM endpoint).

Stack: Python 3.12, FastAPI, SQLAlchemy + SQLite, APScheduler, Jinja2 + HTMX +
Tailwind (CDN). RSS/Atom ingestion is implemented; LLM calls are not yet.

## Quick start

```bash
docker compose up --build
curl localhost:8000/health
```

Then open http://localhost:8000 (placeholder page) or http://localhost:8000/docs.

## Ingestion

Every enabled source in every profile gets an APScheduler job that runs every
`refresh_minutes` (default 30, plus jitter). Feeds are fetched with `httpx`
(10s timeout, conditional GETs via ETag/Last-Modified), parsed with `feedparser`,
normalized (HTML stripped, URLs canonicalized, dates in UTC) and stored in the
`articles` table, deduplicated on `canonical_url` and on a title + domain
`content_hash`. Per-source health lives in `source_status`; repeated failures
back off exponentially.

```bash
curl -X POST localhost:8000/api/admin/refresh                     # all sources now
curl -X POST 'localhost:8000/api/admin/refresh?source_id=hacker-news'
curl 'localhost:8000/api/articles?limit=5&source_id=hacker-news&since=2026-10-01T00:00:00Z'
curl 'localhost:8000/api/articles?since=2026-10-07T08:00:00Z&time_field=fetched'  # new since last look
```

A source's id defaults to a slug of its `name` (set `id:` explicitly to keep it stable
across renames). New source kinds plug in via `app/sources/base.py` (`SourceAdapter` +
`@register(SourceType...)`). The optional `state` argument of `fetch()` carries
ETag/Last-Modified for conditional requests; adapters without them can ignore it.

## Profiles, topics and the UI

On first start the profiles, their sources and topics are seeded from the YAML config into the
database; from then on the DB is the source of truth (edit topics in the UI or API).

A topic is a feed tab: an article matches when any `include` keyword appears as a whole word
(case-insensitive) in its title or feed summary and no `exclude` keyword does. An empty
`include` list means every article from the topic's sources (optionally restricted with
`sources`). Matching runs at query time; no LLM is involved.

- `/` redirects to the first profile; `/p/{slug}` shows topic tabs and a card grid; every card
  links to the original article.
- `/p/{slug}/settings` adds, edits, reorders and deletes topics and shows source health.
- `GET /api/profiles`, `GET /api/profiles/{slug}`,
  `GET /api/profiles/{slug}/feed?topic=&limit=&cursor=&time_field=published|fetched`
  (pass the returned `next_cursor` to get the next page), and
  `POST /api/profiles/{slug}/topics`, `PUT`/`DELETE /api/profiles/{slug}/topics/{id}`.
- Articles carry two URLs: `url` is the feed's own permalink (trimmed, relative links resolved)
  and is what cards and the API expose as the link; `canonical_url` (lowercased host, no fragment,
  `utm_*`/`fbclid`/`gclid`/`mc_cid`/`mc_eid`/`ref`/`ref_src` removed) is only the dedup key.

```bash
make demo   # throwaway DB + fixture feeds (no network) on http://localhost:8000
```

### Demo on Windows (no make)

Install [Python 3.12+](https://www.python.org/downloads/) (tick "Add python.exe to PATH"), then
double-click `demo.cmd` in the repo folder, or run it from a terminal:

```bat
demo.cmd                 :: http://localhost:8000, opens your browser
demo.cmd --port 8001 --no-open
```

The first run creates `.venv` and installs the dependencies (needs internet). Later runs
reinstall only when `pyproject.toml` changes. Press Ctrl+C to stop. The same launcher works
anywhere: `python scripts/demo.py`.

## Development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
make install
make lint
make test
make run
```

Without make (e.g. Windows), run the same commands directly from the activated venv
(`.venv\Scripts\activate`): `python -m pip install -e ".[dev]"`, `python -m ruff check .`,
`python -m ruff format --check .`, `python -m pytest`, `python -m uvicorn app.main:app --reload`.

## Configuration

- Profiles live in a YAML file; see [`config/profiles.example.yaml`](config/profiles.example.yaml).
  Copy it to `config/profiles.yaml` (git-ignored) and point `DISCOVER_CONFIG_PATH` at it.
- YAML values may reference env vars: `${OPENAI_API_KEY}` or `${VAR:-default}`.
  Put secrets in the environment or a git-ignored `.env` (see `.env.example`).

| Env var | Default |
| --- | --- |
| `DISCOVER_CONFIG_PATH` | `config/profiles.yaml` |
| `DISCOVER_DATABASE_URL` | `sqlite:///./data/discover.db` (`/data/discover.db` in Docker) |
| `DISCOVER_SCHEDULER_ENABLED` | `true` |
| `DISCOVER_LOG_LEVEL` | `info` |
| `DISCOVER_FETCH_TIMEOUT_SECONDS` | `10` |
| `DISCOVER_REFRESH_JITTER_SECONDS` | `60` |
| `DISCOVER_CONTACT_EMAIL` | unset; appended to the fetch User-Agent. Set it if you use SEC feeds (startup warns otherwise). |
