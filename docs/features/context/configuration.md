# Context links — configuration

## Enable

```yaml
context:
  enabled: false                 # Settings > Pipeline toggles it per profile
  providers:
    - name: tinyfish
      min_interval_seconds: 2
    # - name: exa                # semantic: find_similar() on the anchor URL
    #   api_key: EXA_API_KEY     # env var name, never the key
  daily_request_budget: 30       # context's own cap on top of shared budgets
```

Domain rules (`deny_domains`, `social_domains`, `aggregator_domains`,
`publisher_aliases`) come from the profile's `enrichment` block. Every key:
[context config](../../configuration/reference.md#context).

## Filters

`app/context/filters.py` rejects before and after fetching: same domain as any
member, social / aggregator / deny-listed domains (the shared enrichment lists
plus the DB block list — "Block this domain" on a context link writes there
too), already-ingested URLs, non-HTTP URLs, robots/fetch/paywall failures,
pages under `min_words` (300), near-duplicates of any member or accepted page
(5-word shingle overlap).

The page must share `min_shared_entities` (1; 2 when the angle was heuristic)
names/numbers with the story and land inside the TF-IDF band
`similarity_floor` (0.10) ≤ score < `similarity_ceiling` (0.45): below is off
topic, at or above is the same event — both rejected.

At most `max_per_domain` (1) accepted links per story may come from one
domain; further accepted pages on it are rejected `domain_cap`.

## Same-event signals

Two more rejections run before the (expensive) judge: the shared
source-quality gate (farm-pattern domains and SEO-template titles like
`[2026] ... Comparison`; `context.reject_flagged`, default true — enrichment
keeps-and-marks them, context drops them) and the deterministic same-event
signals (`same_event_signals`, thresholds under `context.same_event`):

- **numbers** — the page's title + first `numbers_text_chars` (600) shares ≥
  `numbers_min_shared` (2) numeric tokens with the story's title + brief
  (single digits and 4-digit years never count: "960 MW" and "$6.2 billion"
  do, "3" and "2026" don't);
- **date_band** — a *contemporaneous* page (published within ±
  `window_hours` (72) of the story's `first_at`, or undated while sharing ≥
  `undated_min_entities` (3) story entities) must keep title similarity AND
  text similarity under `max_*_similarity` (0.30) — same-event coverage inside
  the event window reads a lot like the story even below the ceiling. Dates
  come from the hit date or `og:article:published_time` / JSON-LD
  `datePublished` / `<time>`;
- **entities_verb** — the page's title shares ≥ `entities_min` (3) of the
  story's named entities AND an `event_terms` word that also appears in a
  story headline (breach, launch, raises, unveils, acquires… — inflections
  meet via a crude stem).

Each signal alone rejects `same_event`; which one fired is stored as
`same_event_signal` (`ceiling` for the similarity ceiling, else the signal
name or `judge`). Every key:
[same-event rules](../../configuration/reference.md#same_event-rules).

## The LLM judge

With `llm_judge` on (default when a summarizer role is configured) each
surviving page is judged on the story's headline + brief, the story and page
dates, and the page's title + first 1500 chars:

```json
{"adds_context": 0|1|2, "same_event": bool, "is_opinion": bool,
 "is_substantive_article": 0|1, "note": "<=25 words"}
```

- `same_event` true (the page mainly reports the same specific event,
  including follow-up details of it: cause, motive, reaction, quotes) is
  recorded separately as `judge_same_event` and forces `same_event` only when
  `adds_context` ≥ 1 (a page the judge read as at least related); a score-0
  flag means the page was simply irrelevant → `off_topic`, and score 0 without
  the flag → `judge_rejected`.
- `adds_context` 2 = adds background, history, data, comparison, explanation
  or a different but related event — only 2 is kept.
- `is_opinion` marks opinion/column/advocacy pages.
- `is_substantive_article` 0 = mostly a list of links, a landing/section page
  or a product page (`not_article`).
- Only a substantive article scored 2 is kept, with `note` stored as
  `context_note`. Failures and unparsable answers reject the candidate
  (`judge_failed`) — notes are never fabricated. With the judge off, accepted
  pages carry no note.

## Opinion labels

Opinion/column pages — detected from the URL path (/opinion/, /columnists/,
/mnenia/, /blog/…), newsletter hosts (substack.com & co.), `og:type`/
`article:section` metadata or the judge's `is_opinion` — are stored with
`context_kind = "opinion"`, badged "opinion" in the section, and capped at
`context.max_opinion` (default 1) per story (`opinion_cap` beyond).
