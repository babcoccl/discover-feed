---
title: "Context, deep coverage, and how outlets differ"
description: "Chapter 5: background-and-context links, a deep-coverage tier, per-source perspectives, and planner mode — PRs #24, #27–34."
pubDate: 2026-10-26T15:00:00Z
chapter: 5
prs: [24, 27, 28, 29, 30, 31, 32, 33, 34]
tags: ["context", "enrichment", "perspectives", "docs"]
---

## What shipped

Four distinct features landed in four days.

[#24](https://github.com/babcoccl/discover-feed/pull/24) added
**background-and-context links**: for each eligible published story, a context
finder searches for 1–3 pages that add background — history, data, opposing
views — explicitly *not* coverage of the same event. They're stored as
articles with `origin="context"`, which kept them clean by construction:
`NON_FEED` exclusions mean context pages never count as event sources, never
feed briefs, reports, citations or Q&A, and never fire alerts.

[#32](https://github.com/babcoccl/discover-feed/pull/32) added a **deep
coverage** tier: stories with enough feed publishers or priority fan out to
many distinct outlets inside a `daily_request_budget`, while niche stories
keep the cheap single pass — plus an outlet registry (`config/outlets.yaml`,
~170 outlets) with no bias ratings anywhere.

[#33](https://github.com/babcoccl/discover-feed/pull/33) added
**perspectives**: per-article digests (claims with verbatim quotes, stance,
framing) synthesized into a "how outlets differ" section — agreement,
disagreements with cited positions, different angles — strictly grounded in
the sources' own text. No truth ratings, no political labels.

[#34](https://github.com/babcoccl/discover-feed/pull/34) turned context into a
planner: one LLM call picks research tasks from a fixed catalog (history,
quotes, financials, related developments), deterministic recipes produce dated
search windows, a verifier demands a verbatim evidence quote, and a composer
writes ≤30-word notes — including "Then and now" comparisons when earlier and
current quotes both verify.

And [#31](https://github.com/babcoccl/discover-feed/pull/31) split the README
into a MkDocs site on GitHub Pages — the docs site this log links to.

## What went wrong

The recurring failure this chapter was the **same-event boundary**: a
"context" page that is really just more coverage of the same story is wrong in
a way a similarity score can't always catch. #27 fixed a coverage gap and
tightened the boundary; #29 added a same-event date rule, the content-farm
gate, and opinion labels; #30 replaced fuzzy matching with deterministic
same-event signals and accurate reason attribution. Three PRs of boundary
chasing — the hard part was never finding pages, it was rejecting the ones
that are *almost* the same event.

Then there's [#28](https://github.com/babcoccl/discover-feed/pull/28), the
odd one out: a fix for `skipped_context_budget` starvation, diagnosed by
running `context evaluate` on 30 stories and getting 0 candidates — every one
skipped on budget. The cause was mundane: the default `daily_request_budget`
of 30/day had been consumed by an earlier run (~4 searches per story), so all
later searches skipped. #28 raised the cap to 100 — but it's the only PR of
the 44 that never merged, overtaken by the larger #29/#30 reworks, and it sits
in the timeline as a reminder that not every session's branch lands.

The #29 evaluation results are worth quoting because they show the failure
shape precisely: of 24 accepted context links, ~5–6 were same-event coverage
(the OpenCode breach, a Mercury 3 launch, a touchscreen MacBook, a Roadster
unveil) scored `adds_context: 2` — while clearly off-topic pages (a Rufus
download page, CCPA consulting, NVDA filings) got stamped with the same
`same_event` reason, hiding how the judge actually decided. #30's answer was
to stop trusting the judge's flag entirely: `same_event_signals()` computes
deterministic title/entity/date checks *before* the LLM judge runs, and the
rejection reason reports which signal fired.

## What we changed

Two durable patterns. First, `origin` as a data-model firewall: enrichment and
context both produce `Article` rows, but origin tags keep them out of the
event record, so a whole class of "auxiliary content leaks into the summary"
bugs became impossible by schema rather than by diligence. Second, verified
quotes: perspectives digests and planner context items both require quotes
that appear **verbatim** in the source text, with numbers checked on the page.
That's the anti-hallucination control that lets a 9B local model produce
output you can actually publish — the model proposes, the verifier disposes.
