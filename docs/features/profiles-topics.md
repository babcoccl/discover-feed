# Profiles, topics and the UI

## What it does

A **profile** is one feed: its own topics (tabs), sources, alert rules and LLM
endpoint. A **topic** is a feed tab: an article matches when any `include`
keyword appears as a whole word (case-insensitive) in its title or feed summary
and no `exclude` keyword does. An empty `include` list means every article from
the topic's sources (optionally restricted with `sources`).

Matching runs at query time; no LLM is involved.

## How it works

On first start the profiles, their sources and topics are seeded from the YAML
config into the database; from then on the DB is the source of truth (edit
topics in the UI or API — see [profiles.yaml](../configuration/profiles.md)).

- `/` redirects to the first profile; `/p/{slug}` shows topic tabs and a card
  grid of stories ("Covered by N sources") or, with the Stories/Articles toggle,
  single articles.
- `/story/{id}` lists a story's articles, each linking to the publisher's own
  URL.
- `/p/{slug}/settings` adds, edits, reorders and deletes topics, shows source
  health, and has a Pipeline panel (text-status counts, last ingest/extract/
  cluster runs, buttons to run each).

## URLs

Articles carry two URLs: `url` is the feed's own permalink (trimmed, relative
links resolved) and is what cards and the API expose as the link;
`canonical_url` (lowercased host, no fragment, `utm_*`/`fbclid`/`gclid`/
`mc_cid`/`mc_eid`/`ref`/`ref_src` removed) is only the dedup key.

## Operate

```bash
curl localhost:8000/api/profiles
curl 'localhost:8000/api/profiles/personal-reader/feed?group=stories&topic=AI&limit=20'
curl 'localhost:8000/api/profiles/personal-reader/feed?group=articles&time_field=fetched'
curl -X POST localhost:8000/api/profiles/personal-reader/topics -d 'name=Space&include=space'
```

`GET /api/profiles/{slug}/feed?group=stories|articles&topic=&limit=&cursor=&time_field=published|fetched`
— pass the returned `next_cursor` to get the next page. Topic writes:
`POST /api/profiles/{slug}/topics`, `PUT`/`DELETE /api/profiles/{slug}/topics/{id}`.

See also the stable [public API](../reference/api.md).
