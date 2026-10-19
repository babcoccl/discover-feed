# discover-feed

A personal "Discover-style" news feed: profiles with topics, sources, keywords,
alert rules and an OpenAI-compatible LLM endpoint. It ingests RSS/Atom and API
sources, clusters articles into stories, enriches them with web search, and
publishes briefs and long-form reports — plus Story Q&A, alerts, an API, SSE
stream and Atom feeds.

Stack: Python 3.12, FastAPI, SQLAlchemy + SQLite, APScheduler, Jinja2 + HTMX +
Tailwind.

**Docs: <https://babcoccl.github.io/discover-feed/>** — full setup, feature and
reference documentation. Offline? The same docs live in `docs/` (plain
Markdown, start at `docs/index.md`).

## Quick start

```bash
cp config/profiles.example.yaml config/profiles.yaml   # set llm.summarizer
docker compose up --build -d
curl localhost:8000/health
# open http://localhost:8000  (feed) or /docs (API)
docker compose down -v        # reset
```

No LLM? `python scripts/docker_e2e.py --keep` runs the stack with a fake LLM
and fixture feeds. Windows without make: `demo.cmd`.

## Features

- [Sources and ingestion](docs/features/sources.md) — RSS/Atom, Guardian,
  NewsData, X; budgets, backoff, OPML import
- [Profiles, topics and the UI](docs/features/profiles-topics.md) — feeds,
  per-topic views, mobile
- [Text extraction](docs/features/text-extraction.md) and
  [clustering](docs/features/clustering.md) — full-text articles grouped into
  stories
- [Web-search enrichment](docs/features/enrichment/overview.md) — extra
  coverage per story with provider pacing and per-candidate filters
- [Background and context links](docs/features/context/overview.md) — further
  reading next to each story
- [Briefs and reports](docs/features/summaries.md) — versioned summaries with
  validation and regeneration
- [Story Q&A](docs/features/qa.md) — follow-up questions grounded in the story
- [Alerts](docs/features/alerts.md) — keyword rules to webhook/ntfy/email
- [Card images](docs/features/images.md) — validated, cached, SSRF-guarded
- [Authentication](docs/features/authentication.md) — API keys and admin login
- [Public API v1](docs/reference/api.md) — REST, SSE, Atom, OpenAPI
- [Operations](docs/operations/pipeline.md) — pipeline control,
  [CLI](docs/operations/cli.md), [backups](docs/operations/backups.md),
  [troubleshooting](docs/operations/troubleshooting.md)
- [Development](docs/development/architecture.md) — layout, testing, extending

## Build the docs

```bash
pip install -e ".[docs]"   # or: pip install -r requirements-docs.txt
make docs                  # check_docs.py + mkdocs build --strict -> site/
make docs-serve            # live preview at http://127.0.0.1:8000
```
