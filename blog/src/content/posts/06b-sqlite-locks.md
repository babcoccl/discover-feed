---
title: "Chapter 6b: hunting 'database is locked'"
description: "Chapter 6, part 2: the SQLite lock hunt — wrong first hypothesis, instrumentation, transaction splits, retry_locked — PRs #40, #43."
pubDate: 2026-10-26T17:00:00Z
chapter: 6
prs: [40, 43]
tags: ["sqlite", "debugging", "database"]
---

## What went wrong

For a while the deployed app logged roughly **19 "database is locked"
failures per hour across 9 tables** — with `busy_timeout` already at 15000 ms,
so connections were waiting the full timeout and *still* failing. The database
held about 3,129 stories at the time. Busy timeout wasn't the problem;
something was holding write transactions open far longer than 15 seconds.

SQLite serializes writers: one write transaction at a time, and readers in
WAL can coexist but a second writer waits. With a summarizer worker, an image
worker, an enrichment worker, a telemetry writer and request threads all
sharing one file, any transaction held long enough turns the whole app into a
queue — and a 15-second `busy_timeout` just means requests fail *slowly*.
The first hypothesis was the obvious one: the biggest, busiest writers —
clustering and the summarizer — must be the offenders. It was wrong, or at
least wrong about *where* the time went.

## What shipped

[#40](https://github.com/babcoccl/discover-feed/pull/40) did this the way the
spec asked: instrument first, fix second. Engine events now mark each
connection's write transaction on its first INSERT/UPDATE/DELETE (thread,
caller, start time) and clear it on commit; a daemon warns past 2 s; and on a
lock error it logs the table plus every open write transaction. Then two fixes
fell out of the data:

- `Summarizer._save` held one write transaction across `record_attempts`, the
  `StorySummary` insert, *and* the `sync_story` view rebuild — split into two
  short transactions.
- `ImageWorker._process`/`asset` held a write transaction **across
  `fetch_image` and `render_webp`** — a network fetch and an image render
  inside a database write. The claim `INSERT OR IGNORE` now commits and the
  session closes before the slow work; a second short transaction writes the
  result.

Plus `retry_locked(fn)` — jittered 0.5/1/2 s backoff, max 3 tries — so
transient contention retries instead of failing.

The result, per the author's notes: **0 lock failures over 6+ hours** after
#40. The longest remaining write transactions were the real writers —
clustering and the telemetry writer at about 2–2.5 s — which is contention the
system can tolerate.

Then the instrumentation showed its own bug.
[#43](https://github.com/babcoccl/discover-feed/pull/43) fixed every
`write txn held Ns by <thread> at ?` warning showing `?`: `_caller()` used
`traceback.extract_stack(limit=10)`, but SQLAlchemy's event dispatch alone is
deeper than 10 frames, so every captured frame was `app/db.py` or
`sqlalchemy/` — all skipped by design, leaving the `?` fallback. The fix walks
the untruncated stack to the first real `.py` frame, producing
`app/telemetry/writer.py:88:_flush_once`-style callers. The instrumentation
had been working — it just couldn't say *who* was holding the lock.

## What we changed

Two durable rules came out of this hunt. First: **never hold a write
transaction across I/O or slow compute** — claim, commit, do the slow work,
then write the result in a second transaction. Second: **verify the verifier
before trusting it** — the "?" caller bug meant the instrumentation's most
useful field was silently useless until #43, and the tell was in its own
output all along.

There's also a workflow lesson worth stating: this was the second time a spec
was written as "find the evidence, then fix" rather than "fix the thing we
suspect." The result was a fix that addresses the actual mechanism — long
write transactions — instead of the visible symptom, plus a permanent watchdog
(the >2 s hold warning, the holder log on every lock error) that makes the
next contention issue diagnosable in minutes. When the fix is cheap and the
diagnosis is expensive, spend the PR on the diagnosis; the fix usually lands
in the same diff anyway.
