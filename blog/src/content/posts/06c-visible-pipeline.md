---
title: "Chapter 6c: making the pipeline visible"
description: "Chapter 6, part 3: a live pipeline visualizer, a per-story step log merging every event source, and enrichment provenance — PRs #41, #42, #44."
pubDate: 2026-10-26T18:00:00Z
chapter: 6
prs: [41, 42, 44]
tags: ["telemetry", "ui", "enrichment"]
---

## What shipped

The last three PRs are the observability story's user-facing half: turning
the tables from #35 and #40 into something you can actually read.

[#41](https://github.com/babcoccl/discover-feed/pull/41) added a live pipeline
visualizer strip on the Workflow page plus a recent-activity lane: all 12
pipeline stages as state-coloured nodes (running, stalled, failed, ok, idle),
with the last 30 stage events sorted running-first — the system-wide view of
what's running right now.

[#42](https://github.com/babcoccl/discover-feed/pull/42) — +1,566 lines — is
the payoff of the whole arc: a per-story **step log** in the story inspector
that merges stage events, `llm_calls`, a new `db_events` table (lock errors,
retries, give-ups, slow writes — each with the victim and the holder),
generation attempts, summary saves, lifecycle timestamps and queued jobs into
one chronological list. Each row shows time, offset, gap, duration, tokens,
status, reason, and an "alongside" count of overlapping work, with a summary
strip: wall time, queue→start, LLM active vs. waiting, lock wait, longest
holder, final outcome. The `db_events` emission is non-blocking,
recursion-guarded on the writer thread and rate-limited to 20/s — the
telemetry is still designed so it can never be the thing that breaks the app.

[#44](https://github.com/babcoccl/discover-feed/pull/44) closed the loop on
enrichment: the story page now marks which members came from web-search
enrichment, so "where did this coverage come from" is a property of the page
rather than something you reconstruct from the database. Each cited point in
the brief and report is labelled `web`, `mixed` or `feed` depending on the
origin of its citations — which, two chapters after enrichment started adding
foreign sources to stories, is the first time a reader can tell.

## Why it matters

Every earlier debugging session in this log was, at some level, a visibility
problem. The enrichment stall (6a) was invisible until a dashboard showed 0
requests. The lock failures (6b) were invisible until engine events named the
holder. The step log is what those hunts look like when they're *designed
in*: for any story, you can see queue time versus LLM time versus lock time,
and whether its delays were its own or someone else's write transaction.

There's a reasonable argument the order was wrong — that the instrumentation
should have come before eight enrichment PRs and four context PRs. The honest
answer is that the failure modes couldn't be known in advance, and the tools
got built exactly when the failures demanded them. But the shape of chapter 6
— instrument (#35, #36), fix the stall (#39), instrument the locks (#40),
fix the caller bug (#43), then surface it all (#41, #42, #44) — is a decent
template for growing observability on any long-running pipeline: storage
first, then diagnosis, then UI, in that order and not before there's a real
failure to explain.

Two of these PRs also show the cost discipline the project settled into. The
visualizer in #41 adds no tables, jobs or WebSockets — it reuses the existing
`flow_overview` query and one capped `stage_events` select, rendered by the
HTMX poll that was already there. The provenance marks in #44 are computed at
render time from data already in stored view docs — no schema version bump, no
migration, and pages with no enrichment render byte-for-byte identical. After
six chapters of growing the pipeline, the last thing to grow was the ability
to read it — and it cost almost nothing, because everything it needed had been
recorded all along.
