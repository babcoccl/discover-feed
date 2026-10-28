---
title: "How I built an open source News Discovery and Summarization feed, with Devin.ai, for $200"
description: "A personal news aggregator written almost entirely by a coding agent — 44 pull requests in 18 days. This site is the engineering log."
pubDate: 2026-10-26T10:00:00Z
prs: []
tags: ["meta", "devin"]
---

This site is the engineering log of
[discover-feed](https://github.com/babcoccl/discover-feed), a personal
"Discover-style" news reader built almost entirely through sessions with
[Devin](https://devin.ai), a coding agent. Between October 7 and October 25,
2026, 44 pull requests — each opened by a Devin session from a written spec —
took the repo from an empty scaffold to a running system: RSS/Atom and API
ingestion, article clustering, LLM-written briefs and reports, web-search
enrichment, context links, outlet-perspective synthesis, alerts, a public API
with SSE, and a telemetry pipeline built to debug its own failures.

## The app

The stack is boring on purpose: Python 3.12, FastAPI, SQLAlchemy over SQLite,
APScheduler, and a server-rendered Jinja2 + HTMX + Tailwind UI. Summaries come
from a local model — Qwen3.5 9B on llama.cpp, at roughly 42 tokens per second —
behind an OpenAI-compatible endpoint, so any compatible endpoint can be swapped
in. A story is a cluster of related articles; each story gets a brief (a lead
plus cited bullet points) and, when it earns one, a longer report. Everything
runs in Docker on the author's own hardware; the deployed UI is reachable only
on the author's LAN.

The full feature list and the operating docs live on the repo's
[documentation site](https://babcoccl.github.io/discover-feed/) — itself a
Devin PR ([#31](https://github.com/babcoccl/discover-feed/pull/31)).

## How the work actually happened

Each session got a spec in the same shape: a context paragraph describing the
repo, numbered tasks, acceptance criteria, non-goals, and a closing line —
"open a PR and stop." The agent ran the app locally, wrote tests, opened a PR,
and the author reviewed, commented, and merged. Repeat 44 times.

That cadence is what this log is about. The chapters follow the pull requests:

- **Chapter 1** — scaffolding, feed ingestion, deduplication, the first UI,
  text extraction and clustering.
- **Chapter 2** — cited summaries, briefs and reports, the publish gate, the
  public API and SSE, story Q&A.
- **Chapter 3** — exactly-once alerts, source management with budgets,
  SSRF-safe card images.
- **Chapter 4** — web-search enrichment: adding other publishers' coverage,
  hardening it, and tuning recall against content farms.
- **Chapter 5** — background-and-context links, deep coverage for big stories,
  and per-source "how outlets differ" perspectives.
- **Chapter 6** — observability: telemetry, the enrichment stall, the SQLite
  lock hunt, and making the pipeline visible.

## Ground rules for this log

Every factual claim in these posts comes from one of three places: a merged
pull request (always cited by number and linked), a file in the repo, or the
author's notes — reproduced verbatim where they're used. No invented metrics,
no invented quotes, no session transcripts, no dates that aren't in the PR
data. Where a spec's description of the world turned out to be stale, the post
says what actually happened instead.

The tone is deliberately a log, not a launch post. Several chapters exist
*because* something failed — enrichment silently stopped for days, SQLite
locks hit ~19 errors an hour, the instrumentation itself had a bug that made
its most useful field unreadable. Those stories get told with the same
specificity as the features, because they're the parts that generalize.

## How to read it

The [timeline](/timeline) is generated from committed PR metadata and covers
all 44 PRs — including [#28](https://github.com/babcoccl/discover-feed/pull/28),
the one that never merged. Posts are grouped into six chapters in merge order;
each ends with a table of the PRs it covers (title, merge date, diff size), so
the claims are one click from their evidence. Tags cut across chapters
(`enrichment`, `telemetry`, `debugging`, `docs`, …) if you'd rather follow a
theme than a chronology.

This site is also itself a Devin session's output — an Astro build generated
the same way everything else in the repo was, from a spec in the same format
it describes, deployed to Cloudflare Pages from the `blog/` directory.
