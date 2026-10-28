---
title: "Cited summaries, a publish gate, and an API in four days"
description: "Chapter 2: OpenAI-compatible summarization, briefs and reports, the publish gate, /api/v1 + SSE, and story Q&A — PRs #8–14."
pubDate: 2026-10-26T12:00:00Z
chapter: 2
prs: [8, 9, 10, 11, 12, 13, 14]
tags: ["llm", "summaries", "api"]
---

## What shipped

This chapter turned the clustered feed into a readable product.

[#8](https://github.com/babcoccl/discover-feed/pull/8) added cited story
summaries through a configurable OpenAI-compatible LLM endpoint — the
abstraction that lets the app run against a local llama.cpp server (Qwen3.5 9B,
about 42 tokens/s on the author's hardware) or any hosted equivalent. It
shipped with a fake LLM for testing, which mattered: every later feature that
touches the model could be exercised end-to-end in Docker without real
inference.

[#10](https://github.com/babcoccl/discover-feed/pull/10) split summaries into
two products: a **brief** — a lead plus three cited bullets — and a longer
**report**. [#11](https://github.com/babcoccl/discover-feed/pull/11) followed
almost immediately with fixes: reports that could never be reached and report
timeouts that stalled briefs.

Then came the two biggest PRs of the chapter.
[#12](https://github.com/babcoccl/discover-feed/pull/12) (+5,534) introduced
the **publish gate**: an explicit story lifecycle
(`ingested → text_pending → text_final → clustered → brief_ready → report_final
→ published`) with a background worker, so feed cards only appear when there's
something finished to show. Before this, feed cards surfaced mid-generation and
opening a story page kicked off report generation synchronously.
[#13](https://github.com/babcoccl/discover-feed/pull/13) (+4,543) added report
quality guards and metrics plus `/api/v1` with a resumable SSE stream.
[#14](https://github.com/babcoccl/discover-feed/pull/14) (+4,221) finished the
arc with story Q&A — streamed, cited answers grounded in the story — plus
attribution and publisher details.

[#13](https://github.com/babcoccl/discover-feed/pull/13) is also where the
"investigate before changing" habit shows up in the diff: before touching
report generation, the session inventoried the existing routes, reproduced the
quality failures the 9B model was producing (repeated `verbatim_overlap`
rejections, identical retries, glued words, ~330-word reports) and then fixed
the causes — quality guards plus metrics — alongside the read API. And #14's
Q&A shipped with attribution rules: when a story has a single first-party
source (SEC, Fed) or the headline names the publisher's domain, briefs and
reports switch to attributed prompts ("Meta says…") rather than stating
promotional claims as fact.

## What went wrong

Report timeouts stalling briefs (#11) was the first real operational bug:
two features sharing one LLM worker meant a slow report could starve the quick
briefs that actually populate the feed. The fix shaped how later workers were
designed — every subsequent background job (enrichment, context, telemetry)
got its own scheduling and its own failure domain rather than piggybacking on
the summarizer.

#11 hid a second, more embarrassing bug behind the timeout story: detailed
reports could never appear at all for single-source stories, because those
cards linked straight to `Article.url` — the story page where reports get
requested was literally unreachable from the feed. No timeout involved; the
feature existed and worked, it just had no path to it. #11 relinked clustered
cards through `/story/{id}` and added a `retrying` report state with a Retry
button, turning a silent dead end into a visible, recoverable state.

## What we changed

The publish gate is the architectural commitment of the whole project. Before
#12, "what the user sees" was whatever the pipeline had produced so far; after
it, reads only ever touch `story_views`, and writes go through the lifecycle.
That single decision — publish only fully-formed states — is what made later
features safe to add aggressively: enrichment can mark stories pending, context
links can attach extra articles, and none of it leaks to the public API until
it clears the gate.

## Looking back

Chapters 2's PRs are all still load-bearing: the brief/report split, the
lifecycle states, and `/api/v1` survived every later refactor. The one pattern
that didn't last was implied rather than written: "one worker, one queue" —
by chapter 6 the app has a summarizer worker, an enrichment worker, an image
worker, a telemetry writer, and a watchdog, and the contention between them
became its own story.
