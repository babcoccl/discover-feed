---
title: "Lessons learned building with a coding agent"
description: "What 44 specs taught us: prompt format that worked, instrument-first debugging, and where the agent's instincts needed steering."
pubDate: 2026-10-26T19:00:00Z
prs: []
tags: ["meta", "devin", "process"]
---

Forty-four sessions, forty-four specs, forty-three merged PRs. Some of what
worked is in the prompts; some is in how the codebase evolved to absorb
agent-written code. Both halves mattered.

## The prompt format that worked

Every successful spec had the same shape:

1. **Context paragraph** — what the repo is, the stack, the naming the spec
   will use. Sessions start cold; the context is the entire shared vocabulary.
2. **Numbered tasks** — concrete, in dependency order, naming files and
   symbols where they exist.
3. **Acceptance criteria** — checkable statements ("`npm run scan` fails on
   private IPs in dist/"), not intentions.
4. **Non-goals** — explicit "do not touch X" lines. Without them, a
   well-meaning session refactors adjacent code.
5. **"Open a PR and stop"** — a defined finish. Ambiguous endings produce
   either extra scope or extra questions.

The specs that produced the cleanest PRs were the ones that constrained the
*problem* hard and left the *implementation* to the agent. The ones that went
sideways under-specified the boundary (chapter 5's three same-event PRs are
partly that story) or bundled unrelated work into one session
([#16](https://github.com/babcoccl/discover-feed/pull/16) is the example).

## Specs go stale — say so early

Specs were often written before the last PR merged, so "add X" sometimes
landed where X already existed. The reliable fix was the same each time: the
session checks merged code first and states up front what's already done or
different. [#28](https://github.com/babcoccl/discover-feed/pull/28) is the
cautionary case — a targeted fix for context-budget starvation that the later,
larger #29/#30 reworks made moot before it merged. Written a day earlier,
reviewed a day too late.

## Instrument first, then fix

Chapter 6 is the whole lesson in miniature. The lock hunt succeeded because
[#40](https://github.com/babcoccl/discover-feed/pull/40) was specified as
"instrument, then split the transactions the evidence names" — not "guess the
biggest writer and optimize it." The first hypothesis (clustering is the
hog) was wrong: the worst offenders were the summarizer holding a transaction
across a view rebuild and the image worker holding one across a *network
fetch*. Neither is guessable from a lock error; both are obvious once you can
see open write transactions and their callers.

Corollary, from [#43](https://github.com/babcoccl/discover-feed/pull/43):
verify the instrumentation itself. Every lock warning said `at ?` for days —
the fix was one stack-walk away, but nobody looked at the `?` closely enough
to notice it meant the stack was truncated.

## Where the agent needed steering

- **Security defaults**: SSRF guards, publish gates and secret redaction all
  had to be named in specs. Left alone, a session builds the feature, not the
  boundary.
- **Failure semantics**: "exactly once" alerts (#15) and "pending stories
  restart their worker" (#39) are spec-level requirements. Agents implement
  the happy path unless the failure path is acceptance criteria.
- **Verification**: a local LLM is too slow to iterate against; the fake-LLM
  harness from [#8](https://github.com/babcoccl/discover-feed/pull/8) is what
  made end-to-end checking possible at all. Give the agent a way to *prove*
  the change, or it will assert it.

## The short version

Write the spec like a bug report for a feature that doesn't exist yet:
context, steps, expected, non-goals. Assume it may be stale. Make the agent
prove it ran. And when something breaks in production, the first PR adds
visibility — the second one fixes what's actually there.

The meta-lesson is that none of this is exotic. Every failure in this log was
a normal software failure — a capacity edge case, a transaction held across
I/O, a truncated stack — and every fix was a normal software fix. What the
agent changed is throughput: the distance between "noticed something odd" and
"a tested PR that explains itself" is now one spec long. That makes the
writing the bottleneck, which is exactly why the writing is the part worth
getting systematic about.
