# Text extraction

## What it does

Fetches each article's page and pulls out the main text with `trafilatura`,
so clustering, briefs and reports work on real content instead of feed
summaries (`app/extract.py`).

## How it works

The pipeline job runs every `DISCOVER_PIPELINE_INTERVAL_SECONDS` (60) after
ingestion:

- Up to `DISCOVER_EXTRACT_MAX_ARTICLES` (50) pending articles, newest first.
- Fetched from the raw `url`: `DISCOVER_EXTRACT_TIMEOUT_SECONDS` (10) timeout,
  `DISCOVER_EXTRACT_MAX_BYTES` (2 MB) cap, at most 3 redirects, same User-Agent
  as feed fetching, `robots.txt` respected.
- One request at a time per domain with
  `DISCOVER_EXTRACT_DOMAIN_DELAY_SECONDS` (2) between them.

`Article.text_status` becomes:

| Status | Meaning |
| --- | --- |
| `ok` | Text extracted (≥ `DISCOVER_EXTRACT_MIN_WORDS`, 80) |
| `failed` | HTTP error, too big, not HTML, too few words |
| `skipped` | `robots.txt` disallowed |

Failures never stop the run; clustering falls back to the feed summary.
Extracted text is **internal only**: it is not in the UI or the public API,
just `GET /api/admin/articles/{id}?include_text=true`.

While a story waits for text it sits in `text_pending`; after
`summaries.extraction_wait_seconds` (120) it is clustered with whatever is
available.

## Operate

```bash
curl -X POST localhost:8000/api/admin/extract    # extract pending articles now
```

Settings > Pipeline shows text-status counts and the last extract run.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `text_status = skipped` | Publisher's robots.txt disallows the fetcher | None needed — clustering uses the summary; do not spoof a crawler UA |
| `text_status = failed` | Paywall/JS-heavy page, or under `DISCOVER_EXTRACT_MIN_WORDS` | Lower the threshold, or accept summary-only |
| Slow extraction | `extract_domain_delay_seconds` pacing one domain | Lower it if the publisher tolerates it |
