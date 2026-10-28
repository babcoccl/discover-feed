---
title: "Alerts that fire once, sources with budgets, SSRF-safe images"
description: "Chapter 3: keyword alerts via webhook/ntfy/email, source management and API budgets, and a validated WebP image cache — PRs #15–17."
pubDate: 2026-10-26T13:00:00Z
chapter: 3
prs: [15, 16, 17]
tags: ["alerts", "sources", "images", "security"]
---

## What shipped

Three PRs, three distinct subsystems — the biggest growth spurt in the log by
lines (+5,928, +6,814, +2,518 respectively across #15–17's diffs as a whole).

[#15](https://github.com/babcoccl/discover-feed/pull/15) added keyword alerts.
A rule matches on article title and feed summary the moment an article is
ingested — before extraction, clustering, briefs, or the publish gate — and
notifies its channels (webhook, ntfy, or email) **exactly once per story
lineage**. The exactly-once guarantee is the interesting part: before firing,
the engine resolves story redirects and checks every member of the story for
an existing firing, so an article that clusters late into an already-alerted
story doesn't re-fire. An `alert_max_age_hours` window (default 6) means a
first start, a new rule, or a backfill can't flood the channel.

[#16](https://github.com/babcoccl/discover-feed/pull/16) — the single largest
PR in the project at +5,927/−688 — did two things at once. Part A was ops:
docker-compose `env_file`, graceful shutdown so an open SSE stream doesn't
hang `docker down`, an `.env.example` documenting every variable, and a
startup warning pass that lists missing `${ENV}` secret references by name
(never values). Part B was source management: sources became editable in the
app (YAML seeds the DB once, the DB is truth), with Guardian, NewsData and X
adapters behind generic per-source daily/monthly budgets tracked in
`source_usage`.

[#17](https://github.com/babcoccl/discover-feed/pull/17) added card images
without adding fetches: the extractor parses the HTML it already downloaded
for `og:image`, `twitter:image`, JSON-LD and favicons, storing
`page_image_url` per article. Validation is strict — `image/*` content type, a
3 MB cap checked on the header and the stream, a Pillow decode with a pixel
limit against decompression bombs, and rejections for SVG, ICO, tracking
pixels and anything under 320×180 — each verdict cached per-URL in
`image_assets` so a bad URL isn't retried forever. Fetches go through an SSRF
guard that re-checks redirects, so an image URL can't pivot the server into
internal addresses.

## What went wrong

Two security-shaped problems got designed away rather than patched later. The
alert channels and API keys live in env vars, referenced as `${VAR}` in config
— so the failure mode (a source configured but its key absent) needed the
startup warning from #16 to be visible at all. And images are fetched from
arbitrary article pages, which is a classic SSRF surface; the guard in #17 was
built in from the start rather than added after an incident, and the same
guard was reused when enrichment started fetching arbitrary URLs in chapter 4.

The other wrong turn: doing ops and features in the same PR. #16 bundled
compose/env fixes with a whole source-management subsystem. It merged fine,
but it's the kind of PR that's hard to review and harder to bisect — later
sessions' specs got narrower partly because of it.

## What we changed

Budgets were the durable idea. Every external call — feed fetch, API adapter,
later every search provider — counts against a per-source daily or monthly
spend limit in `source_usage`. When enrichment arrived in chapter 4, it didn't
need a new accounting system; it just wrote to `"search:<provider>"` rows in
the same table. That's the pattern that kept recurring: each new feature
introduces one mechanism, and the next two features get it for free.

It's worth noting what this chapter *didn't* build: no notification UI beyond
a firing log, no dashboards, no settings wizard. Each PR added exactly the
mechanism its spec named and stopped — which is why three subsystems landed in
two days without colliding. The discipline of "one mechanism per spec, reused
by later specs" is load-bearing: it keeps each session's diff reviewable and
makes the codebase legible to the next cold-start session that has to modify
it.
