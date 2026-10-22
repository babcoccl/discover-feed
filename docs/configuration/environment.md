# Environment variables

Process-level settings come from `DISCOVER_*` env vars (pydantic `Settings`,
`app/settings.py`). Provider and channel secrets are referenced **by name** in
the YAML (`${VAR}`) — values live only here or in `.env`.

## `DISCOVER_*` settings

| Env var | Default | What it does |
| --- | --- | --- |
| `DISCOVER_CONFIG_PATH` | `config/profiles.yaml` | Path to the profiles YAML |
| `DISCOVER_ENV_FILE` | `.env` | Which dotenv file local runs load |
| `DISCOVER_DATABASE_URL` | `sqlite:///./data/discover.db` | SQLAlchemy URL (`/data/discover.db` in Docker) |
| `DISCOVER_SCHEDULER_ENABLED` | `true` | Master switch for all APScheduler jobs |
| `DISCOVER_LOG_LEVEL` | `info` | Python logging level |
| `DISCOVER_FETCH_TIMEOUT_SECONDS` | `10` | Feed-fetch HTTP timeout |
| `DISCOVER_REFRESH_JITTER_SECONDS` | `60` | Spread first feed fetches and per-source jitter |
| `DISCOVER_CONTACT_EMAIL` | unset | Appended to the fetch User-Agent; SEC feeds expect it |
| `DISCOVER_SOURCE_AUTO_DISABLE_FAILURES` | `10` | Consecutive fetch failures before a source is disabled (0 = never) |
| `DISCOVER_API_KEYS` | unset | Comma-separated read keys for `/api/v1` + legacy reads ([auth](../features/authentication.md)) |
| `DISCOVER_ADMIN_KEY` | unset | Guards `/api/admin/*`, topic writes, Settings (`/login`) |
| `DISCOVER_PIPELINE_INTERVAL_SECONDS` | `60` | How often extract + cluster runs (only new/pending articles) |
| `DISCOVER_EXTRACT_MAX_ARTICLES` | `50` | Articles fetched for text per pipeline run |
| `DISCOVER_EXTRACT_TIMEOUT_SECONDS` | `10` | Article-page fetch timeout |
| `DISCOVER_EXTRACT_MAX_BYTES` | `2097152` | Page size cap (2 MB) |
| `DISCOVER_EXTRACT_MIN_WORDS` | `80` | Fewer extracted words = `text_status` failed |
| `DISCOVER_EXTRACT_DOMAIN_DELAY_SECONDS` | `2` | Politeness delay between requests to one domain |
| `DISCOVER_IMAGES_DIR` | `data/images` | Card-image cache (`/data/images` in Docker) |
| `DISCOVER_NO_IMAGE_STYLE` | unset | Overrides `images.no_image_style` (`logo`/`text_only`/`gradient`) |
| `DISCOVER_IMAGES_ALLOW_HOSTS` | unset | Comma-separated hosts exempt from the image SSRF guard |
| `DISCOVER_OUTLETS_PATH` | `config/outlets.yaml` | Outlet registry (names/countries/kinds/groups) used for coverage labels |
| `DISCOVER_CLUSTER_THRESHOLD` | `0.45` | Min cosine similarity to join a story |
| `DISCOVER_CLUSTER_WINDOW_HOURS` | `72` | Article must be within this of the story's first/latest member |
| `DISCOVER_CLUSTER_SAME_SOURCE_THRESHOLD` | `0.7` | Stricter threshold when the story already has that source |
| `DISCOVER_CLUSTER_MIN_SHARED_TOKENS` | `2` | Significant tokens shared with the story title |
| `DISCOVER_SUMMARIZE_ENABLED` | `true` | Turn brief/report generation on/off |
| `DISCOVER_SUMMARIZE_PROFILE` | first profile | Whose `llm.summarizer` endpoint the worker uses |
| `DISCOVER_SUMMARIZE_INTERVAL_SECONDS` | `60` | Summary worker tick |
| `DISCOVER_ENRICH_INTERVAL_SECONDS` | `60` | Scheduled enrichment sweep: admits eligible stories, starts the worker for anything waiting |
| `DISCOVER_ENRICH_WATCHDOG_SECONDS` | `120` | Restarts the enrichment worker thread if it is not alive while stories wait |
| `DISCOVER_SUMMARIZE_MAX_PER_RUN` | `25` | Stories processed per worker tick |
| `DISCOVER_SUMMARIZE_RESUMMARIZE_MIN_MINUTES` | `30` | Min spacing before a story may regenerate |
| `DISCOVER_SUMMARIZE_MAX_ARTICLES_PER_STORY` | `5` | Members sent to a brief prompt |
| `DISCOVER_SUMMARIZE_MAX_WORDS_PER_ARTICLE` | `1200` | Words per member sent to a brief prompt |
| `DISCOVER_SUMMARIZE_FAILURE_LIMIT` | `3` | Consecutive endpoint failures before the worker pauses |
| `DISCOVER_SUMMARIZE_PAUSE_MINUTES` | `10` | Pause length after the failure limit |
| `DISCOVER_REPORT_MODE` | unset | Overrides every profile's `summaries.report_mode` |

## Other variables (`.env.example`)

Referenced from `profiles.yaml` as `${NAME}` — only the name is stored.

| Env var | Used for |
| --- | --- |
| `OPENAI_API_KEY` | Cloud provider key (market-monitor example) |
| `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_API_KEY`, `LOCAL_LLM_MODEL` | OpenAI-compatible endpoint for `llm.*` roles |
| `GUARDIAN_API_KEY`, `NEWSDATA_API_KEY`, `X_BEARER_TOKEN` | API ingestion sources (`api_key:` on a source) |
| `TINYFISH_API_KEY`, `BRAVE_API_KEY`, `EXA_API_KEY` | Web-search providers (`api_key:` on a provider) |
| `DISCOVER_TIMEZONE`, `DISCOVER_PUBLIC_URL` | Alert timezone / base URL for links in notifications |
| `NTFY_SERVER`, `NTFY_TOPIC`, `NTFY_TOKEN` | ntfy channel |
| `ALERT_WEBHOOK_URL`, `ALERT_WEBHOOK_SECRET` | Webhook channel + HMAC key |
| `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO` | Email channel |
| `LLM_API_KEY_<NAME>` | Per-endpoint key for `python -m app.summarize.compare` |

Missing `${ENV}` values for alert channels and sources are logged once at
startup and listed (names only) on the Alerts page and Settings > Pipeline.
