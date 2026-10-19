# Sources and ingestion

## What it does

Every enabled source in every profile gets an APScheduler job that runs every
`refresh_minutes` (default 30, plus jitter). Feeds are fetched with `httpx`,
parsed with `feedparser`, normalized (HTML stripped, URLs canonicalized, dates
in UTC) and stored in the `articles` table.

## How it works

- Conditional GETs (ETag/Last-Modified) with a `DISCOVER_FETCH_TIMEOUT_SECONDS`
  (10 s) timeout.
- Dedup on `canonical_url` and on a title+domain `content_hash`.
- Per-source health lives in `source_status`; repeated failures back off
  exponentially.
- A source's id defaults to a slug of its `name` — set `id:` explicitly to keep
  it stable across renames. Sources are shared: one feed assigned to several
  profiles is still fetched once.
- Sources live in the DB (seeded once from YAML) and are managed in Settings >
  Sources or via `/api/v1/sources` (admin key required).

## Adding and managing sources

- **Add feed**: paste a site or feed URL; HTML `<link rel="alternate">` RSS/Atom
  links are discovered, **Test fetch** shows HTTP status, latency, title, item
  count and the 5 newest items without writing anything, then **Save**.
  API: `POST /api/v1/sources/discover`, `/api/v1/sources/test`.
- **Edit, enable/disable, remove, restore**. Remove is a soft delete: fetching
  stops, its articles and stories stay; restore re-enables it.
- **Health** (Settings table, source page, `GET /api/v1/sources/health`): last
  success, items/day (7 d), consecutive failures, last error, next fetch, API
  requests today vs budget, X reads/spend vs cap. After
  `DISCOVER_SOURCE_AUTO_DISABLE_FAILURES` (10) consecutive failures a source is
  disabled (never deleted) with a banner and a one-click **Re-enable**.
- **OPML** import/export per profile (name, URL, tags as `category`):
  `GET|POST /api/v1/profiles/{slug}/sources.opml`.
- **Catalog**: `config/source_catalog.yaml` lists free feeds (finance,
  regulators, tech, science, world), picked in Settings > Sources > Catalog.
  Re-check the URLs with `python scripts/verify_catalog.py` (network).
- **Sync from YAML** adds profiles, sources, profile links and topics that the
  YAML has and the DB lacks. It never overwrites or deletes, and leaves
  app-removed sources removed.

## API sources

The `api_key` is an env var *name*; the value never reaches the DB, logs or
responses.

```yaml
sources:
  - name: Guardian business
    type: guardian            # Guardian Open Platform; bodyText is stored, no extraction
    api_key: ${GUARDIAN_API_KEY}
    params: {q: "interest rates", section: business, from_date: 2026-10-01, page_size: 50, max_pages: 2}
    daily_request_budget: 400
  - name: NewsData markets
    type: newsdata            # NewsData.io /api/1/latest
    api_key: ${NEWSDATA_API_KEY}
    params: {q: ecb, country: de, language: en, category: business, max_pages: 1}
    daily_request_budget: 150 # enforced locally per UTC day; set it to your plan's limit
  - name: Fed watchers on X
    type: x                   # optional, enabled only with a monthly cap
    api_key: ${X_BEARER_TOKEN}
    params: {users: [federalreserve], query: "rate cut"}
    max_posts_per_run: 20
    monthly_budget_usd: 5     # hard stop; spend persists across restarts
    cost_per_read_usd: 0.005  # default
```

Requests and paid reads are counted in `source_usage` (`day:YYYY-MM-DD`,
`month:YYYY-MM`); an exhausted budget skips the run (not a failure). HTTP 429
honours `Retry-After`; invalid keys and provider quota errors show a clear
reason in the health table. X posts are short social items: no text extraction
and no detailed report (`insufficient_text`), the card shows the post.

`python scripts/demo_api_sources.py` builds the demo with fake Guardian and
NewsData APIs and shows their items joining stories.

## Operate

```bash
curl -X POST localhost:8000/api/admin/refresh                     # all sources now
curl -X POST 'localhost:8000/api/admin/refresh?source_id=hacker-news'
curl 'localhost:8000/api/articles?limit=5&source_id=hacker-news&since=2026-10-01T00:00:00Z'
curl 'localhost:8000/api/articles?since=2026-10-07T08:00:00Z&time_field=fetched'  # new since last look
```

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Source disabled banner | 10+ consecutive failures | Fix the URL/credentials, then **Re-enable** |
| Budget skipped runs | `daily_request_budget`/`monthly_budget_usd` exhausted | Raise the cap or wait for the next period |
| SEC feed warns | `DISCOVER_CONTACT_EMAIL` unset | Set it in `.env` |
| Missing `${ENV}` warnings | Secret env var not defined | Add it to `.env`; names are listed on the Alerts page and Pipeline |
