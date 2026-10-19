# Clustering and tuning

## What it does

Each unassigned article joins the most similar story or starts a new one
(`app/cluster/`). Stories are shared by all profiles: a profile's feed shows
only the members from sources that profile follows, and a story matches a topic
if any of those members matches.

## How it works

The default clusterer (`TfidfClusterer`) builds TF-IDF vectors (single words,
stopwords removed, plurals and possessives normalized, sublinear tf) from the
title (counted twice), the feed summary and the first 1,000 words of extracted
text. It compares each article with every story's centroid. An article joins
the best story only if all of these hold:

| Parameter (env var) | Default | Rule |
| --- | --- | --- |
| `DISCOVER_CLUSTER_THRESHOLD` | `0.45` | cosine similarity to the story centroid must be at least this |
| `DISCOVER_CLUSTER_WINDOW_HOURS` | `72` | published within this many hours of the story's first or latest member |
| `DISCOVER_CLUSTER_MIN_SHARED_TOKENS` | `2` | shares this many significant tokens (non-stopword and 4+ chars or capitalized, e.g. `Fed`, `M5`) with the story title |
| `DISCOVER_CLUSTER_SAME_SOURCE_THRESHOLD` | `0.7` | if the story already has an article from the same source, similarity must reach this instead |

The story's representative is the earliest article from the source with the
longest text; it supplies the title, image and snippet. Clustering is
incremental and idempotent: re-running it only touches unassigned articles.
Other algorithms implement `Clusterer.assign(new_articles, existing_stories)`
in `app/cluster/base.py`.

When clustering merges stories the oldest id is kept; the others become
`merged` and `/story/{old}` and `/api/stories/{old}` redirect (301) to it.

## Operate

```bash
curl -X POST localhost:8000/api/admin/cluster            # cluster unassigned articles
curl -X POST 'localhost:8000/api/admin/cluster?rebuild=true'   # re-cluster everything
python -m app.cluster --rebuild                          # same, from the shell
curl localhost:8000/api/stories/1                        # all members, any profile
```

Re-cluster after a parameter change so existing articles re-group under the new
thresholds.

## Tuning

The demo fixtures are labelled: `tests/fixtures/demo/stories.yaml` lists which
articles are the same story, plus near-miss pairs that must stay apart (same
company or regulator, different event). To try other parameters, run:

```bash
python -m app.cluster.evaluate --threshold 0.35 0.45 0.55   # sweep: pairwise precision/recall
python -m app.cluster.evaluate --markdown                    # per-member join scores, near-miss cosines
python -m app.cluster.evaluate --window-hours 48 --min-shared-tokens 3 --same-source-threshold 0.8
```

Procedure:

1. Find the threshold range where precision stays at 100% and recall is
   highest. Joins are reported with the score they joined at, and near misses
   with their cosine. Keep the threshold well above the highest near-miss
   cosine.
2. Lower the threshold if real related stories fail to merge (shown under
   "missed"). Raise it, or raise `MIN_SHARED_TOKENS`, if you see false merges
   in production.
3. If a live false merge or miss repeats, add it to the fixtures (feed item +
   page in `tests/fixtures/demo/`, label in `stories.yaml`) so
   `tests/test_cluster.py` covers it.
4. Set the env var and re-cluster existing data: `python -m app.cluster --rebuild`.

At the defaults the fixtures score 100% precision and 88% recall. The one miss
is the Fed's formal FOMC statement, which shares too little wording with the
press coverage of the rate cut (cosine about 0.33). Reaching 100% recall would
need a threshold of about 0.30, which leaves too little margin against false
merges on live feeds.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Same event split across stories | Threshold too strict, or too little shared wording | Lower `DISCOVER_CLUSTER_THRESHOLD`; check `evaluate --markdown` |
| Unrelated articles merged ("false merges") | Threshold too loose, generic titles | Raise `DISCOVER_CLUSTER_THRESHOLD` / `DISCOVER_CLUSTER_MIN_SHARED_TOKENS`; add the pair to fixtures |
| Old articles keep joining | Window too wide | Lower `DISCOVER_CLUSTER_WINDOW_HOURS` |
