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

Then open http://localhost:8000 or http://localhost:8000/docs.

First run in Docker (same commands in PowerShell on Windows with Docker Desktop):

1. Copy `config/profiles.example.yaml` to `config/profiles.yaml` and set each profile's
   `llm.summarizer` (or `LOCAL_LLM_BASE_URL` / `LOCAL_LLM_API_KEY` in `.env`).
2. `docker compose up --build -d`. The log shows the LLM health check (`GET {base_url}/models`).
3. Open http://localhost:8000. Until the first story is fully generated the feed says
   "Preparing your first stories..." with live counts and any blocker (endpoint not set,
   unreachable, HTTP 401, paused). Settings > Pipeline shows every story still in progress.
4. Stories appear one at a time as they publish ("N new stories - click to load").
   `docker compose down -v` starts over with an empty database.

No LLM at hand? `python scripts/docker_e2e.py --keep` starts the stack with a fake LLM and
local fixture feeds (`docker-compose.fake-llm.yml`); open http://localhost:8000, then
`docker compose -p discover-e2e down -v`.

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

## Sources: management, API adapters and budgets

Sources live in the DB (seeded once from YAML) and are managed in **Settings > Sources** or via
`/api/v1/sources` (admin key required). A source can be assigned to several profiles and is
still fetched once; adding a feed/API query that already exists just shares it.

- **Add feed**: paste a site or feed URL; HTML `<link rel="alternate">` RSS/Atom links are
  discovered, **Test fetch** shows HTTP status, latency, title, item count and the 5 newest items
  without writing anything, then **Save**. API: `POST /api/v1/sources/discover`, `/sources/test`.
- **Edit, enable/disable, remove, restore**. Remove is a soft delete: fetching stops, its
  articles and stories stay; restore re-enables it.
- **Health** (Settings table, source page, `GET /api/v1/sources/health`): last success,
  items/day (7 d), consecutive failures, last error, next fetch, API requests today vs budget,
  X reads/spend vs cap. After `DISCOVER_SOURCE_AUTO_DISABLE_FAILURES` (10) consecutive failures a
  source is disabled (never deleted) with a banner and a one-click **Re-enable**.
- **OPML** import/export per profile (name, URL, tags as `category`):
  `GET|POST /api/v1/profiles/{slug}/sources.opml`.
- **Catalog**: [`config/source_catalog.yaml`](config/source_catalog.yaml) lists free feeds
  (finance, regulators, tech, science, world), picked in Settings > Sources > Catalog. Re-check
  the URLs with `python scripts/verify_catalog.py` (network).
- **Sync from YAML** adds profiles, sources, profile links and topics that the YAML has and the
  DB lacks. It never overwrites or deletes, and leaves app-removed sources removed.

API sources (the key is an env var *name*; the value never reaches the DB, logs or responses):

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

Requests and paid reads are counted in `source_usage` (`day:YYYY-MM-DD`, `month:YYYY-MM`); an
exhausted budget skips the run (not a failure). HTTP 429 honours `Retry-After`; invalid keys and
provider quota errors show a clear reason in the health table. X posts are short social items:
no text extraction and no detailed report (`insufficient_text`), the card shows the post.
Missing `${ENV}` values for alert channels and sources are logged once at startup and listed on
the Alerts page and Settings > Pipeline (names only). `python scripts/demo_api_sources.py` builds
the demo with fake Guardian and NewsData APIs and shows their items joining stories.

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

## Web-search enrichment (more sources per story)

Most stories arrive from one or two of your feeds. When a profile turns on `enrichment`, each
new or under-covered story (fewer than `target_sources` publishers, updated within
`max_age_hours`) is searched for on the web, and up to `max_added_sources` independent
publishers' pages are added to it, so briefs and reports cite several outlets.

How it works (`app/search/`, `app/enrich/`):

1. After clustering the story is marked `enrich_status = pending`; its brief waits up to
   `wait_seconds` from that first admission (the summary worker skips it meanwhile, never
   blocking on the search; retries and second passes never hold it again). A background
   thread does the rest, `concurrency` stories at a time. **Admission control:** only as
   many stories are marked as the first provider can serve within `wait_seconds`
   (`wait_seconds / (min_interval_seconds x max_queries_per_story)`, minus a running
   cooldown and the stories already waiting), newest first; the rest stay unmarked and are
   admitted on a later pipeline sweep (every minute). During backlog catch-up, stories older
   than the summarizer's `summarize_backfill_hours` are not enriched.
