# Architecture and project layout

## Request flow

Ingestion (per-source APScheduler jobs) → `articles` → pipeline job every
`DISCOVER_PIPELINE_INTERVAL_SECONDS`: text extraction → TF-IDF clustering →
enrichment pass → context pass → summary queue → summary worker (one story at
a time, newest first) → publish gate → `story_views` → `story_events` → SSE.
Pages and the public API only ever read published views.

## Layout

| Path | Contents |
| --- | --- |
| `app/main.py` | FastAPI app wiring + lifespan/scheduler |
| `app/config.py`, `app/settings.py` | profiles.yaml models and `DISCOVER_*` env settings |
| `app/sources/` | `SourceAdapter` + `@register(SourceType…)`; rss/guardian/newsdata/x |
| `app/source_admin.py`, `app/sources_api.py`, `app/sources_web.py` | Source CRUD, health, OPML, catalog |
| `app/extract.py` | Article-page fetching + trafilatura |
| `app/cluster/` | `TfidfClusterer`, `evaluate`, `__main__ --rebuild` |
| `app/search/`, `app/enrich/` | Providers + enrichment (`query`, `filters`, `fetch`, `service`) |
| `app/context/` | Background links (`angles`, `filters`, `service`, `rules`, cli/diagnose/evaluate) |
| `app/summarize/` | Worker, prompts, validation, fake LLM, diagnose/compare/smoke/capture |
| `app/chat/` | Story Q&A (`context`, `prompt`, `service`, `verify`, `store`, `api`, `smoke`) |
| `app/alerts/` | Rules engine, channels (webhook/ntfy/SMTP), `sink`, api/web |
| `app/images/` | Page-image meta, validation, SSRF `Guard`, cache, selection, worker |
| `app/templates/` | Jinja2 + HTMX UI |
| `app/lifecycle.py`, `app/publish.py`, `app/story_views.py`, `app/events.py` | Publish gate, versions, SSE events |
| `app/auth.py`, `app/login.py` | API keys + admin login |
| `config/` | `profiles.example.yaml`, `source_catalog.yaml` |
| `scripts/` | `demo.py`, `docker_e2e.py`, `demo_api_sources.py`, `verify_catalog.py`, `check_docs.py` |
| `tests/` | pytest suite + labelled fixtures (`tests/fixtures/demo/`) |
| `docs/` | This site (MkDocs source) + `openapi.v1.json` |

## Documentation

User/operator docs live here (`docs/`, built by MkDocs); coding-agent
conventions live in `.agents/skills/run-and-test/SKILL.md`; the API contract
is `docs/openapi.v1.json`. **Every code change must update the matching docs
page** — `scripts/check_docs.py` (run by `make docs` and CI) fails when a
setting, command, endpoint or config key is undocumented.
