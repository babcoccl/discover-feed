# Config reference

Every key of `profiles.yaml`, grouped by block. Validation is strict: unknown
keys are rejected (`extra="forbid"`), so typos fail loudly at startup.

Env-var settings (`DISCOVER_*`) live in [Environment variables](environment.md).
`${VAR}` / `${VAR:-default}` expansion applies to every value.

## Profile

| Key | Default | What it does |
| --- | --- | --- |
| `id` | required | Slug (`a-z0-9_-`); feed at `/p/{id}`; unique across profiles |
| `name` | required | Display name |
| `description` | `""` | Shown on the profile page |
| `topics` | `[]` | Feed tabs — see [topics](#topics) |
| `keywords` | `[]` | Profile-level keyword list |
| `sources` | `[]` | Feeds and API adapters — see [sources](#sources) |
| `alert_rules` | `[]` | Keyword alert rules, seeded once — see [alert rules](#alert-rules) |
| `alerts` | defaults | Notification channels + settings — see [alerts](#alerts-block) |
| `llm` | required | `summarizer` + `chat` roles — see [LLM endpoints](llm.md) |
| `summaries` | defaults | Brief/report behaviour — see [summaries](#summaries) |
| `enrichment` | disabled | Web-search enrichment — see [enrichment](#enrichment) |
| `context` | disabled | Background/context links — see [context](#context) |

## Topics

| Key | Default | What it does |
| --- | --- | --- |
| `name` | required | Tab label; unique per profile (case-insensitive) |
| `include` | `[]` | Whole-word, case-insensitive keywords; empty = every article |
| `exclude` | `[]` | Any match excludes the article (wins over `include`) |
| `sources` | `[]` | Restrict to these source names; empty = all of the profile's |
| `enabled` | `true` | Disable without deleting |

## Sources

| Key | Default | What it does |
| --- | --- | --- |
| `id` | slug of `name` | Stable id shared across profiles; set it explicitly to survive renames |
| `name` | required | Display name |
| `type` | `rss` | `rss`, `atom`, `web`, `api`, `guardian`, `newsdata`, `x` |
| `url` | API home for API types | Feed URL; required for rss/atom |
| `enabled` | `true` | Disabled sources are not fetched |
| `refresh_minutes` | `30` | Fetch interval (plus jitter) |
| `tags` | `[]` | Labels (exported as OPML `category`) |
| `aggregator` | `false` | Feed links to other publishers (HN): citations show domain "via <name>" |
| `first_party` | `false` | Publisher is the subject (SEC/Fed, blogs): claims attributed to it |
| `params` | `{}` | API query — guardian `q`/`section`/`from_date`/`page_size`/`max_pages`, newsdata `q`/`country`/`language`/`category`/`max_pages`, x `query`/`users` (all allow `base_url`) |
| `api_key` | unset | Env var *name* holding the key (`api_key: ${GUARDIAN_API_KEY}`) |
| `daily_request_budget` | unset | Stop calling after this many requests per UTC day |
| `max_posts_per_run` | unset | Cap items kept per fetch (1-100) |
| `monthly_budget_usd` | unset | X: hard spend cap per UTC month — required to enable an X source |
| `cost_per_read_usd` | `0.005` | X: price per read for budget accounting |
| `social` | `false` | Short social posts: no extraction, no reports (set automatically for `x`) |

## Alert rules

| Key | Default | What it does |
| --- | --- | --- |
| `name` | required | Rule name; unique per profile |
| `enabled` | `true` |  |
| `include` | required | Keywords; `$AAPL` = case-sensitive ticker also matching "AAPL" |
| `case_sensitive` | `false` | Whole-rule override: match include/exclude verbatim |
| `exclude` | `[]` | Wins over `include` |
| `match` | `any` | `any` include matches, or `all` must |
| `match_in` | `[title, summary]` | Add `text` to re-check after extraction |
| `sources` | `[]` | Source names to restrict to; empty = all the profile's |
| `priority` | `default` | `low` / `default` / `high` (maps to ntfy priority 2/3/5) |
| `channels` | `[]` | Channel ids from `alerts.channels` |
| `quiet_hours` | unset | `{start: "22:00", end: "07:00"}` local `alerts.timezone`; held → digest |
| `max_per_hour` | `10` | Overflow is held and sent as one digest |
| `notify_on_update` | `false` | One update (max hourly) when a new source joins an alerted story |

## Alert channels

| Key | Default | What it does |
| --- | --- | --- |
| `id` | required | Slug; referenced by rules' `channels` |
| `type` | required | `webhook`, `ntfy`, `email` |
| `name` | `id` | Display name |
| `enabled` | `true` |  |
| `timeout_seconds` | `10` | Send timeout (max 120) |
| `url` | required for webhook | JSON POST target |
| `secret` | unset | `${ENV}` name — HMAC key for `X-Discover-Signature` |
| `server` | `https://ntfy.sh` | ntfy server root |
| `topic` | required for ntfy | ntfy topic |
| `token` | unset | `${ENV}` name — bearer token for protected topics |
| `host` | required for email | SMTP host |
| `port` | `587` | SMTP port |
| `tls` | `starttls` | `starttls` / `ssl` (port 465) / `none` |
| `username`, `password` | unset | `${ENV}` names — SMTP login |
| `sender` | required for email | From address |
| `to` | required for email | Recipient list |

## Alerts block

| Key | Default | What it does |
| --- | --- | --- |
| `channels` | `[]` | Channel list (above); seeded once, then managed in the UI |
| `alert_max_age_hours` | `6` | Older articles never alert (no backfill floods) |
| `notification_retention_days` | `30` | Notification log retention |
| `max_attempts` | `5` | Send attempts with exponential backoff (max 20) |
| `timezone` | `UTC` | IANA zone for quiet hours and notification times |
| `public_url` | `http://localhost:8000` | Base URL for story links in notifications |

## Summaries

Read from the YAML on every start (not stored in the DB).

| Key | Default | What it does |
| --- | --- | --- |
| `card_style` | `lead_bullets` | `lead_only` shows cards without bullets |
| `report_mode` | `auto` | `off` publishes with `skipped(reports_off)`; `on_demand` reads as `auto` |
| `publish_requires_report` | `true` | Publish only once the report is final (ready/skipped/failed) |
| `publish_max_wait_minutes` | `30` | Then publish with "Detailed report still generating" |
| `brief_fallback_hours` | `6` | A failed brief publishes with the feed snippet after this |
| `extraction_wait_seconds` | `120` | Cluster a story after this long even if text is pending |
| `max_inflight` | `1` | Concurrent LLM requests, all kinds, process-wide |
| `summarize_backfill_hours` | `24` | Unpublished stories older than this are not generated |
| `summarize_backfill_max` | `40` | At most this many unpublished stories are worked on |
| `report_max_articles` | `4` | Members sent to a report prompt (one per source first) |
| `report_total_words` | `2400` | Source words per report, shared by its articles |
| `report_max_tokens` | `1400` | `max_tokens` for report requests (briefs use the role's) |
| `report_timeout_seconds` | `300` | Timeout for report requests |
| `duplicate_threshold` | `0.6` | Bullet/paragraph token overlap that counts as duplicate |
| `verbatim_max_run` | `12` | Reject a report copying this many consecutive source words |
| `verbatim_max_ratio` | `0.20` | Or with more than this share of copied 8-word phrases |
| `verbatim_allow` | `[]` | Terms copied runs may consist of (title words always allowed) |
| `retry_temperature_bump` | `0.2` | Added to temperature on validation retries |
| `event_retention_days` | `7` | `story_events` kept for stream resume |
| `chat_context_words` | `3000` | Source words sent with a Q&A question |
| `chat_history_turns` | `6` | Earlier Q&A pairs sent with a question |
| `chat_retention_days` | `30` | Idle conversations deleted after this |
| `chat_priority` | `true` | Chats block new generation requests on the shared endpoint |
| `chat_max_concurrent` | `1` | Chats streaming at once per profile |
| `followups` | `true` | 3 suggested questions per story (needs `llm.chat`) |
| `report_concurrency`, `max_reports_per_run`, `report_auto_max_age_hours`, `report_max_words_per_article` | — | Deprecated, accepted but ignored |

## Enrichment

Per profile (`enrichment:`). The UI toggle in Settings > Pipeline overrides
`enabled` for an existing database. Thresholds and providers come from the
`DISCOVER_SUMMARIZE_PROFILE` profile (else the first).

| Key | Default | What it does |
| --- | --- | --- |
| `enabled` | `false` |  |
| `providers` | `[tinyfish]` | Ordered search providers — see [providers](#search-providers) |
| `target_sources` | `3` | Stories with fewer distinct publishers are searched |
| `max_age_hours` | `48` | Only stories updated this recently |
| `max_added_sources` | `3` | Max independent publishers added per story |
| `wait_seconds` | `180` | How long a new story's brief waits for enrichment |
| `concurrency` | `2` | Stories searched at once |
| `min_snippet_similarity` | `0.25` | Hit title+snippet vs headlines (TF-IDF cosine) |
| `min_text_similarity` | `0.35` | Extracted page text vs the story |
| `min_shared_entities` | `2` | Shared names/numbers (rule B) |
| `min_snippet_strong` | `0.45` | Rule B: snippet this strong can carry weaker text |
| `min_text_floor` | `0.25` | Rule B: text must still reach this |
| `link_only` | `false` | Keep strong-snippet unfetchable hits as link-only members |
| `max_time_gap_hours` | `72` | Publication-time gap vs the story |
| `duplicate_overlap` | `0.6` | 5-word shingle overlap that counts as a copy |
| `deny_domains` | `[]` | Blocked candidate domains |
| `social_domains` | `[]` | Extra hosts treated as social |
| `aggregator_domains` | `[]` | Extra hosts treated as aggregators |
| `publisher_aliases` | `bbc.co.uk→bbc.com`, `guardian.co.uk→theguardian.com` | Domains that are the same publisher |
| `extra_rules` | on | Rules C/D below |
| `quality` | on | Source-quality gate — see [quality](#enrichment-quality) |
| `reenrich_hours` | `24` | Admin "enrich now" ignores stories enriched more recently (unless `story_id`) |
| `query_mode` | `heuristic` | `llm` asks the summarizer for queries |
| `resummarize_on_enrichment` | `true` | Late sources regenerate a published story once |
| `cache_hours` | `24` | Search-result cache |
| `language` | `en` | Hits in another language are rejected |
| `max_candidates` | `10` | Candidates fetched per story |
| `max_queries_per_story` | `2` | Requests per provider per story |
| `min_title_similarity` | `0.30` | Snippet-less hits (GDELT): title similarity + shared entity |
| `empty_cache_minutes` | `10` | Empty results cached this long at most (0 = never) |
| `second_pass_hours` | `3` | Under-target stories searched again after this (0 = off) |
| `late_regen_max_per_hour` | `6` | Cap on enrichment-triggered regenerations |
| `skip_title_patterns` | `^Show HN`, `^Ask HN`, `^Tell HN`, `^Opinion`, `^Letters` | Headline regexes: never searched |
| `strip_title_prefixes` | `Show HN`, `Analysis`, `Exclusive`, `Live`, … | Boilerplate removed before building queries |
| `min_signal_tokens` | `1` | Headlines with fewer distinct names/numbers are skipped (0 = off) |
| `skip_url_patterns` | `/commentisfree/`, `/opinion/`, `/opinions/`, `/comment/`, `/columnists/`, `/blogs/`, `/live/` | URL-path substrings: searched and never match |

### extra_rules

| Key | Default | What it does |
| --- | --- | --- |
| `enabled` | `true` | Rules C and D accept real coverage scoring under `min_text_similarity` |
| `rule_c_entities` | `5` | Rule C: shared names/numbers … |
| `rule_c_text` | `0.25` | … plus text similarity at least this → `entities+c` |
| `rule_d_title` | `0.55` | Rule D: hit title this similar to the story headlines … |
| `rule_d_entities` | `3` | … plus this many shared entities … |
| `rule_d_text` | `0.22` | … and text at least this similar → `title+d` |

### Enrichment `quality`

The [source-quality gate](../features/enrichment/source-quality.md): accepted
candidates are flagged, never rejected. Confirmed farms in `deny_domains` are
hard-denied.

| Key | Default | What it does |
| --- | --- | --- |
| `suspicious_tlds` | `edu.pl`, `club`, `shop`, `online`, `site`, `xyz`, `top`, `icu`, `info`, `biz`, `click`, `link`, `work`, `buzz`, `vip`, `website`, `space`, `today`, `live` | Domain endings that flag a hit (`tld:<suffix>`) |
| `suspicious_patterns` | `(news\|wire\|daily\|hub\|insider\|feed)` token on the registrable domain | Keyword-stuffed farm names flag (`name:<token>`); fused names like `wired.com` do not |
| `deny_domains` | farms confirmed in live evals | Hard-denied like `deny_domains`; a YAML list replaces the defaults entirely |

## Search providers

Each entry under `enrichment.providers` / `context.providers`
(`SearchProviderConfig`):

| Key | Default | What it does |
| --- | --- | --- |
| `name` | required | `tinyfish`, `gdelt`, `searxng`, `guardian`, `newsdata`, `brave`, `exa` |
| `base_url` | provider default | API root; **required for `searxng`** (with `json` in `search.formats`) |
| `api_key` | unset | Env var *name* holding the key |
| `budget_source` | unset | Share another source's daily request budget |
| `daily_request_budget` | unset | Requests/day cap, counted in `source_usage` as `search:<name>` |
| `monthly_budget_usd` | unset | Monthly spend cap |
| `cost_per_request_usd` | `0.0` | Price per request for the spend cap |
| `min_interval_seconds` | tinyfish `2`, gdelt `10`, else `1` | Seconds between requests (gdelt floored at 5; tinyfish at 2 unless `override_min_interval`) |
| `override_min_interval` | `false` | Allow tinyfish `min_interval_seconds` below 2 |
| `search_type` | `news` | tinyfish `domain_type`: `news` or `web` |
| `max_results` | `10` | Results requested per query (max 50) |
| `timeout_seconds` | `15` | Per-request timeout |

## Context

Per profile (`context:`). Domain rules (`deny_domains`, `social_domains`,
`aggregator_domains`, `publisher_aliases`) come from the profile's `enrichment`
block.

| Key | Default | What it does |
| --- | --- | --- |
| `enabled` | `false` |  |
| `providers` | `[tinyfish]` | Same provider registry; `exa` also does `find_similar` |
| `max_angles` | `3` | Search angles generated per story |
| `max_items` | `3` | Context links kept per story |
| `max_per_domain` | `1` | Accepted links per story from one domain (`domain_cap`) |
| `min_words` | `300` | Pages extracting less are rejected |
| `similarity_floor` | `0.10` | Below: `off_topic` |
| `similarity_ceiling` | `0.45` | At/above: `same_event` (must be > floor) |
| `min_shared_entities` | `1` | Shared names/numbers required |
| `min_shared_entities_heuristic` | `2` | Same, for heuristic (non-LLM) angles |
| `min_priority` | `0` | Eligible when `story.priority` is above this … |
| `min_publishers` | `2` | … or it has this many distinct publishers |
| `daily_request_budget` | `30` | Context's own cap (`context:<name>` usage rows) |
| `llm_judge` | `true` | LLM scores pages 0/1/2; keeps only `adds_context: 2` |
| `reject_flagged` | `true` | Drop candidates the quality gate flags |
| `max_opinion` | `1` | Opinion/column links kept per story (0 = none) |
| `same_event` | on | Deterministic same-event signals — below |
| `language` | `en` | Hit language must match when reported |
| `cache_hours` | `24` | Search-result cache |
| `empty_cache_minutes` | `10` | Empty results cached this long at most |
| `max_age_hours` | `168` | Stories eligible while published within this |
| `concurrency` | `2` | Stories processed at once |

### `same_event` rules

| Key | Default | What it does |
| --- | --- | --- |
| `numbers_min_shared` | `2` | Shared numeric tokens (no single digits or 4-digit years) → `same_event` |
| `numbers_text_chars` | `600` | Page prefix scanned for numbers (title + first N chars) |
| `window_hours` | `72` | Published within ± this of `first_at` = contemporaneous |
| `undated_min_entities` | `3` | Undated pages sharing this many entities count as contemporaneous |
| `max_title_similarity` | `0.30` | Contemporaneous page must stay under this title similarity … |
| `max_text_similarity` | `0.30` | … and this text similarity (both) |
| `entities_min` | `3` | Title shares this many story entities AND an event word → `same_event` |
| `event_terms` | verbs/nouns list | Event words (`announce`, `launch`, `acquire`, `breach`, `verdict`, …; full list `DEFAULT_EVENT_TERMS` in `app/context/rules.py`) |

## Images

Top-level `images:` block:

| Key | Default | What it does |
| --- | --- | --- |
| `cache` | `true` | Download validated images once, serve resized WebP from `/img/{key}` |
| `max_mb` | `500` | LRU cap of the image cache |
| `no_image_style` | `logo` | `logo` / `text_only` / `gradient` fallback (`DISCOVER_NO_IMAGE_STYLE` overrides) |
| `backfill_days` | `7` | Backfill images of stories published this recently |
| `backfill_per_run` | `20` | Articles per backfill run |
| `backfill_interval_minutes` | `10` | `images-backfill` job interval |
| `per_run` | `50` | New articles checked per pipeline run |
| `timeout_seconds` | `10` | Image fetch timeout |
| `max_bytes` | `3145728` | Image size cap (3 MB, header and streamed) |
| `min_width`, `min_height` | `320`, `180` | Smaller images rejected |
| `tiny_feed_width` | `640` | Page image wins over a feed image narrower than this |
| `cache_width` | `640` | Cached WebP width |
| `generic_threshold` | `3` | Same URL on this many articles of one domain = `generic` |
| `max_pixels` | `40000000` | Decompression-bomb guard (Pillow) |
