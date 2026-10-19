# Queries

Queries come from the **headlines only** (never article text), built per
provider, after removing boilerplate labels (`strip_title_prefixes`:
`Show HN:`, `Exclusive:`, `Analysis -`, `Live:` ...).

## Skips

Stories other outlets are unlikely to cover are not searched — they are marked
done at once (briefs never wait) and counted separately in Settings > Pipeline
and `evaluate`:

| Reason | Rule |
| --- | --- |
| `skipped:pattern` | Headline matches `skip_title_patterns` (default `^Show HN`, `^Ask HN`, `^Tell HN`, `^Opinion`, `^Letters`) |
| `skipped:no_signal` | Headline has fewer than `min_signal_tokens` (default 1) distinct names/numbers — "The cost of getting a job today" skips; one entity, acronym or number is enough, so "AI was great until it was not" is searched |
| `skipped:url_pattern` | Anchor URL path matches `skip_url_patterns` (opinion columns, live blogs, features) |

## How terms are ranked

The heuristic ranks terms: quoted multi-word names (`"Federal Reserve"`),
other names, names with a version or number (`"Ubuntu 26.10"`), numbers and
percentages (`0.1%`), then distinctive words, boosted when other member
headlines repeat them; lone digits are dropped.

Before ranking, apostrophes are cleaned (`O'Brien` → `OBrien`, `Apple's` →
`Apple`), whole contractions (`can't`, `we're`, `it's`) and filler words that
carry no signal (`make`, `stuff`, `fascinated`, `says` ...) are dropped,
keeping names, numbers and distinctive nouns.

## Per-provider limits

- TinyFish gets up to 5 terms, GDELT and the Guardian (which AND every term)
  3-4; NewsData's `q` stays within 100 characters; SearXNG/Brave get up to 6
  terms.
- GDELT never gets a keyword under 3 characters (it splits on punctuation, so
  `U.S.` and `0.1%` count): `AI`, `US`, `UK`, `EU`, `UN` are spelled out
  (`"artificial intelligence"`), other short words dropped; a quoted name keeps
  a short word (`"Gen Z"`). If GDELT still answers "keyword that was too
  short", the query is rebuilt once without any short part and sent again (no
  cooldown); if that is rejected too the search is skipped (`query_too_short`).
- Only when the strict query leaves too few usable candidates after the cheap
  rejects (same domain, social, already known, time, language) is a looser one
  sent (`"Federal Reserve" (Powell OR 2025 OR rates)`), at most
  `max_queries_per_story` (default 2) requests per provider and story.
- `query_mode: llm` asks the summarizer for up to 2 queries, trimmed to the
  same per-provider limits, falling back to the heuristic.
