# Command-line tools

Every runnable module. On Windows use `.venv\Scripts\python`; inside Docker use
`docker exec discover-feed python -m ...` (same DB, pacing and cooldowns as the
app). Read-only tools are marked; the rest write state.

## App modules

| Command | What it does |
| --- | --- |
| `python -m app.demo` | Throwaway demo (`make demo`); `--real-llm`, `--fake-llm-delay`, `--fake-chat-mode`, `--report-mode`, `--no-image-style` |
| `python -m app.openapi` | Regenerate `docs/openapi.v1.json` (`make openapi`) |
| `python -m app.cluster --rebuild` | Re-cluster every article in the configured DB |
| `python -m app.cluster.evaluate` | Precision/recall sweep on labelled fixtures — read-only; `--threshold`, `--window-hours`, `--min-shared-tokens`, `--same-source-threshold`, `--markdown` |
| `python -m app.enrich.diagnose --auto\|--story-id N [--dry-run]` | One story's queries, raw responses, decisions; `--auto` without `--dry-run` adds pages |
| `python -m app.enrich.evaluate` | Dry run over recent stories; `--stories`, `--story-ids`, `--provider`, `--wait`, `--backtest`, `--markdown` — read-only |
| `python -m app.context.diagnose --story-id N [--dry-run]` | One story's context queries + decisions — read-only |
| `python -m app.context.evaluate` | Dry run over recent stories; `--stories`, `--markdown` — read-only |
| `python -m app.summarize.diagnose --auto\|--story-id N` | Members, prompt, raw attempts, validation — read-only; `--kind`, `--dry-run`, `--full`, `--markdown` |
| `python -m app.summarize.compare` | Quality metrics per endpoint; `--endpoints`, `--stories`, `--kind`, `--chat`, `--markdown`, `--db` — read-only |
| `python -m app.summarize.smoke` | Real-model briefs/reports for a few stories; `--profile`, `--limit`, `--kind`, `--db`, `--fake` |
| `python -m app.summarize.capture` | Save one raw response for `tests/test_summarizer.py`; `--kind`, `--out`, `--db` |
| `python -m app.summarize.fake_server` | Fake LLM over HTTP for the Docker e2e; `--port`, `--mode`, `--delay` |
| `python -m app.chat.smoke` | One Q&A question against the real chat role; `--story-id`, `--question`, `--history`, `--db` |
| `python -m app.alerts.sink` | Standalone fake webhook + ntfy + SMTP receiver; `--port`, `--smtp-port` |
| `python -m app.fixture_server` | Fixture feed/page/search server used by the e2e |

## Scripts

| Command | What it does |
| --- | --- |
| `python scripts/demo.py` | Stdlib-only demo launcher behind `demo.cmd` (any OS) |
| `python scripts/docker_e2e.py` | Fresh Docker stack with fake LLM + fixtures; asserts the publish gate; `--keep`, `--timeout`, `--out` |
| `python scripts/demo_api_sources.py` | Demo with fake Guardian/NewsData APIs joining stories |
| `python scripts/verify_catalog.py` | Re-check `config/source_catalog.yaml` URLs — network |
| `python scripts/check_docs.py` | Assert the docs cover every env var, command, endpoint and config key |

## Makefile

| Target | What it does |
| --- | --- |
| `make install` | `pip install -e ".[dev]"` |
| `make lint` / `make format` | ruff check + format (check / fix) |
| `make test` | pytest |
| `make run` | uvicorn with reload on :8000 |
| `make demo` | Local demo (above) |
| `make openapi` | Regenerate the API contract |
| `make docs` / `make docs-serve` | Build the docs site (strict) / serve with live reload |
| `make docker-up` / `make docker-down` | `docker compose up --build` / `down` |
