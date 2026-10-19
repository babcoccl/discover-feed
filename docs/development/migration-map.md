# README migration map

Where each section of the old 72 KB `README.md` moved. The old file is
preserved at `tests/fixtures/old-readme.md` (used by `scripts/check_docs.py`).

| Old README section | New page |
| --- | --- |
| Quick start (Docker, `scripts/docker_e2e.py --keep`, `make demo`) | [Quick start](../getting-started/quick-start.md), [Demo](../getting-started/demo.md) |
| Ingestion (fetch paths, seeds, limits, backoff) | [Sources and ingestion](../features/sources.md) |
| Sources: management, API adapters and budgets | [Sources](../features/sources.md), [Operations > Pipeline](../operations/pipeline.md) |
| Profiles, topics and the UI | [Profiles, topics and UI](../features/profiles-topics.md) |
| Demo on Windows (no make) | [Demo](../getting-started/demo.md) |
| Stories: text extraction and clustering | [Text extraction](../features/text-extraction.md), [Clustering](../features/clustering.md) |
| Tuning clustering | [Clustering > Tuning](../features/clustering.md#tuning) |
| Web-search enrichment (overview) | [Enrichment overview](../features/enrichment/overview.md) |
| "How enrichment works" / acceptance rules B/C/D | [Filters and acceptance](../features/enrichment/filters.md) |
| Queries, signal tokens, skips | [Queries and signal rules](../features/enrichment/queries.md) |
| Deny lists, flagged domains, block-domain, aliases | [Source quality](../features/enrichment/source-quality.md) |
| Provider config, pacing, budgets, evaluate/dry-run | [Providers](../features/enrichment/providers.md), [Operating](../features/enrichment/operating.md) |
| Background and context links | [Context overview](../features/context/overview.md), [Configuration](../features/context/configuration.md), [Operating](../features/context/operating.md) |
| Card images | [Card images](../features/images.md) |
| Briefs and detailed reports | [Summaries](../features/summaries.md) |
| The profile `summaries` block / configuring the endpoint | [Config reference](../configuration/reference.md), [LLM endpoints](../configuration/llm.md) |
| Diagnosing a slow or empty report | [Summaries > Diagnostics](../features/summaries.md#diagnosing-a-slow-or-empty-report) |
| Demo and evaluation | [Evaluation tools](evaluation.md) |
| Story Q&A | [Story Q&A](../features/qa.md) |
| Keyword alerts | [Alerts](../features/alerts.md) |
| Public API (v1) | [API reference](../reference/api.md) |
| Authentication (NAS / exposed port) | [Authentication and admin keys](../features/authentication.md) |
| Development | [Architecture](architecture.md), [Testing](testing.md) |
| Configuration | [profiles.yaml](../configuration/profiles.md), [Environment variables](../configuration/environment.md), [Config reference](../configuration/reference.md) |
| (new) | [First-run checklist](../getting-started/first-run.md), [NAS / home server](../getting-started/nas.md), [CLI index](../operations/cli.md), [Backups](../operations/backups.md), [Troubleshooting](../operations/troubleshooting.md), [Glossary](../reference/glossary.md), [Database tables](../reference/database.md) |
