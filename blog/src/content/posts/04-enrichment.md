---
title: "Teaching stories to find their own coverage"
description: "Chapter 4: web-search enrichment, TinyFish + GDELT, recall tuning, and the content-farm quality gate — PRs #18–23, #25–26."
pubDate: 2026-10-26T14:00:00Z
chapter: 4
prs: [18, 19, 20, 21, 22, 23, 25, 26]
tags: ["enrichment", "search", "quality"]
---

## What shipped

A story seen by only one configured feed is the common case — the interesting
question is what other publishers said about it.
[#18](https://github.com/babcoccl/discover-feed/pull/18) added web-search
enrichment: after clustering, an eligible story (fewer than `target_sources`
publishers, fresh enough) is marked `enrich_status=pending`, and a background
thread searches the web for matching coverage. Accepted pages become story
members, so they feed the brief and report like any other article. The
pipeline picked up the budget table from chapter 3 for free: search providers
track daily/monthly spend as `search:<name>` in `source_usage`.

[#19](https://github.com/babcoccl/discover-feed/pull/19) hardened it — shared
provider state, retries, per-provider pacing.
[#20](https://github.com/babcoccl/discover-feed/pull/20) made TinyFish the
primary provider with GDELT as the fallback, and
[#21](https://github.com/babcoccl/discover-feed/pull/21) lowered thresholds
and set a 10-second GDELT default.
[#22](https://github.com/babcoccl/discover-feed/pull/22) was a recall pass:
query continuation, a second acceptance rule, and headline-only candidates.

The candidate path stayed the same throughout: pre-filter (same registrable
domain, social/aggregator lists, deny domains, known URLs, time gap, language,
snippet TF-IDF similarity), fetch through the extractor with the image SSRF
guard, then a text-similarity check before acceptance. Paywalled pages are
dropped. Every gate records *why* it rejected a candidate — surfaced in the UI
by #25 — which is the only reason the tuning in #21–23 was measurable at all.

## What went wrong

More coverage is not the same as better coverage. Casting a wider search net
landed content-farm and SEO-rewrite domains alongside real outlets, and an
empty-snippet regression appeared where TinyFish results came back with
nothing to score — the snippet investigation in
[#23](https://github.com/babcoccl/discover-feed/pull/23) traced it to the API
itself returning empty snippets, not the parser (the diff hadn't touched
parsing). That's a good example of what debugging this codebase looks like:
the first suspect is your own diff, but the fix starts by proving where the
data actually went missing.

## What we changed

The response was a two-layer quality gate.
[#23](https://github.com/babcoccl/discover-feed/pull/23) added domain lists
and publisher aliases: social domains and common aggregators get filtered,
and outlets with multiple domains collapse to one identity.
[#25](https://github.com/babcoccl/discover-feed/pull/25) pushed the social
exclusions server-side and surfaced why candidates were rejected.
[#26](https://github.com/babcoccl/discover-feed/pull/26) closed the loop: a
source-quality gate that flags content-farm domains and denies confirmed ones.

The design principle that came out of this chapter: **accept conservatively,
log the reason loudly**. Every rejection path records *why* a candidate was
dropped, which is what made recall tuning in #21–22 measurable instead of
vibes — you can count rejections by reason before and after a threshold
change. That "emit the reason, then tune" habit is the direct ancestor of the
per-reason telemetry and step logs of chapter 6.

## Looking back

Enrichment is the project's most-touched subsystem (eight PRs here, and it
reappears in chapters 5 and 6). The architecture that survived is the one
from #18: mark, search in a worker, filter, fetch, verify text similarity,
accept. Everything else — provider choice, thresholds, quality gates — turned
out to be tuning. Getting the skeleton right mattered more than getting any
threshold right.

The provider abstraction paid off the same way the budget table did: swapping
in TinyFish as primary (#20) didn't touch the pipeline — it changed which
`SearchService` answered, with GDELT kept underneath as fallback. When #32
later added deep coverage and an outlet registry, it plugged into the same
service rather than growing a parallel path. The subsystem is genuinely hard
— it's the part of the app that talks to the open web — but each hard part
landed exactly once, in one place, and the rest is configuration.
