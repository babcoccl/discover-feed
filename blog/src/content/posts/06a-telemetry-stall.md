---
title: "Chapter 6a: instrument first, find the stall"
description: "Chapter 6, part 1: workflow telemetry, and the enrichment stall where 45 stories sat pending with a dead worker — PRs #35–39."
pubDate: 2026-10-26T16:00:00Z
chapter: 6
prs: [35, 36, 37, 38, 39]
tags: ["telemetry", "enrichment", "debugging"]
---

## What shipped

By mid-October the pipeline had five stages, three workers, and no way to see
what any of them were doing. [#35](https://github.com/babcoccl/discover-feed/pull/35)
added workflow observability: `llm_calls` and `stage_events` tables fed by a
dedicated writer thread, a Settings → Workflow page, and a per-story
inspector. Every LLM call now requires a `purpose=` label — an unlabelled call
site raises — so latency and token usage attribute to the feature that caused
them. [#36](https://github.com/babcoccl/discover-feed/pull/36) added per-reason
telemetry and honest image-sweep status: live `stage_events` had shown 58
image "error" rows whose sweeps were actually healthy — every candidate URL
already had a cached verdict, so zero fetches were attempted and the catch-all
error path wiped the counts. Zero-fetch sweeps now report `ok` with a reason.
[#37](https://github.com/babcoccl/discover-feed/pull/37)
and [#38](https://github.com/babcoccl/discover-feed/pull/38) were small fixes
(image dedup treated as cache hit, a clipped feed pill).

## What went wrong

The telemetry writer almost caused its own outage. The first version shared
the app's connection pool — per-second diagnostic commits serialized against
real writes on the same SQLite file, exhausted the QueuePool, and the app went
unhealthy while enrichment produced zero results. The fix, in the same PR: the
writer gets its own engine (`pool_size=1`), `journal_mode=WAL`, and
`synchronous=OFF` — losing the last seconds of diagnostic data on a crash is
fine; losing the app is not.

The second failure was older and quieter. Enrichment had silently stopped: 45
stories sat `pending` with the same `enrich_started_at`, while TinyFish's own
dashboard showed 0 requests since start. Nothing logged an error, because the
worker wasn't failing — it was never starting.

[#39](https://github.com/babcoccl/discover-feed/pull/39) explains it: the
scheduler admits `capacity()` stories, where capacity is provider throughput
minus stories already pending. The fill-every-sweep design means that if the
process restarts while stories are pending, capacity computes to 0 — so `ids`
was empty, and `start()` (which ran `if ids or retries_due`) never ran.
Pending stories didn't count toward restarting their own worker. Briefs kept
shipping once their hold expired, so the rest of the pipeline looked healthy.

## What we changed

The fix was a two-line condition — start if `waiting or due`, not `if ids or
due` — plus the missing infrastructure around it: enrichment got its own
scheduler job sweeping every 60 seconds, a watchdog every 120 seconds that
restarts the thread only when it's dead *and* work is queued, and error
recording on the worker loop. The thread exiting on an empty queue is normal;
the bug was that nobody could tell.

The lesson the chapter writes in bold: **the failure wasn't in the code path
that ran — it was in the code path that decided whether to run at all.** And
the only reason it became diagnosable is that #35–36 had just built the
instrumentation to see it. Instrument first; the bugs are already there.

The other, subtler lesson is about the instrumentation itself. The writer
thread's contract — callers only ever `queue.put_nowait`, flush errors are
logged and dropped, the queue is unbounded so telemetry can never block or
fail the pipeline — is what "observability that can't hurt the patient" looks
like in practice. It had to be relearned once already: the shared-pool version
of the writer was the thing that made the app unhealthy under e2e load. When
chapter 6b's `db_events` table arrived in #42, it inherited exactly the same
contract — non-blocking, recursion-guarded, rate-limited to 20 events/second
with dropped events counted visibly. Diagnosing the tool that diagnoses the
app is embarrassing the first time; it's just a design constraint the second.