2. Stories other outlets are unlikely to cover are not searched: headlines matching
   `skip_title_patterns` (default `^Show HN`, `^Ask HN`, `^Tell HN`, `^Opinion`, `^Letters`;
   `enrich_reason = skipped:pattern`) and headlines with fewer than `min_signal_tokens`
   (default 1) distinct names / numbers ("The cost of getting a job today":
   `skipped:no_signal`; one entity, acronym or number is enough to search, so "AI was
   great until it was not" is searched now). They are marked done at once (briefs never wait)
   and counted separately in Settings > Pipeline and `evaluate`.
   Queries come from the headlines only (never article text), built per provider, after
   removing boilerplate labels (`strip_title_prefixes`: `Show HN:`, `Exclusive:`,
   `Analysis -`, `Live:` ...). The heuristic ranks terms: quoted multi-word names (`"Federal Reserve"`), other names,
   names with a version or number (`"Ubuntu 26.10"`), numbers and percentages (`0.1%`), then
   distinctive words, boosted when other member headlines repeat them; lone digits are
   dropped. TinyFish gets up to 5 terms, GDELT and the Guardian (which AND every term) 3-4;
   NewsData's `q` stays within 100 characters; SearXNG/Brave get up to 6 terms. GDELT never
   gets a keyword under 3 characters (it splits on punctuation, so `U.S.` and `0.1%` count):
   `AI`, `US`, `UK`, `EU`, `UN` are spelled out (`"artificial intelligence"`), other short
   words dropped; a quoted name keeps a short word (`"Gen Z"`). If GDELT still answers
   "keyword that was too short", the query is rebuilt once without any short part and sent
   again (no cooldown); if that is rejected too the search is skipped (`query_too_short`). Only when that query leaves too few
   usable candidates after the cheap rejects (same domain, social, already known, time,
   language) is a looser one sent (`"Federal Reserve" (Powell OR 2025 OR rates)`), at most
   `max_queries_per_story` (default 2) requests per provider and story. `query_mode: llm`
   asks the summarizer for up to 2 queries, trimmed to the same per-provider limits, falling
   back to the heuristic.
3. Each provider in `providers` is asked in order until enough sources are found (a failing
   or empty provider falls through to the next). The default is `tinyfish` alone: `gdelt`
   stays available but is opt-in (on a live eval it rate-limited 32 of 37 requests and
   found nothing; when TinyFish cools down stories are retried rather than handed to a
   fallback that keeps failing). TinyFish is sent `exclude_domains` with the story's own
   publishers so their hits do not use up result slots.
   Supported: `tinyfish` ([TinyFish Search API](https://docs.tinyfish.ai/search-api/reference):
   titles, snippets and dates; key in `TINYFISH_API_KEY`, free up to 12,000 requests a day,
   `search_type: news|web`), `gdelt` (free, no key, titles only), `searxng` (your instance;
   `base_url` and `json` in its `search.formats`), `guardian`, `newsdata` and `brave` (keys by
   env var name). Without `TINYFISH_API_KEY` TinyFish is asked keyless (no `X-API-Key`
   header; works through a keyless proxy `base_url`); the real API answers 401 then, which
   rests TinyFish for an hour (GDELT serves meanwhile) instead of failing every story.
   `min_interval_seconds` defaults to 2 for tinyfish (30 requests / minute; lower values are
   raised to 2 unless `override_min_interval: true`), 10 for gdelt (GDELT documents one request
   per 5 s but rate-limits at 6 s spacing in practice, so it is an unreliable fallback; values
   below 5 are raised to 5 with a warning) and 1 for the others. `guardian` is opt-in only:
   its search is broad (many loosely related results for tech stories) and burns requests. Pacing and cooldowns are stored per provider in `search_provider_state`
   (`last_request_at`, `cooldown_until`, `consecutive_errors`) and shared by the app and the
   CLI tools. Retry-After / HTTP 429 and GDELT's plain-text "limit requests" reply are
   honored: waits of 10 s or less are retried once, longer ones cool the provider down;
   during a cooldown no request is sent (counted as a *cooldown skip*, not an error). Results
   with hits are cached for `cache_hours`, empty results for at most `empty_cache_minutes`
   (default 10; 0 = never). An empty body, malformed JSON or an unknown plain-text reply
   (GDELT's is logged in full) is *transient*: retried once after 2 s, never cached, no
   cooldown.
   `daily_request_budget` / `monthly_budget_usd` are counted in `source_usage` as
   `search:<name>` (or `budget_source`, to share an ingestion source's budget).
4. **Provider failure is not completion.** If no provider answered for a story (errors,
   timeouts, rate limits, cooldowns, budgets), it is not marked done and `enriched_at` stays
   unset: `enrich_status = retry`, `enrich_next_attempt_at` after a backoff of 2 minutes
   doubling to 60 (`enrich_reason = "provider_unavailable: <error>"`). While a provider only
   cools down, the story waits for the cooldown end without counting a failed attempt.
   A story that later succeeds records `recovered after N failed attempt(s)`.
   A **degraded pass** is not completion either: when the first provider (TinyFish) never
   answered (auth, rate limit, cooldown, timeout, budget) and only a fallback did, the
   fallback's sources are kept but the story stays `retry`
   (`enrich_reason = "degraded: tinyfish unavailable: <error>"`) and is searched again after
   the backoff or the primary's cooldown, whichever is later, while it is under
   `target_sources`, for at most 3 degraded passes in a row. A primary that answers with
   zero hits is not degraded: the fallback's result counts as done.
5. **Second pass:** stories still under `target_sources` are searched once more
   `second_pass_hours` (default 3; 0 = off) after the first pass.
6. Candidates are rejected before fetching for: same registrable domain as a member
   (`bbc.co.uk`, `cnn.com`), social sites and aggregators, `deny_domains`, an already known
   URL, a publication time more than `max_time_gap_hours` from the story, another language,
   or title/snippet TF-IDF similarity below `min_snippet_similarity` (default 0.25). Hits without a snippet
   (GDELT returns titles only) must share an entity with the headlines and reach
   `min_title_similarity` (default 0.30); both scores are stored in `enrichment_candidates`
   and the text check below has the final say. The best
   `max_candidates` are fetched with the extractor's robots.txt rules, per-domain delay and
   size caps, through the image fetcher's SSRF guard (http(s) only, public addresses only,
   every redirect re-checked). Failed, robots-disallowed (reason `robots`, e.g. 9to5linux.com,
   whose robots.txt allows named crawlers only) and paywalled (`isAccessibleForFree: false`)
   pages are dropped; `diagnose` prints the fetch detail under each one
   (`fetch: skipped (disallowed by robots.txt)`, `fetch: failed (HTTP 403)`). Extracted text
   is accepted when it reaches `min_text_similarity` (default 0.35), or — rule B — when the
   hit's snippet was strong (`min_snippet_strong`, default 0.45), the text shares
   `min_shared_entities` names with the headlines and stays above `min_text_floor`
   (default 0.25): a precise snippet can carry a page that extracts thinly. In both cases
   it must not be a copy (5-word shingle overlap of `duplicate_overlap`
   with a member or an accepted page: syndicated wire copies collapse).
7. Accepted pages become members with `origin = enrichment`, source id `web:<domain>`,
   pinned to the story through incremental clustering, merges and `--rebuild`. They count
   toward "N sources", are cited and numbered in briefs and reports like any member, and are
   badged "found via web search" on the story page; the API returns `sources[].origin`
   (`feed` | `enrichment`). They never fire alerts, never become cards of their own and
   never make a story visible in a profile that does not follow one of its feed sources.
   With `link_only: true`, a candidate whose snippet is strong (>= `min_snippet_strong`)
   and shares the entity names but whose page cannot be fetched (failed, blocked or
   paywalled — never robots-disallowed) is stored as a member with `text_status = none`:
   the story page lists it as "also reported by (link only)"; it is never summarized,
   cited or counted in the sources.
8. Sources found after a story was published regenerate it once (`enrich_regens`); the
   published version stays visible until the new one passes the publish gate. At most
   `late_regen_max_per_hour` (default 6) stories are regenerated that way per hour, so
   enrichment cannot swamp the LLM; excess late sources are kept and used the next time the
   story is regenerated (`enrich_reason = late_regen_deferred`). Turn late regenerations off
   with `resummarize_on_enrichment: false`.

Enable it per profile (`config/profiles.example.yaml` shows every option; the
Settings > Pipeline checkbox overrides the YAML for an existing database):

```yaml
enrichment:
  enabled: true
  providers:
    - name: tinyfish                # primary; key in TINYFISH_API_KEY (.env)
      min_interval_seconds: 2       # never below 2 (30 requests / minute)
    # - name: gdelt                 # opt-in fallback: rate-limits in practice (32 of 37
    #   min_interval_seconds: 10    # requests on a live eval); never below 5 s
    # - name: guardian              # opt-in: broad search, mostly noise for tech stories
    #   api_key: GUARDIAN_API_KEY
    # - name: searxng
    #   base_url: http://searxng:8080
    # - name: brave
    #   api_key: BRAVE_API_KEY        # env var name, never the key itself
    #   daily_request_budget: 60
```

The thresholds and providers come from the summarize profile (`DISCOVER_SUMMARIZE_PROFILE`,
else the first). Settings > Pipeline shows eligible / waiting / enriched stories, the retry
queue (and when the next retry is due), "gdelt cooling down for N s, M stories waiting",
average sources added, rejection reasons, stories not searched (`pattern` / `no_signal`)
and, separately for each provider, requests today, then requests, successes, rate limits,
timeouts, cooldown skips, transient (empty / malformed) replies, rejected queries and errors
since start, the current cooldown and failures in a row, plus **Enrich now**
(`POST /api/admin/enrich[?story_id=N]`, admin key; a story id ignores `reenrich_hours`) and
the one-time **Re-enrich stories that ended with provider errors** (stories that failed,
are waiting to retry, or were marked done by older versions with nothing found and no
candidate recorded; they are retried at the providers' pace, briefs do not wait).

Diagnose and evaluate against real providers before trusting it. The similarity thresholds
(`min_snippet_similarity` 0.25, `min_text_similarity` 0.35) are starting points from one live
evaluation (story "Ubuntu 26.10": related GamingOnLinux / Phoronix / Linuxiac pages scored
0.36-0.67 on text, Betanews / OMG! Ubuntu 0.29-0.30 on snippets); tune them with `evaluate`
as more live data comes in (real-world false-match rates are not measured yet):

```bash
python -m app.enrich.diagnose --story-id 42 --dry-run   # queries, raw responses, decisions
python -m app.enrich.diagnose --auto                     # newest eligible story; adds pages
python -m app.enrich.evaluate --stories 30 --markdown    # dry run over 30 recent stories
python -m app.enrich.evaluate --stories 20 --provider gdelt   # only the fallback
python -m app.enrich.evaluate --stories 20 --wait             # wait out cooldowns
# in the Docker container (same database, so the same pacing and cooldowns as the app):
docker exec discover-feed python -m app.enrich.evaluate --stories 20 --markdown
docker exec discover-feed python -m app.enrich.diagnose --story-id 42 --dry-run
```

Windows: `.venv\Scripts\python -m app.enrich.diagnose --auto --dry-run` (PowerShell:
`docker exec discover-feed python -m app.enrich.evaluate --stories 20`).

`evaluate` and `diagnose --dry-run` write nothing (no `enrich_status`, no candidates, no
search-cache entries; only the shared pacing/cooldown state and request counts). `evaluate`
prints every accepted page with its snippet / title / text scores (check them for false
matches), rejections by reason with average scores, stories not searched, per provider the
requests actually sent vs cooldown skips and errors grouped by exact text (count and an
example query), and per story the queries sent, hits per query and the top three rejected
candidates with scores and reasons. A provider that is cooling down (shared with the app)
is skipped and its fallback answers; with every provider cooling down both refuse (exit 3).
`--wait` waits instead, and `evaluate --wait` also waits whenever a provider starts cooling
down mid-run ("waiting for gdelt (cooling down)"). Runs take at least
`min_interval_seconds` per query (tinyfish 2 s, GDELT 10 s by default).

## Card images

Cards show the best validated image of a story, never one repeated placeholder (`app/images/`):

1. **Page images at no extra cost.** When the extractor fetches an article's HTML it also reads
   `og:image`, `twitter:image`, JSON-LD `image` (incl. `@graph`/`ImageObject`) and, as a fallback,
   `trafilatura.extract_metadata`, plus the page's `apple-touch-icon`/favicon. Relative URLs are
   resolved against the page; only `http(s)` is kept. Stored as `Article.page_image_url` /
   `icon_url`; the feed's own `Article.image_url` is never overwritten.
2. **Validation** (`app/images/fetch.py`): 10 s timeout, 3 MB cap (header and streamed),
   `image/*` content type, decoded with Pillow (pixel limit `max_pixels`, 40 MP, against
   decompression bombs). Rejected: SVG, ICO, tracking pixels (tiny, or `pixel`/`spacer`/
   `beacon` URLs), anything under 320x180. Failures are recorded per URL (`image_assets.reason`).
   The same URL on 3+ articles of one domain (a site logo/seal) is marked `generic` and only used
   when nothing else is available.
3. **Cache and proxy** (`images.cache`, default on): each validated image is downloaded once,
   resized to at most 640 px wide as WebP into `DISCOVER_IMAGES_DIR` (`/data/images` in Docker)
   keyed by a hash of its URL, and served from `/img/{hash}` with
   `Cache-Control: public, max-age=31536000, immutable`. Least-recently-used files are evicted
   above `images.max_mb` (500). The DB keeps the original URL (`/img/{hash}` redirects to it if
   the file was evicted); with `cache: false` cards hotlink the original. Fetches send no cookies
   or referrer (`<img referrerpolicy="no-referrer">` too). **SSRF guard**
   (`app/images/safety.py`): `http(s)` only, every hostname is resolved and all addresses must be
   public (no private, loopback, link-local, CGNAT, multicast or reserved IPs, incl.
   IPv4-mapped IPv6), checked again for every redirect (max 3) and at connect time, so DNS
   rebinding can't slip through. `DISCOVER_IMAGES_ALLOW_HOSTS` exempts named hosts (the
   Docker e2e fixture service).
4. **Story image** (`app/images/select.py`): the largest valid non-generic image among the
   story's members; for one article, the page image wins over a feed image narrower than
   `tiny_feed_width` (640). Recomputed (and the story republished as a new version) when members
   change or an image check finishes. Published as `image` (`url`, `key`, `source`, size) and
   `logo` in the story document, the cards, and API v1 (`image: {url, proxy_url, source,
   width, height}`; `image_url` stays for compatibility).
   `image_source` on articles and stories is `feed | page | member | logo | none`.
5. **No-image fallback** (`images.no_image_style`): `logo` (default) shows the publisher's icon
   (page or `/favicon.ico`, validated and cached like photos) on a tint derived from it, with the
   publisher name; `text_only` drops the image area (compact card); `gradient` picks a gradient
   per card. Preview any style with `?no_image=logo|text_only|gradient` on a feed page.
6. **Backfill**: the `images-backfill` job (every `backfill_interval_minutes`, 10) checks up to
   `backfill_per_run` (20) articles of stories published in the last `backfill_days` (7),
   re-fetching the page only for articles extracted before image support, one request at a time
   with a delay. It is resumable: articles with `image_checked_at` set are never re-checked.
   The Settings > Pipeline panel shows published stories by image source, missing count, cache
   size and failures by reason.

```yaml
images:                 # top level of profiles.yaml; all optional
  cache: true
  max_mb: 500
  no_image_style: logo  # logo | text_only | gradient
  backfill_days: 7
```

Not verified against real publishers: hotlink blocking / `403` for non-browser fetches (the
original-URL fallback may then fail too), real image sizes and aspect ratios, CDN URLs that
change per request (which defeats generic-image detection and the cache), and favicons that are
only `.ico` (rejected as photos, accepted as icons when Pillow can decode them).

## Briefs and detailed reports

Stories get two LLM-written artifacts (Perplexity Discover style), both with `[n]` citations
that link to each article's own permalink (`Article.url`):

- **Brief** (feed cards and the top of the story page): one lead sentence (at most 30 words,
  what happened) plus exactly three bullets (at most 28 words each: what happened, why it
  matters or context, what's next or a key detail). Cards show the image, title, lead, the
  three bullets, source chips and relative time; `card_style: lead_only` hides the bullets.
- **Detailed report** (story page, below the brief): 4-6 paragraphs, about 300-550 words,
  paraphrasing the sources, then the numbered **Sources** list. Every card opens its story
  page (single-source cards also have a "Read on <source>" link to the article). `[n]` is the same article in
  the brief, the report and the Sources list. The page shows the model and when it was written.

Written by an OpenAI-compatible chat completions endpoint (llama.cpp `llama-server`, vLLM,
Ollama, OpenAI...). Only the numbered sources' text goes into the prompt; extracted article
text is never returned by the public API or rendered in a page.

How it works: the publish gate (`app/lifecycle.py`, `app/summarize/worker.py`, `app/publish.py`):

The app is "always on": generation runs in the background and pages only read finished,
published story views. Opening a page never triggers, queues or waits on the LLM.

- **Story states.** Every article gets a story at ingest, which moves
  `ingested -> text_pending -> text_final -> clustered -> brief_ready -> report_final ->
  published`. `text_final` means extracted text is available or unavailable: decided after
  extraction, or after `extraction_wait_seconds` (120). When clustering merges stories the
  oldest id is kept; the others become `merged` and `/story/{old}` and `/api/stories/{old}`
  redirect (301) to it.
- **One story at a time, newest first.** The worker takes the highest-priority, most recently
  updated story and runs its brief, then its report (if eligible), then the publish decision,
  before it starts the next story. `max_inflight` (1) caps all LLM requests process-wide.
  Unpublished stories older than `summarize_backfill_hours` (24) are skipped and at most
  `summarize_backfill_max` (40) unpublished stories are worked on; published stories that
  gained members are always refreshed.
- **Report eligibility** is decided without the LLM: at least one included member with
  `text_status=ok` and 150+ extracted words. Otherwise the report is final as
  `skipped(insufficient_text)` with each member's status and word count. Report failures back
  off their own job and never pause briefs; brief endpoint failures
  (`DISCOVER_SUMMARIZE_FAILURE_LIMIT`, 3 in a row) pause the worker for
  `DISCOVER_SUMMARIZE_PAUSE_MINUTES` (10).
- **Publish rule.** A story publishes once its brief is valid and its report is final
  (`ready`, `skipped(reason)` or `failed(reason)`). With `publish_requires_report: false` it
  publishes as soon as the brief is valid. After `publish_max_wait_minutes` (30) it publishes
  with the report pending ("Detailed report still generating"). If the brief fails for good,
  the story publishes after `brief_fallback_hours` (6) with the feed snippet, labelled
  `Summary unavailable: <reason>`.
- **Story views.** Every change writes a new version to `story_views (story_id, version,
  schema_version, state, published_at, doc JSON, updated_at)` in the same transaction as the
  summary row. `stories.published_version` moves only when the new version meets the publish
  rule, so the old version stays on screen while a replacement is generated. One document per
  story; the profile's sources are filtered when it is read. Existing databases are migrated
  in place on startup (idempotent; old `story_summaries` publish under these rules).
- **Live arrivals.** Every publish, update and retraction is stored in `story_events` (kept
  `event_retention_days`, 7) and streamed by `GET /api/v1/stream` (see
  [Public API](#public-api-v1)); the pages still use the id-only
  `GET /api/profiles/{slug}/stream` (`story.published` / `story.updated` with
  `{story_id, version}` and a `heartbeat` event). The
  feed shows "N new stories - click to load" without moving the grid; without SSE it polls
  `GET /api/profiles/{slug}/published?since=<iso>` every 30 s. An open story page offers a
  reload when a newer version is published.
- **Before the first story.** The feed says "Preparing your first stories..." with live counts
  (ingested, extracting, clustering, summarizing N of M) and names what blocks generation: no
  endpoint set, endpoint unreachable, HTTP 401 or the worker paused. On startup the app logs
  the result of `GET {base_url}/models` (a 401 there is reported but tolerated).
- **Pipeline panel** (Settings > Pipeline): every unpublished story with its state and reason
  and an ETA from the median brief/report time, **Prioritize** and **Retry** per story,
  **Pause/Resume generation**, the last 20 generation attempts, and **Generation quality**
  over the last 500 attempts: first-attempt pass rate per kind, median attempts per success,
  median end-to-end model seconds per story (brief + report) and the failure reasons.
- **Generation attempts.** `generation_attempts` keeps the latest 500 LLM calls: story, kind,
  model, endpoint host (never the key), member text statuses and word counts, input words,
  estimated tokens, usage, finish reason, latency, content and reasoning length, the raw
  content (truncated), validation errors, outcome and reason (`empty_content`, `truncated`,
  `reasoning_consumed_budget`, `timeout`, `http_<status>`, `invalid_json`,
  `validation:<rule>`, `insufficient_text`). A warning is logged when thinking is disabled but
  the model still returns reasoning.
- **Prompt size.** `llm.summarizer.context_tokens` (16384) is the model's context window;
  report inputs are trimmed so the answer (`report_max_tokens`) still fits. After the usual
  validation retry, a last attempt asks for `structured_output: json_object` and extracts the
  JSON leniently; citation, number and verbatim-copy checks still apply. Retries quote the
  exact phrases that were copied, ask for a different sentence structure and run
  `retry_temperature_bump` (0.2) hotter. Raw newlines/tabs inside JSON strings become spaces
  (no glued words); suspected glued words (`tasksAI`, `AIworkloads`) are logged as attempt
  `warnings`, never rejected.
- Input: briefs use up to `DISCOVER_SUMMARIZE_MAX_ARTICLES_PER_STORY` (5) members and the first
  `DISCOVER_SUMMARIZE_MAX_WORDS_PER_ARTICLE` (1200) words of each; reports use
  up to `report_max_articles` (4) members, one per source first, sharing
  `report_total_words` (2400) source words (a one-source story gets all of them; a short
  article's unused share goes to the others), keeping the brief's numbering. Briefs are rejected for a wrong bullet count, a lead that is not one sentence,
  length violations, bad citations, numbers not in a cited source, or near-duplicate bullets
  (`duplicate_threshold`, 0.6). Reports are rejected for fewer than 4 or more than 6 paragraphs,
  fewer than 270 or more than 605 words (the 300-550 target plus 10%), uncited paragraphs, bad
  citations, invented numbers, duplicate paragraphs, a run of `verbatim_max_run` (12) or more
  words copied from a source (case, punctuation and hyphens ignored), or more than
  `verbatim_max_ratio` (20%) of the report's 8-word phrases found in the sources. Copied runs
  made only of story-title words, numbers or `verbatim_allow` terms don't count. A brief that keeps failing
  falls back to a 2-3 sentence summary, then to the feed snippet.
- Every version is kept in `story_summaries`; `input_hash` covers member ids, source text
  hashes, prompt version, model and kind, so unchanged input means no LLM call.
- API: story feed items and `GET /api/stories/{id}` (published stories only; 404 otherwise)
  have `version`, `published_at`, `brief` (`{lead, lead_citations, bullets, status, style,
  basis, model, generated_at, sources, label}`), `report` (`{paragraphs, state:
  ready|pending|skipped|failed, status (deprecated: ok|pending|skipped|failed), reason, label,
  details, model, generated_at, sources}`) and the deprecated `summary` (kept for one more
  release). `POST /api/admin/summarize` (`?story_id=`, `?kind=brief|report|both`,
  `?force=true`, `?wait=true`) queues generation.

### The profile `summaries` block

Each profile can set (defaults shown; read from the YAML on every start, not stored in the DB):

```yaml
summaries:
  card_style: lead_bullets         # or lead_only (cards show the lead without bullets)
  report_mode: auto                # auto | off (on_demand is read as auto)
  publish_requires_report: true    # wait for the report (or its final skipped/failed state)
  publish_max_wait_minutes: 30     # then publish with "Detailed report still generating"
  brief_fallback_hours: 6          # a failed brief publishes with the feed snippet after this
  extraction_wait_seconds: 120     # stop waiting for article text after this
  max_inflight: 1                  # concurrent LLM requests, all kinds, whole process
  summarize_backfill_hours: 24     # unpublished stories older than this are not generated
  summarize_backfill_max: 40       # at most this many unpublished stories are worked on
  report_max_articles: 4
  report_total_words: 2400         # source words per report, shared by its articles
  report_max_tokens: 1400          # max_tokens for report requests (briefs use llm.summarizer's)
  report_timeout_seconds: 300      # timeout for report requests
  duplicate_threshold: 0.6
  verbatim_max_run: 12             # reject a report copying this many consecutive source words
  verbatim_max_ratio: 0.20         # ... or with more than this share of copied 8-word phrases
  verbatim_allow: []               # names/terms copied runs may consist of (title words always)
  retry_temperature_bump: 0.2      # added to the temperature on validation retries
  event_retention_days: 7          # story_events kept for stream resume (Last-Event-ID)
```

`report_concurrency`, `max_reports_per_run`, `report_auto_max_age_hours` and
`report_max_words_per_article` are still accepted
but ignored (`max_inflight` and the backfill limits replace them). `report_mode: off` publishes
stories with `skipped(reports_off)`. `DISCOVER_REPORT_MODE` overrides every profile's
`report_mode`. **Regenerate** and **Retry** queue a new version; the current one stays visible
until it is replaced.

### Diagnosing a slow or empty report

```bash
python -m app.summarize.diagnose --auto --kind both            # newest story waiting
python -m app.summarize.diagnose --story-id 42 --kind report --markdown
python -m app.summarize.diagnose --auto --dry-run              # prompt only, no LLM call
python -m app.summarize.diagnose --auto --kind report --full   # uncut prompts and answers
```

(Windows: `.venv\Scripts\python -m app.summarize.diagnose --auto`.) It prints the members with
text status and word counts, which are sent, the prompt size against `context_tokens`, then
each attempt's raw response, finish reason, usage, tokens/second, validation result and
warnings, then the validated text exactly as it would be stored. Prompts and answers are cut
to 1,500 characters unless `--full`. Nothing is written to the database.
`python -m app.summarize.compare` reports the same quality metrics as the Pipeline panel
(first-attempt pass rate, attempts per success, end-to-end seconds per story, failure
reasons) per endpoint.

### Configuring the endpoint

Each profile has two LLM roles, `summarizer` (briefs and reports) and `chat` (story Q&A, next
phase).
The worker uses `DISCOVER_SUMMARIZE_PROFILE`'s summarizer (default: the first profile);
`DISCOVER_SUMMARIZE_ENABLED=false` turns summaries off.

```yaml
llm:
  summarizer:
    base_url: ${LOCAL_LLM_BASE_URL:-http://localhost:8080/v1}
    api_key: ${LOCAL_LLM_API_KEY:-}
    model: ${LOCAL_LLM_MODEL:-local}
    temperature: 0.2           # defaults shown
    max_tokens: 600
    timeout_seconds: 120
    structured_output: json_schema   # or json_object / none
    disable_thinking: true     # sends chat_template_kwargs {"enable_thinking": false}
```

Cloud providers use the same code with different settings (see `market-monitor` in
`config/profiles.example.yaml`; set `disable_thinking: false` where unknown parameters are
rejected). Start llama.cpp so other machines can reach it, with a key, enough context for a
report's 4 articles x 600 words, and parallel slots matching `summaries.max_inflight`:

```bash
llama-server -m model.gguf --host 0.0.0.0 --port 8080 --api-key <key> -c 16384 -np 1
```

(`-c` is the total context, split across the `-np` slots: use `-c 32768 -np 2` for
concurrency 2.)

### Demo and evaluation

The demo and tests use a deterministic in-process fake LLM (no network) that writes briefs and
reports from the sources: about one brief in five gets an invented number (fallbacks show up),
one story's report fails validation (shows **Retry**; Retry then succeeds), and single-source
stories with little text show "Report unavailable". The demo generates and publishes every
story at startup (the fixtures are older than the backfill window, so the demo widens it).
Demo options: `--report-mode off`,
`--fake-llm-delay 3` (slow fake answers to see the generating state), and `?cards=lead_only`
on a feed URL previews the lead-only cards. To use your server instead:

```bash
export LOCAL_LLM_BASE_URL=http://<llm-host>:8080/v1 LOCAL_LLM_API_KEY=<key>
make demo DEMO_ARGS="--real-llm --model <model>"     # briefs+reports for the newest 10 at startup
```

```powershell
$env:LOCAL_LLM_BASE_URL = "http://<llm-host>:8080/v1"; $env:LOCAL_LLM_API_KEY = "<key>"
.\demo.cmd --real-llm --model <model>
```

Or put `LOCAL_LLM_BASE_URL` / `LOCAL_LLM_API_KEY` in `.env` (see `.env.example`) and just run
`demo.cmd --real-llm --model <model>`.

Evaluate the real model (nothing is stored; `--db .demo/demo.db` reads the demo's stories,
`--fake` runs offline). On Windows use `.venv\Scripts\python` instead of `python`:

```bash
python -m app.summarize.smoke --profile personal-reader --limit 5 --kind both --db .demo/demo.db
python -m app.summarize.compare --stories 20 --kind both --markdown --db .demo/demo.db \
  --endpoints qwen=http://<host>:8080/v1:qwen3,gemma=http://<host2>:8080/v1:gemma3
python -m app.summarize.capture --kind brief --out tests/fixtures/llm/real_response.json --db .demo/demo.db
```

`--kind brief|report|both` (default both; capture also takes `summary`). `smoke` prints each
brief and report with citations, latency, tokens/sec, word counts and the validation result;
`compare` prints, per endpoint and kind, pass rate, fallbacks, failures, skipped reports,
median latency, tokens/sec and average lead / bullet / report word counts, then the briefs and
reports side by side (API keys: `LLM_API_KEY_<NAME>`, else `LOCAL_LLM_API_KEY`); `capture`
saves one raw response that `tests/test_summarizer.py` then parses (skipped when absent). Only the fake LLM
is tested here; real-model quality is unverified until you run these.

### Story Q&A (follow-up questions)

Each story page has an **Ask a follow-up** panel (under the report) answered by the profile's
`llm.chat` role, only from the story's numbered sources, with `[n]` citations. Without
`llm.chat` (or while its endpoint is unreachable) the panel is disabled with an explanation.

- API (same auth as `/api/v1`): `POST /api/v1/stories/{id}/ask?profile=` with
  `{"question": "...", "conversation_id": null}` streams SSE events `meta`, `waiting` (model
  busy), `token`..., then `citations` + `done` (verification, tokens, latency, time to first
  token) or `error` (`timeout`, `model_busy`, `unreachable`, `upstream_error`, `empty_output`,
  `http_<status>`). `GET|DELETE /api/v1/conversations/{id}`, `GET /api/v1/stories/{id}/followups`
  (3 suggested questions), `GET /api/v1/chat` (availability). Example: `examples/ask.py`.
- Context: up to 5 sources numbered like the brief, sharing `chat_context_words` (3000) words,
  plus the published brief and report and the last `chat_history_turns` (6) turns; trimmed to
  `llm.chat.context_tokens` (oldest turns, then the report, then the sources). Source text is
  wrapped in `<source>` blocks marked untrusted and is never returned by any endpoint.
- Answers are checked afterwards (no retries): `[n]` in range, numbers present in the sources,
  and (warning) uncited factual sentences; the page shows "Some details could not be verified".
- Conversations have no accounts: the id is the key (kept in localStorage per story); idle ones
  are deleted after `chat_retention_days` (30).
- Priority: while a chat request waits or streams, the generation worker starts no new LLM
  request (`chat_priority: true`; only when `llm.chat` uses the summarizer's endpoint); a running
  brief/report is never aborted. `chat_max_concurrent` (1) caps chats per profile. Closing the
  connection (Stop, closed tab) closes the upstream request so llama.cpp stops generating.
- Suggested follow-ups (`summaries.followups: true`, needs `llm.chat`): 3 questions of at most
  12 words, generated with the summarizer only when no brief or report is waiting; not part of
  the publish gate.
- llama-server: `-np` slots share `-c`. With `-np 1` chat and generation take turns (chat
  first). `-np 2 -c 32768` gives each 16k so chat never waits for a report; then set
  `summaries.max_inflight: 2`.
- Try it with the real model (never run in CI):
  `python -m app.chat.smoke --story-id N --question "..." [--history Q A ...] [--db .demo/demo.db]`
  (Windows: `.venv\Scripts\python -m app.chat.smoke ...`) prints time to first token, tokens/s,
  the answer, citations and checks. `python -m app.summarize.compare --chat --endpoints ...
  --stories 5` asks 5 canned questions per story (the last is unanswerable) across endpoints.
  Demo states: `make demo DEMO_ARGS="--fake-chat-mode stream_error"` (or `invented_number`,
  `stall`, ...).

Source flags in the profile YAML (copied onto existing DBs at startup): `aggregator: true`
(Hacker News: citations show the article's domain "via Hacker News") and `first_party: true`
(SEC/Fed releases, company blogs: briefs/reports attribute claims to the publisher; also used
when a single-publisher story's headline names the publisher's domain, e.g. ai.meta.com "Meta ...").

## Keyword alerts

Alert rules notify you the moment a matching article is **ingested**: they look at the title
and feed summary right after the feed is stored, before text extraction, clustering, briefs
or the publish gate (so an unpublished story can alert; the story link is added only once the
story is published). Manage them at **Alerts** (`/p/<slug>/alerts`, admin like Settings):
rules, channels with **Send test**, a 7-day **Preview** that sends nothing, and the
notification log (filter by rule/status, **Retry** failed). Feed cards show a bell badge
with the rules that alerted; Settings > Pipeline shows rules, alerts fired in 24 h, queued,
held and failed counts and each channel's last error.

```yaml
profiles:
  market-monitor:
    alert_rules:                    # seeded into the DB once; edit them in the UI afterwards
      - name: Big tech tickers
        include: [$AAPL, $NVDA]     # $AAPL = case-sensitive "AAPL" (also matches "$AAPL")
        exclude: [proposed rule]    # excludes win over includes
        match: any                  # or all
        match_in: [title, summary]  # add `text` to match the extracted text too (checked
                                    # again after extraction)
        sources: [CNBC Markets]     # optional; default all the profile's sources
        priority: high              # low | default | high
        channels: [phone, email]
        quiet_hours: {start: "22:00", end: "07:00"}   # in alerts.timezone; held -> digest
        max_per_hour: 10            # overflow is held and sent as one digest
        notify_on_update: false     # true: one update (at most hourly) when a new source joins
    alerts:
      timezone: Europe/Berlin
      public_url: http://nas.local:8000   # story links in notifications
      alert_max_age_hours: 6              # older articles never alert (no backfill floods)
      notification_retention_days: 30
      max_attempts: 5                     # exponential backoff between attempts
      channels:
        - {id: phone, type: ntfy, server: https://ntfy.sh, topic: my-markets, token: ${NTFY_TOKEN}}
        - {id: hook, type: webhook, url: https://example.com/hook, secret: ${ALERT_WEBHOOK_SECRET}}
        - id: email
          type: email
          host: smtp.example.com
          port: 587
          tls: starttls                   # starttls | ssl (port 465) | none
          username: ${SMTP_USER}
          password: ${SMTP_PASSWORD}
          sender: alerts@example.com
          to: [me@example.com]
```

Channel secrets (`token`, `secret`, `username`, `password`) must be `${ENV_VAR}` references:
only the variable name is stored and the value is read when sending; it never appears in the
DB, payloads, logs or errors. Each channel has `timeout_seconds` (default 10).

- **Webhook**: JSON POST with `X-Discover-Event`, `X-Discover-Delivery` (notification id) and
  `X-Discover-Signature: sha256=<hex>` = HMAC-SHA256 of the raw body with the secret.
- **ntfy**: JSON publish to the server root (topic, title, message, priority 2/3/5, tags,
  click = story page if published, else the article), optional bearer token.
- **Email**: plain text + simple HTML, `Message-ID` from the notification id.

**Exactly once:** a rule fires at most once per story lineage. Merged stories keep their
history: an article joining a story (or a story redirected into another) that already alerted
for the rule does not alert again, and an article that the clusterer would put into an alerted
story is suppressed even before clustering runs. A unique constraint on (rule, lineage, kind,
slot) makes concurrent evaluations safe. Sent notifications are never re-sent after a
restart; queued ones resume (receivers can dedupe on the delivery id / Message-ID).

**Demo sink:** `make demo` / `demo.cmd` start a local fake webhook + ntfy + SMTP server,
point the market-monitor channels at it and ingest a few "live" market items, so alerts fire
(sent, held in quiet hours, and failed via a demo channel that always fails). See what was
delivered at <http://localhost:8000/demo/sink>. Standalone:
`python -m app.alerts.sink --port 8099 --smtp-port 8025` (PowerShell: same command with
`.venv\Scripts\python`).

## Public API (v1)

`/api/v1` is the stable read API for other applications. It serves published stories only
and never the extracted article text. Contract: [`docs/openapi.v1.json`](docs/openapi.v1.json)
(also `GET /api/v1/openapi.json`; browse everything at `/docs`).

| Endpoint | |
| --- | --- |
| `GET /api/v1/profiles`, `/profiles/{slug}`, `/profiles/{slug}/topics` | profiles, sources, topics |
| `GET /api/v1/profiles/{slug}/feed` | `topic`, `limit` (1-100), `cursor`, `since` (first published after), `updated_since` (current version published after), `include=card\|full` (`full` adds `report` and `sources`) |
| `GET /api/v1/stories/{id}` | brief, report (or its `report_state`/`report_reason`), sources with raw URLs, model, versions; 301 for merged ids, 404 unless published |
| `GET /api/v1/profiles/{slug}/feed.atom` | Atom: title = headline, content = lead + bullets, links to the story page and sources |
| `GET /api/v1/stream` | SSE, see below |
| `GET/POST /api/v1/profiles/{slug}/alert-rules`, `PUT/DELETE .../alert-rules/{id}` | alert rules (writes need the admin key) |
| `GET /api/v1/profiles/{slug}/notifications` | notification log; `rule_id`, `status`, `limit` |
| `POST /api/v1/alert-rules/{id}/preview` | matches of the last 7 days, sends nothing (admin) |
| `GET /api/v1/health`, `/api/v1/version` | |

```bash
curl -s 'localhost:8000/api/v1/profiles/personal-reader/feed?limit=5' | python -m json.tool
curl -s 'localhost:8000/api/v1/profiles/personal-reader/feed?include=full&cursor=<next_cursor>'
curl -s localhost:8000/api/v1/stories/42
curl -N 'localhost:8000/api/v1/stream?profile=personal-reader&include=card'
curl -N -H 'Last-Event-ID: 120' localhost:8000/api/v1/stream    # resume after event 120
curl -H "Authorization: Bearer $DISCOVER_API_KEY" localhost:8000/api/v1/profiles
```

PowerShell:

```powershell
Invoke-RestMethod 'http://localhost:8000/api/v1/profiles/personal-reader/feed?limit=5'
$h = @{ Authorization = "Bearer $env:DISCOVER_API_KEY" }
(Invoke-RestMethod 'http://localhost:8000/api/v1/stories/42' -Headers $h).brief.lead
curl.exe -N -H "X-API-Key: $env:DISCOVER_API_KEY" 'http://localhost:8000/api/v1/stream?since_id=0'
```

**Stream.** `GET /api/v1/stream?profile=&topic=&include=id|card|full&since_id=` sends a
`retry: 5000` hint, a `hello` event (`{latest_id, resume_from}`), then events
`story.published`, `story.updated` and `story.retracted`, each with `id: <n>` (increasing)
and `data: {"id", "type", "story_id", "version", "profiles"[, "story"]}`, plus a `heartbeat`
event every 15 s (`heartbeat=` to change). Reconnect with the `Last-Event-ID` header (what
`EventSource` does by itself) or `since_id` to get every event you missed within
`event_retention_days`; without either the stream starts with new events. A slow client never
holds up the pipeline: its queue overflows and it catches up from the table.

Examples: [`examples/client.py`](examples/client.py) (httpx: prints the feed, follows the
stream and resumes after a disconnect; `python examples/client.py --base http://nas:8000`)
and [`examples/stream.js`](examples/stream.js) (`EventSource`, browser or Node).

**Versioning.** Within v1 changes are additive only: new endpoints, new optional parameters,
new response fields (ignore unknown fields). Removing or renaming anything, or changing a
type or meaning, ships as `/api/v2`. The unversioned `/api/...` read routes still work for one
release as deprecated aliases (`Deprecation: true` and `Link: <...>; rel="successor-version"`
headers); `/api/admin/*` and the topic write routes are not versioned. `make openapi`
(`python -m app.openapi`) regenerates the snapshot; `tests/test_api_v1.py` fails while it is
out of date, so API changes are always deliberate.

### Authentication (NAS / exposed port)

Off by default. Before exposing the port (e.g. on a NAS), set both in `.env`:

```bash
DISCOVER_API_KEYS=key-for-app-1,key-for-app-2   # /api/v1 and the legacy read API
DISCOVER_ADMIN_KEY=a-long-random-admin-key       # /api/admin/*, topic changes, Settings
```

Generate keys with `python -c "import secrets; print(secrets.token_urlsafe(32))"` (PowerShell:
the same with `.venv\Scripts\python`). Clients send `Authorization: Bearer <key>` or
`X-API-Key: <key>`. With `DISCOVER_ADMIN_KEY` set, Settings (and every button that changes
something) redirects to `/login`, which sets a signed, HttpOnly, SameSite=Strict cookie for 30
days; the admin key also works as an API key. Feed and story pages stay readable, and the id-only
`/api/profiles/{slug}/stream` and `/published` endpoints the pages use stay open. Keys are
compared in constant time, never logged and never stored; startup logs only whether auth is
enabled. Put a TLS reverse proxy in front if the port leaves your LAN.

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
`python -m ruff format --check .`, `python -m pytest`, `python -m uvicorn app.main:app --reload`,
`python -m app.openapi` (= `make openapi`).

## Configuration

- Profiles live in a YAML file; see [`config/profiles.example.yaml`](config/profiles.example.yaml).
  Copy it to `config/profiles.yaml` (git-ignored) and point `DISCOVER_CONFIG_PATH` at it.
- YAML values may reference env vars: `${OPENAI_API_KEY}` or `${VAR:-default}`.
  Put secrets in the environment or a git-ignored `.env` (see `.env.example`). Both docker
  compose and local runs (uvicorn, `make demo`, `demo.cmd`, the `app.summarize` commands) read
  `.env`, so `${LOCAL_LLM_BASE_URL}` etc. resolve either way; variables set in the shell win.

| Env var | Default |
| --- | --- |
| `DISCOVER_CONFIG_PATH` | `config/profiles.yaml` |
| `DISCOVER_DATABASE_URL` | `sqlite:///./data/discover.db` (`/data/discover.db` in Docker) |
| `DISCOVER_SCHEDULER_ENABLED` | `true` |
| `DISCOVER_LOG_LEVEL` | `info` |
| `DISCOVER_FETCH_TIMEOUT_SECONDS` | `10` |
| `DISCOVER_REFRESH_JITTER_SECONDS` | `60` |
| `DISCOVER_API_KEYS` | unset (API open); comma-separated keys, see [Authentication](#authentication-nas--exposed-port) |
| `DISCOVER_ADMIN_KEY` | unset (admin open); guards `/api/admin/*`, topic writes and Settings |
| `DISCOVER_SOURCE_AUTO_DISABLE_FAILURES` | `10` consecutive failures before a source is disabled |
| `GUARDIAN_API_KEY`, `NEWSDATA_API_KEY`, `X_BEARER_TOKEN` | unset; referenced by API sources as `api_key: ${NAME}` |
| `DISCOVER_CONTACT_EMAIL` | unset; appended to the fetch User-Agent. Set it if you use SEC feeds (startup warns otherwise). |
| `DISCOVER_EXTRACT_INTERVAL_MINUTES` | `15` (extract + cluster job) |
| `DISCOVER_EXTRACT_MAX_ARTICLES` | `50` per run |
| `DISCOVER_EXTRACT_MIN_WORDS` | `80` |
| `DISCOVER_EXTRACT_DOMAIN_DELAY_SECONDS` | `2` |
| `DISCOVER_CLUSTER_*` | see [Stories](#stories-text-extraction-and-clustering) |
| `DISCOVER_IMAGES_DIR` | `data/images` (`/data/images` in Docker); see [Card images](#card-images) |
| `DISCOVER_NO_IMAGE_STYLE` | unset; overrides `images.no_image_style` |
| `DISCOVER_IMAGES_ALLOW_HOSTS` | unset; comma-separated hosts exempt from the image SSRF guard |
