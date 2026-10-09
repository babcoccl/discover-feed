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

## Briefs and detailed reports

Stories get two LLM-written artifacts (Perplexity Discover style), both with `[n]` citations
that link to each article's own permalink (`Article.url`):

- **Brief** (feed cards and the top of the story page): one lead sentence (at most 30 words,
  what happened) plus exactly three bullets (at most 28 words each: what happened, why it
  matters or context, what's next or a key detail). Cards show the image, title, lead, the
  three bullets, source chips and relative time; `card_style: lead_only` hides the bullets.
- **Detailed report** (story page, below the brief): 3-5 paragraphs, about 250-450 words,
  paraphrasing the sources, then the numbered **Sources** list. Every card opens its story
  page (single-source cards also have a "Read on <source>" link to the article). `[n]` is the same article in
  the brief, the report and the Sources list. The page shows the model and when it was written.

Written by an OpenAI-compatible chat completions endpoint (llama.cpp `llama-server`, vLLM,
Ollama, OpenAI...). Only the numbered sources' text goes into the prompt; extracted article
text is never returned by the public API or rendered in a page.

How it works (`app/summarize/`):

- After clustering, new stories and stories that gained members are queued (`summary_jobs`,
  one row per story and kind). A background worker (`DISCOVER_SUMMARIZE_INTERVAL_MINUTES`,
  default 5, plus right after each pipeline run) does briefs before reports, newest stories
  first: briefs `DISCOVER_SUMMARIZE_CONCURRENCY` (1) at a time and at most
  `DISCOVER_SUMMARIZE_MAX_PER_RUN` (25) per run, reports `report_concurrency` (1) at a time and
  at most `max_reports_per_run` (3). Briefs and reports debounce independently
  (`DISCOVER_SUMMARIZE_RESUMMARIZE_MIN_MINUTES`, 30). The worker has its own thread, so
  ingestion, extraction and clustering never wait for the LLM.
- Input: briefs use up to `DISCOVER_SUMMARIZE_MAX_ARTICLES_PER_STORY` (5) members and the first
  `DISCOVER_SUMMARIZE_MAX_WORDS_PER_ARTICLE` (1200) words of each; reports use
  `report_max_articles` (4) and `report_max_words_per_article` (600), keeping the brief's
  numbering and appending any extra sources. Members are numbered one per source first; the
  feed summary stands in when extraction failed.
- The answer must be JSON (`response_format` JSON schema; code fences/prose around it are
  tolerated). Briefs are rejected for a wrong bullet count, a lead that is not one sentence,
  length violations, missing or out-of-range citations, numbers (digits, %, amounts) not in a
  cited source, or near-duplicate bullets / a bullet repeating the lead (token overlap at or
  above `duplicate_threshold`, 0.6). Reports are rejected for fewer than 3 or more than 5
  paragraphs, fewer than 225 or more than 500 words (the 250-450 target plus 10%), uncited
  paragraphs, bad citations, invented numbers, duplicate paragraphs, or any run of 8+ words
  copied from a source.
- A rejected answer is retried once with the error. A brief that fails twice falls back to a
  Phase 4 style 2-3 sentence summary, then to the feed snippet (labelled **Feed snippet**),
  `status=fallback` with the reason. A report that fails twice is stored `failed` with the
  reason and nothing is shown in its place; the story page offers **Retry**.
- Stories whose only text is feed summaries (every member failed extraction, or a single member
  under 150 words) get a brief but no report: `status=skipped`, `reason=insufficient_text`,
  shown as "Report unavailable (not enough source text)".
- Endpoint errors (HTTP 5xx, timeouts) are `failed`, retried later with backoff, and after
  `DISCOVER_SUMMARIZE_FAILURE_LIMIT` (3) brief failures in a row the worker pauses for
  `DISCOVER_SUMMARIZE_PAUSE_MINUTES` (10). Report failures only back off their own job (a
  slow report timing out never stops briefs); meanwhile the story page says the endpoint
  failed and offers **Retry now**. Report requests use `report_timeout_seconds` (300).
- Every version is kept in `story_summaries` (`kind` brief|report, text, structured
  `content_json`, citations, the article id behind each `[n]`, basis, model, prompt version,
  tokens, latency). `input_hash` covers member ids, source text hashes, the kind's prompt
  version (`BRIEF_PROMPT_VERSION`, `REPORT_PROMPT_VERSION`), model and kind: unchanged input
  means no LLM call (a report that failed validation is not retried until its input changes or
  you force it). Pre-report databases are migrated in place (old rows become briefs and stay
  readable).
- Settings > Pipeline shows the queue, brief and report counts, running state, last error and
  tokens/sec, with a **Summarize now** button. API: `POST /api/admin/summarize` (all queued
  jobs in the background; `?story_id=` one story, awaited; `?kind=brief|report|both`, default
  both; `?force=true` ignores the cache; `?wait=true`).
- Story feed items and `GET /api/stories/{id}` have `brief` (`{lead, lead_citations, bullets:
  [{text, citations}], status: ok|fallback, style: brief|summary|snippet, basis, model,
  generated_at, sources}`), `report` (`{paragraphs: [{text, citations}], status:
  ok|failed|skipped, model, generated_at, sources}` or `null` when none was requested) and
  the deprecated `summary` (the brief's lead and bullets joined; removed next release). Each
  citation is `{index, source, headline, url}` with the raw `Article.url`.

### Report modes and the profile `summaries` block

Each profile can set (defaults shown; read from the YAML on every start, not stored in the DB):

```yaml
summaries:
  card_style: lead_bullets       # or lead_only (cards show the lead without bullets)
  report_mode: auto              # auto | on_demand | off
  report_auto_max_age_hours: 48  # auto: pre-generate reports for multi-source stories this recent
  max_reports_per_run: 3
  report_concurrency: 1
  report_max_articles: 4
  report_max_words_per_article: 600
  report_max_tokens: 1000        # max_tokens for report requests (briefs use llm.summarizer's)
  report_timeout_seconds: 300    # timeout for report requests (briefs use llm.summarizer's)
  duplicate_threshold: 0.6       # token overlap at which two bullets/paragraphs are duplicates
```

- `auto`: reports are generated in the background for multi-source stories updated within the
  window; any other story (single source, older) gets its report when its page is opened.
- `on_demand`: opening a story page queues its report.
- `off`: no reports.

While a report is queued or running the story page shows "Generating detailed report..." and
polls every 3 seconds (HTMX) for up to 2 minutes, then says it is still queued. **Regenerate**
(and **Retry** after a failure) forces a new brief/report. `DISCOVER_REPORT_MODE` overrides
every profile's `report_mode` (the demo's `--report-mode`).

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
report's 4 articles x 600 words, and parallel slots matching `DISCOVER_SUMMARIZE_CONCURRENCY`:

```bash
llama-server -m model.gguf --host 0.0.0.0 --port 8080 --api-key <key> -c 16384 -np 1
```

(`-c` is the total context, split across the `-np` slots: use `-c 32768 -np 2` for
concurrency 2.)

### Demo and evaluation

The demo and tests use a deterministic in-process fake LLM (no network) that writes briefs and
reports from the sources: about one brief in five gets an invented number (fallbacks show up),
one story's report fails validation (shows **Retry**; Retry then succeeds), and single-source
stories with little text show "Report unavailable". The demo writes reports for every
multi-source story at startup (the fixtures are older than the auto window); open a
single-source story to see an on-demand report. Demo options: `--report-mode on_demand|off`,
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

### Next phase: story Q&A

`story_context(session, story_id)` (`app/summarize/context.py`) already assembles a story's
sources and latest summary into a prompt-ready bundle with the summary's `[n]` numbering.
Planned: `POST /api/stories/{id}/ask` streaming over SSE with the `chat` role, answers grounded
in the story's sources with the same citations.

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
| `DISCOVER_CONTACT_EMAIL` | unset; appended to the fetch User-Agent. Set it if you use SEC feeds (startup warns otherwise). |
| `DISCOVER_EXTRACT_INTERVAL_MINUTES` | `15` (extract + cluster job) |
| `DISCOVER_EXTRACT_MAX_ARTICLES` | `50` per run |
| `DISCOVER_EXTRACT_MIN_WORDS` | `80` |
| `DISCOVER_EXTRACT_DOMAIN_DELAY_SECONDS` | `2` |
| `DISCOVER_CLUSTER_*` | see [Stories](#stories-text-extraction-and-clustering) |
