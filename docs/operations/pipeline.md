# Pipeline panel and admin API

## What it does

**Settings > Pipeline** (`/p/<slug>/settings`) is the operations view: every
unpublished story with its state and reason, plus per-feature controls.

## What it shows

- **Generation**: queue with ETA from median brief/report time, **Prioritize**
  and **Retry** per story, **Pause/Resume generation**, the last 20 generation
  attempts, and **Generation quality** over the last 500: first-attempt pass
  rate per kind, median attempts per success, median end-to-end model seconds
  per story (brief + report) and failure reasons.
- **Enrichment**: eligible / waiting / enriched stories, retry queue and next
  retry, provider cooldowns ("gdelt cooling down for N s, M stories waiting"),
  average sources added, rejection reasons, per-provider counters (requests
  today, requests/successes/rate limits/timeouts/cooldown skips/transient
  replies/rejected queries/errors since start), the enabled toggle and
  **Enrich now** / **Re-enrich stories that ended with provider errors**.
- **Context**: toggle plus eligible / waiting / done / links added / judge
  accept rate / rejections by reason, and **Find context now**.
- **Sources**: health table (last success, items/day, failures, next fetch,
  budgets) and the catalog.
- **Alerts**: rules, fired in 24 h, queued/held/failed counts, each channel's
  last error, missing `${ENV}` names.
- **Images**: published stories by image source, missing count, cache size,
  failures by reason.
- **Domains**: most-accepted and top flagged domains of the last 7 days.

## Admin API

All under `/api/admin/*` (need `DISCOVER_ADMIN_KEY` when auth is on):

| Endpoint | Action |
| --- | --- |
| `POST /api/admin/refresh[?source_id=]` | Fetch feeds now (all or one) |
| `POST /api/admin/extract` | Extract pending articles now |
| `POST /api/admin/cluster[?rebuild=true]` | Cluster unassigned, or re-cluster all |
| `POST /api/admin/enrich[?story_id=N]` | Enrich eligible stories (id ignores `reenrich_hours`) |
| `POST /api/admin/context[?story_id=N]` | Find context for eligible stories |
| `POST /api/admin/summarize[?story_id=&kind=&force=&wait=]` | Queue brief/report generation |
| `GET /api/admin/articles/{id}?include_text=true` | The only place extracted text is exposed |
| `POST /story/{id}/members/{article_id}/block-domain` | Persist a blocked domain (story page) |
