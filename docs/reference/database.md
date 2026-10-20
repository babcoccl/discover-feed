# Database tables

SQLite (`DISCOVER_DATABASE_URL`; `/data/discover.db` in Docker). Migrations run
idempotently on startup.

| Table | Contents |
| --- | --- |
| `profiles` | Profiles seeded from YAML (`context_enabled`, `enrichment` toggles, `alerts_seeded_at`) |
| `topics` | Topic tabs per profile |
| `sources` | Feeds/API adapters (soft-deleted `removed_at`) |
| `articles` | Normalized items: `url`, `canonical_url`, `content_hash`, `text_status`, `origin` (`feed`/`enrichment`/`context`), `page_image_url`, `icon_url`, `image_source`, `context_*` fields |
| `stories` | Clusters: state, priority, `published_version`, `enrich_*`, `context_*` columns |
| `story_views` | Immutable published documents (one per version) |
| `story_summaries` | Every generated version + `input_hash` |
| `story_events` | Publish/update/retract feed for SSE resume (`event_retention_days`) |
| `summary_jobs` | Generation work queue |
| `generation_attempts` | Last 500 LLM calls with reasons and metrics |
| `source_status` | Per-source health/backoff |
| `source_usage` | Request/spend counters (`day:`/`month:` keys; `search:<name>`, `context:<name>`) |
| `search_provider_state` | Shared provider pacing/cooldowns |
| `search_cache` | Cached search results |
| `enrichment_candidates` | Every enrichment decision with scores and `via` |
| `context_candidates` | Every context decision with scores/judge verdict |
| `context_traces` | One per planner-mode run: planner output, every query, every decision, timings |
| `blocked_domains` | Persisted admin block list |
| `alert_rules`, `alert_channels`, `alert_firings`, `notifications` | Alerts: rules, channels, exactly-once ledger, delivery log |
| `images` | Validated/failed image URLs (`reason`), cache keys |
| `conversations`, `messages` | Story Q&A state |
| `pipeline_runs` | Last pipeline sweep stats |
