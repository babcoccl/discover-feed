# Filters and acceptance rules

## Rejected before fetching

Candidates are dropped for:

| Reason | Rule |
| --- | --- |
| same domain | Same domain as a member (`bbc.co.uk`, `cnn.com`; `publisher_aliases` first collapses regional editions — `bbc.com` = `bbc.co.uk`, `theguardian.com` = `guardian.co.uk` — and `www.`/`amp.`/`m.`/`uk.` prefixes, so the same publisher is caught before any fetch and counted once in the publishers tally) |
| social / aggregator | Built-in lists cover threads.com / threads.net, x.com, Bluesky and common fediverse hosts (plus any `mastodon.`/`mstdn.` host); aggregators: daily.dev, flipboard.com, newsbreak.com, ground.news, smartnews.com, msn.com — `social_domains` / `aggregator_domains` extend both |
| denied | `deny_domains`, `quality.deny_domains`, and the persisted `blocked_domains` table (see [source quality](source-quality.md)) |
| already known | URL already in the database |
| time | Published more than `max_time_gap_hours` (72) from the story |
| language | Another language (`language`, default `en`) |
| low similarity | Title/snippet TF-IDF below `min_snippet_similarity` (0.25); hits without a snippet (GDELT: titles only) must share an entity and reach `min_title_similarity` (0.30). Both scores are stored in `enrichment_candidates` and the text check below has the final say |

## Fetch

The best `max_candidates` are fetched with the extractor's robots.txt rules,
per-domain delay and size caps, through the image fetcher's SSRF guard
(http(s) only, public addresses only, every redirect re-checked). Failed,
robots-disallowed (reason `robots`, e.g. 9to5linux.com, whose robots.txt allows
named crawlers only) and paywalled (`isAccessibleForFree: false`) pages are
dropped; `diagnose` prints the fetch detail under each one (`fetch: skipped
(disallowed by robots.txt)`, `fetch: failed (HTTP 403)`).

## Acceptance rules

Extracted text is accepted when it reaches `min_text_similarity` (default
0.35), or — **rule B** — when the hit's snippet was strong
(`min_snippet_strong`, default 0.45), the text shares `min_shared_entities`
names with the headlines and stays above `min_text_floor` (default 0.25): a
precise snippet can carry a page that extracts thinly.

Two extra rules (`extra_rules`, on by default) accept real coverage that still
scores low on text:

- **rule C**: the page shares `rule_c_entities` (default 5) names and reaches
  `rule_c_text` (0.25);
- **rule D**: the headline matches at `rule_d_title` (0.55) with
  `rule_d_entities` (3) names and `rule_d_text` (0.22).

In all cases it must not be a copy (5-word shingle overlap of
`duplicate_overlap` with a member or an accepted page: syndicated wire copies
collapse) or a social / aggregator / paywalled page.

`evaluate`'s `via` column records which rule accepted: `text` / `snippet+b` /
`entities+c` / `title+d` / `link` (link-only members).

The similarity thresholds (`min_snippet_similarity` 0.25,
`min_text_similarity` 0.35) are starting points from one live evaluation
(story "Ubuntu 26.10": related GamingOnLinux / Phoronix / Linuxiac pages scored
0.36-0.67 on text, Betanews / OMG! Ubuntu 0.29-0.30 on snippets); tune them
with `evaluate` as more live data comes in (real-world false-match rates are
not measured yet).
