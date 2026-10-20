# Perspectives — how outlets differ

For stories covered by several sources, perspectives shows where outlets agree,
where they disagree, and how each one frames the story — strictly grounded in the
sources' own text. There are no truth ratings and no political labels for
outlets.

## How it works

1. **Per-source digest** — each member article gets a digest:
   `{claims: [{text, quote} ≤3], stance, kind, key_parties, framing}` where every
   `quote` is at most 15 words and must appear **verbatim** in the article text
   (whitespace and curly quotes normalized). Numbers must appear in the text.
   Invalid output is retried with the specific error and then dropped; the
   digest row is stored anyway with status `invalid`. Headline-only members get
   a deterministic title digest (`stance: unclear`) without an LLM call.
   Digests live in the `article_digests` table, keyed by content hash — a
   digest is computed once per (article text, story claim, prompt version,
   model) and reused.

2. **Synthesis** — once at least `perspectives.min_sources` (default 4) digests
   exist, one synthesis is generated:
   `{agreement: [{text, citations}], disagreements: [{topic, positions:
   [{label, summary, citations, quote}]}], different_angles: [...]}` and stored
   as a `story_summaries` row with `kind="perspectives"`.

   A disagreement is kept **only** when at least two positions each cite a
   different source, each with a verified quote, and the cited stances differ;
   otherwise it is omitted silently — balance is never invented. Position
   labels must describe the view (e.g. "says the tax will raise revenue"):
   an outlet's name or a political label is rejected. The synthesis runs the
   same number check and verbatim guard as briefs.

3. **Counter-narrative retrieval** — deep stories get extra context search
   angles: "criticism of", "defense of", "fact-check", affected parties'
   "reaction" and expert "analysis". Fact-check pages are stored as
   `context_kind="fact_check"` and labeled in the UI; opinion links stay
   labeled and capped by `context.max_opinion`.

4. **Scheduling** — the summary worker runs a perspectives pass after the
   follow-ups pass, once per story version (the input hash covers the member
   set, each member's text hash and the central claim). It is rate limited by
   `perspectives.max_per_hour`, backs off on transport failures like late
   regenerations, and never blocks briefs.

## UI and API

The story page gets a collapsible **How outlets differ** section (agreement,
disagreements with source chips and the quoted evidence, different angles) and
each source in the Coverage panel gets a stance badge (hidden when `unclear`).
API v1 adds `story.perspectives` and `source.stance`. A profile can turn the
section off from the pipeline settings page (`perspectives_enabled`).

## Configuration

Per profile (`perspectives:`) — see
[configuration reference](../configuration/reference.md#perspectives):

```yaml
perspectives:
  enabled: true
  min_sources: 4        # synthesize only with at least this many digests
  max_per_hour: 6       # rate limit, like late regenerations
  max_articles: 12      # at most this many members are digested
  digest_max_words: 1400  # article words sent to the digest prompt
  max_age_hours: 168    # stories are eligible while published within this
```

The work uses the profile's `llm.perspectives` role, defaulting to
`llm.summarizer` — point it at a stronger endpoint if you have one.

## Tools

```bash
python -m app.perspectives.diagnose --story-id N      # digests, quotes, errors, synthesis
python -m app.perspectives.evaluate --stories 20 --markdown   # review runs for false balance
```

`evaluate` is a dry run by default (writes nothing); pass `--store` to persist
syntheses, `--json` for machine output.
