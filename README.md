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

## Development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
make install
make lint
make test
make run
```

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
