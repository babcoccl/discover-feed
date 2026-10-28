---
title: "From empty repo to clustered stories in a day"
description: "Chapter 1: scaffolding, RSS/Atom ingestion with dedup, a Discover-style UI, text extraction, and clustering — PRs #1–7."
pubDate: 2026-10-26T11:00:00Z
chapter: 1
prs: [1, 2, 3, 4, 5, 6, 7]
tags: ["ingestion", "clustering", "ui"]
---

## What shipped

The first PR ([#1](https://github.com/babcoccl/discover-feed/pull/1)) laid the
foundation: a FastAPI app, Pydantic models for profile configuration, Docker
packaging, and CI. A day later [#2](https://github.com/babcoccl/discover-feed/pull/2)
added the actual pipeline — RSS/Atom ingestion that normalizes articles and
deduplicates them, with follow-up cleanups in
[#3](https://github.com/babcoccl/discover-feed/pull/3) (dropping a config
alias, adding a `time_field`).

[#4](https://github.com/babcoccl/discover-feed/pull/4) was the first big swing
(+2,212 lines): profiles and keyword topics moved into the database, and the
feed got its Discover-style card UI. The same day,
[#7](https://github.com/babcoccl/discover-feed/pull/7) — the largest PR of the
chapter at +3,516 — added full-text extraction and the clustering step that
groups related articles from different sources into a single story. Clustering
is the feature that makes it a "Discover-style" reader rather than an RSS list:
one event, many outlets, one card.

Two small PRs show the shape the whole project would take:
[#5](https://github.com/babcoccl/discover-feed/pull/5) made the demo runnable
on Windows without `make` (the author's machine is Windows + Docker Desktop),
and [#6](https://github.com/babcoccl/discover-feed/pull/6) fixed card links to
point at the feed's raw permalink — a "the thing worked but the detail was
wrong" fix that recurs throughout the log.

Under the hood, #7's clustering is deliberately simple and tunable: TF-IDF over
title×2 + summary + the first 1,000 words of extracted text, joining a story
when cosine ≥ 0.45 to the story centroid, within 72 hours, sharing at least 2
significant tokens with the story title — with a stricter 0.7 bar if the story
already has an article from the same source. Extraction respects robots.txt,
caps pages at 2 MB, and rate-limits to one request every 2 seconds per domain.
It's incremental and idempotent, which mattered later: enrichment and context
both attach articles to existing stories and lean on the same guarantees.

## What went wrong

Nothing catastrophic yet — this is the part of the project where every PR is
additive and nothing has had time to rot. The closest thing to a lesson is in
#3 and #6: the scaffold and ingestion shipped fast, but small semantic
mismatches (a stale config alias, links pointing at the canonical URL instead
of the feed's own permalink) needed follow-up PRs rather than being caught on
first pass.

## What we changed

Nothing structural — chapter 1 is a straight line. The pattern it established
mattered more than the code: specs describe behavior, the agent implements and
verifies against a running stack, and review happens on the PR. Each of these
seven PRs was opened by a separate session with a fresh context, so the specs
also had to teach the repo to each new session from scratch — which is why the
"Context:" paragraph at the top of every spec got longer as the codebase grew.

## Looking back

Eighteen days later, the structure from chapter 1 is still recognizable:
`app/` for pipeline and services, `config/` for profiles, `tests/` for the
suite, Docker for deployment. The ingestion normalization and dedup decisions
made in #2 survived unchanged through enrichment, context links and alerts —
each later feature produces `Article` rows in the same shape and lets the same
pipeline carry them.

One choice paid off disproportionately: keeping the UI server-rendered
(Jinja2 + HTMX, Tailwind via CDN, no Node build step) meant every later
session could touch the UI without a frontend toolchain, and "does it work"
always meant "does the Docker stack respond," not "does the build pass." When
chapter 6 needed a pipeline visualizer and a per-story step log, they were
just more templates over the same queries — no new framework, no client state
to keep honest. For a one-operator project, that simplicity is the feature.
