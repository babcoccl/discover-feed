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

- `/` redirects to the first profile; `/p/{slug}` shows topic tabs and a card grid of stories
  ("Covered by N sources") or, with the Stories/Articles toggle, single articles.
  `/story/{id}` lists a story's articles, each linking to the publisher's own URL.
- `/p/{slug}/settings` adds, edits, reorders and deletes topics, shows source health, and has a
  Pipeline panel (text-status counts, last ingest/extract/cluster runs, buttons to run each).
- `GET /api/profiles`, `GET /api/profiles/{slug}`,
  `GET /api/profiles/{slug}/feed?group=stories|articles&topic=&limit=&cursor=&time_field=published|fetched`
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

## Stories: text extraction and clustering

A pipeline job runs every `DISCOVER_EXTRACT_INTERVAL_MINUTES` (15) after ingestion:

1. **Extract** (`app/extract.py`): up to `DISCOVER_EXTRACT_MAX_ARTICLES` (50) pending articles,
   newest first, are fetched from their raw `url` (10s timeout, 2 MB cap, at most 3 redirects,
   same User-Agent as feed fetching, `robots.txt` respected, one request at a time per domain
   with `DISCOVER_EXTRACT_DOMAIN_DELAY_SECONDS` (2) between them). `trafilatura` pulls out the
   main text. `Article.text_status` becomes `ok`, `failed` (HTTP error, too big, not HTML, fewer
   than `DISCOVER_EXTRACT_MIN_WORDS` words) or `skipped` (robots.txt). Failures never stop the
   run; clustering falls back to the feed summary. Extracted text is internal only: it is not in
   the UI or the public API, just `GET /api/admin/articles/{id}?include_text=true`.
2. **Cluster** (`app/cluster/`): each unassigned article joins the most similar story or starts a
   new one. Stories are shared by all profiles. A profile's feed shows only the members from
   sources that profile follows, and a story matches a topic if any of those members matches.

The default clusterer (`TfidfClusterer`) builds TF-IDF vectors (single words, stopwords removed,
plurals and possessives normalized, sublinear tf) from the title (counted twice), the feed summary and the first 1,000
words of extracted text. It compares each article with every story's centroid. An article joins
the best story only if all of these hold:

| Parameter (env var) | Default | Rule |
| --- | --- | --- |
| `DISCOVER_CLUSTER_THRESHOLD` | `0.45` | cosine similarity to the story centroid must be at least this |
| `DISCOVER_CLUSTER_WINDOW_HOURS` | `72` | published within this many hours of the story's first or latest member |
| `DISCOVER_CLUSTER_MIN_SHARED_TOKENS` | `2` | shares this many significant tokens (non-stopword and 4+ chars or capitalized, e.g. `Fed`, `M5`) with the story title |
| `DISCOVER_CLUSTER_SAME_SOURCE_THRESHOLD` | `0.7` | if the story already has an article from the same source, similarity must reach this instead |

The story's representative is the earliest article from the source with the longest text. It
supplies the title, image and snippet. Clustering is incremental and idempotent: re-running it
only touches unassigned articles. `python -m app.cluster --rebuild` (or
`POST /api/admin/cluster?rebuild=true`) re-clusters everything after a parameter change. Other
algorithms implement `Clusterer.assign(new_articles, existing_stories)` in `app/cluster/base.py`.

```bash
curl -X POST localhost:8000/api/admin/extract            # extract pending articles now
curl -X POST localhost:8000/api/admin/cluster            # cluster unassigned articles
curl 'localhost:8000/api/profiles/personal-reader/feed'  # stories (default)
curl 'localhost:8000/api/profiles/personal-reader/feed?group=articles'
curl localhost:8000/api/stories/1                        # all members, any profile
```

### Tuning clustering

The demo fixtures are labelled: `tests/fixtures/demo/stories.yaml` lists which articles are the
same story, plus near-miss pairs that must stay apart (same company or regulator, different
event). To try other parameters, run:

```bash
python -m app.cluster.evaluate --threshold 0.35 0.45 0.55   # sweep: pairwise precision/recall
python -m app.cluster.evaluate --markdown                    # per-member join scores, near-miss cosines
python -m app.cluster.evaluate --window-hours 48 --min-shared-tokens 3 --same-source-threshold 0.8
```

1. Find the threshold range where precision stays at 100% and recall is highest. Joins are
   reported with the score they joined at, and near misses with their cosine. Keep the
   threshold well above the highest near-miss cosine.
2. Lower the threshold if real related stories fail to merge (shown under "missed"). Raise it,
   or raise `MIN_SHARED_TOKENS`, if you see false merges in production.
3. If a live false merge or miss repeats, add it to the fixtures (feed item + page in
   `tests/fixtures/demo/`, label in `stories.yaml`) so `tests/test_cluster.py` covers it.
4. Set the env var and re-cluster existing data: `python -m app.cluster --rebuild`.

At the defaults the fixtures score 100% precision and 88% recall. The one miss is the Fed's
formal FOMC statement, which shares too little wording with the press coverage of the rate cut
(cosine about 0.33). Reaching 100% recall would need a threshold of about 0.30, which leaves too
little margin against false merges on live feeds.

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
| `DISCOVER_EXTRACT_INTERVAL_MINUTES` | `15` (extract + cluster job) |
| `DISCOVER_EXTRACT_MAX_ARTICLES` | `50` per run |
| `DISCOVER_EXTRACT_MIN_WORDS` | `80` |
| `DISCOVER_EXTRACT_DOMAIN_DELAY_SECONDS` | `2` |
| `DISCOVER_CLUSTER_*` | see [Stories](#stories-text-extraction-and-clustering) |
