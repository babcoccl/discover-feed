# Web-search enrichment — overview

## What it does

Most stories arrive from one or two of your feeds. When a profile turns on
`enrichment`, each new or under-covered story (fewer than `target_sources`
publishers, updated within `max_age_hours`) is searched for on the web, and up
to `max_added_sources` independent publishers' pages are added to it, so briefs
and reports cite several outlets.

## How it works end to end

```mermaid
flowchart LR
    A[New story clustered] --> B{enrich_status: pending}
    B --> C[Query building<br/>headlines only]
    C --> D[Providers in order:<br/>tinyfish -> fallbacks]
    D --> E[Cheap rejects:<br/>same domain, social,<br/>denied, time, language]
    E --> F[Fetch top candidates<br/>robots + SSRF guard]
    F --> G{Acceptance rules<br/>text / B / C / D}
    G -->|accept| H[Member: origin=enrichment<br/>web:domain]
    G -->|reject| I[enrichment_candidates row]
    B -.->|no provider answered| J[retry with backoff<br/>never done]
    H --> K[Brief/report cite [n]<br/>like any member]
```

Details in code: `app/search/` (providers, pacing, cache) and `app/enrich/`
(query, filters, fetch, service). The pipeline:

1. After clustering the story is marked `enrich_status = pending`; its brief
   waits up to `wait_seconds` from that first admission (the summary worker
   skips it meanwhile, never blocking on the search; retries and second passes
   never hold it again). A background thread does the rest, `concurrency`
   stories at a time.
2. **Admission control:** only as many stories are marked as the first
   provider can serve within `wait_seconds`
   (`wait_seconds / (min_interval_seconds x max_queries_per_story)`, minus a
   running cooldown and the stories already waiting), newest first; the rest
   stay unmarked and are admitted on a later pipeline sweep (every minute).
   During backlog catch-up, stories older than the summarizer's
   `summarize_backfill_hours` are not enriched.
3. Stories other outlets are unlikely to cover are not searched — see
   [skips](queries.md#skips). Queries come from headlines only — see
   [queries](queries.md).
4. Each provider in `providers` is asked in order until enough sources are
   found — see [providers](providers.md).
5. Candidates are rejected before fetching, fetched politely, then accepted by
   the similarity rules — see [filters and acceptance](filters.md).
6. Accepted pages become members — see below. The
   [source-quality gate](source-quality.md) flags content-farm domains.
7. **Retry and second pass:** provider failure is not completion — see
   [operating](operating.md).
8. Sources found after a story was published regenerate it once
   (`enrich_regens`); at most `late_regen_max_per_hour` (6) per hour so
   enrichment cannot swamp the LLM; excess late sources are kept for the next
   regeneration (`enrich_reason = late_regen_deferred`). Turn it off with
   `resummarize_on_enrichment: false`.

## What accepted pages become

Members with `origin = enrichment`, source id `web:<domain>`, pinned to the
story through incremental clustering, merges and `--rebuild`. They:

- count toward "N sources", are cited and numbered in briefs and reports like
  any member, and are badged "found via web search" on the story page;
- appear in the API as `sources[].origin` (`feed` | `enrichment`);
- never fire alerts, never become cards of their own, and never make a story
  visible in a profile that does not follow one of its feed sources.

With `link_only: true`, a candidate whose snippet is strong
(`>= min_snippet_strong`) and shares the entity names but whose page cannot be
fetched (failed, blocked or paywalled — never robots-disallowed) is stored as
a member with `text_status = none`: the story page lists it as "also reported
by (link only)"; it is never summarized, cited or counted in the sources.

The same goes a step softer for unfetchable hits that pass the pre-filter and
match the story closely by headline (`title similarity >=
headline_only_title_similarity`, at least `headline_only_min_entities` shared
names/numbers, inside the time window — the snippet is not needed): they are
stored as members with `text_status = none` and `headline_only = true`, shown
in the story's Coverage section under "N outlets reporting, K headline only",
and never summarized or cited.

## Deep coverage (`enrichment.depth`)

Important stories get a deeper pass — more queries, result pages and
per-country repeats — so they end up with many distinct outlets (8+ instead of
0-3); niche stories keep the cheap standard behavior. A story is deep when its
feed members span at least `deep_min_publishers` distinct publishers, its
priority exceeds `deep_min_priority`, or an alert already fired on it
(`deep_on_alert`); `enabled: false` disables the tier entirely. Every request
counts against `daily_request_budget` in addition to each provider's own
budget — when it runs out, searches record `stopped: deep_budget` and the
story keeps what it found.

A deep pass sends up to `max_queries_per_story` query variants (core entities;
a paraphrase — the LLM's when `query_mode: llm`; key numbers; the main action
phrase), each on result pages `0..pages-1` (a page with no usable candidates
stops early, as does reaching `target_sources`). The strongest query is then
repeated once per `locations` country code, and one `include_domains` query
per missing `ensure_groups` group goes out (capped at `max_group_queries`).
Candidates are deduplicated by canonical URL and publisher alias across all
calls and follow the standard acceptance rules. `python -m
app.enrich.evaluate --deep` forces every story onto the deep tier and prints
the outlets-per-story distribution; the Pipeline panel shows the same numbers.
Every key: [enrichment depth](../../configuration/reference.md#enrichment-depth).

## Outlet registry

`config/outlets.yaml` is a git-tracked starter list of widely known outlets —
name, `domains`, `country` and `kind` (`wire` | `national` | `local` |
`trade` | `international` | `state` | `blog`) — plus free-form `groups` tags
that are yours to define (regions, beats, affiliations — the shipped list
leaves `groups: []` and asserts no political bias or third-party ratings).
Unknown domains fall back to a country guessed from the TLD where possible and
`kind: unknown`. Point `DISCOVER_OUTLETS_PATH` at a different file to replace
it. The registry labels story members: the Coverage section on the story page
groups outlets by kind, API v1 `sources[]` carries `outlet` / `country` /
`kind` / `groups` / `headline_only`, the summarizer picks its sources with a
diversity rule (new kinds, then countries, then groups) before recency, and
`enrichment.depth.ensure_groups` names groups from `groups` here.

## Configure

```yaml
enrichment:
  enabled: true
  providers:
    - name: tinyfish                # primary; key in TINYFISH_API_KEY (.env)
      min_interval_seconds: 2       # never below 2 (30 requests / minute)
    # - name: gdelt                 # opt-in fallback: rate-limits in practice
    #   min_interval_seconds: 10    # never below 5 s
    # - name: searxng
    #   base_url: http://searxng:8080
    # - name: brave
    #   api_key: BRAVE_API_KEY        # env var name, never the key itself
    #   daily_request_budget: 60
```

Enable per profile (`config/profiles.example.yaml` shows every option; the
Settings > Pipeline checkbox overrides the YAML for an existing database). The
thresholds and providers come from the summarize profile
(`DISCOVER_SUMMARIZE_PROFILE`, else the first). Every key:
[enrichment config](../../configuration/reference.md#enrichment).
