# Public API (v1)

`/api/v1` is the stable read API for other applications. It serves published
stories only and never the extracted article text.

**Contract:** [openapi.v1.json](../openapi.v1.json) (also
`GET /api/v1/openapi.json`; browse everything at `/docs`).

## Endpoints

| Endpoint | |
| --- | --- |
| `GET /api/v1/profiles`, `/profiles/{slug}`, `/profiles/{slug}/topics` | profiles, sources, topics |
| `GET /api/v1/profiles/{slug}/feed` | `topic`, `limit` (1-100), `cursor`, `since` (first published after), `updated_since` (current version published after), `include=card\|full` (`full` adds `report` and `sources`) |
| `GET /api/v1/stories/{id}` | brief, report (or its `report_state`/`report_reason`), sources with raw URLs, `context[]` background links (url, title, domain, note, angle), model, versions; 301 for merged ids, 404 unless published |
| `GET /api/v1/profiles/{slug}/feed.atom` | Atom: title = headline, content = lead + bullets, links to the story page and sources |
| `GET /api/v1/stream` | SSE, see below |
| `GET/POST /api/v1/profiles/{slug}/alert-rules`, `PUT/DELETE .../alert-rules/{id}` | alert rules (writes need the admin key) |
| `GET /api/v1/profiles/{slug}/notifications` | notification log; `rule_id`, `status`, `limit` |
| `POST /api/v1/alert-rules/{id}/preview` | matches of the last 7 days, sends nothing (admin) |
| `GET /api/v1/profiles/{slug}/sources.opml` | OPML export (POST imports) |
| `GET /api/v1/sources`, `/sources/discover`, `/sources/test`, `/sources/health` | source management (writes need the admin key) |
| `GET /api/v1/stories/{id}/ask`, `/stories/{id}/followups`, `/conversations/{id}`, `/chat` | [Story Q&A](../features/qa.md) |
| `GET /api/v1/health`, `/api/v1/version` | |

```bash
curl -s 'localhost:8000/api/v1/profiles/personal-reader/feed?limit=5' | python -m json.tool
curl -s 'localhost:8000/api/v1/profiles/personal-reader/feed?include=full&cursor=<next_cursor>'
curl -s localhost:8000/api/v1/stories/42
curl -N 'localhost:8000/api/v1/stream?profile=personal-reader&include=card'
curl -N -H 'Last-Event-ID: 120' localhost:8000/api/v1/stream    # resume after event 120
curl -H "Authorization: Bearer $DISCOVER_API_KEY" localhost:8000/api/v1/profiles
```

=== "PowerShell"

    ```powershell
    Invoke-RestMethod 'http://localhost:8000/api/v1/profiles/personal-reader/feed?limit=5'
    $h = @{ Authorization = "Bearer $env:DISCOVER_API_KEY" }
    (Invoke-RestMethod 'http://localhost:8000/api/v1/stories/42' -Headers $h).brief.lead
    curl.exe -N -H "X-API-Key: $env:DISCOVER_API_KEY" 'http://localhost:8000/api/v1/stream?since_id=0'
    ```

## Stream

`GET /api/v1/stream?profile=&topic=&include=id|card|full&since_id=` sends a
`retry: 5000` hint, a `hello` event (`{latest_id, resume_from}`), then events
`story.published`, `story.updated` and `story.retracted`, each with `id: <n>`
(increasing) and `data: {"id", "type", "story_id", "version", "profiles"[,
"story"]}`, plus a `heartbeat` event every 15 s (`heartbeat=` to change).
Reconnect with the `Last-Event-ID` header (what `EventSource` does by itself)
or `since_id` to get every event you missed within `event_retention_days`;
without either the stream starts with new events. A slow client never holds up
the pipeline: its queue overflows and it catches up from the table.

## Examples

[`examples/client.py`](https://github.com/babcoccl/discover-feed/blob/main/examples/client.py)
(httpx: prints the feed, follows the stream and resumes after a disconnect;
`python examples/client.py --base http://nas:8000`) and
[`examples/stream.js`](https://github.com/babcoccl/discover-feed/blob/main/examples/stream.js)
(`EventSource`, browser or Node), plus `examples/ask.py` for Q&A.

## Versioning

Within v1 changes are additive only: new endpoints, new optional parameters,
new response fields (ignore unknown fields). Removing or renaming anything, or
changing a type or meaning, ships as `/api/v2`. The unversioned `/api/...` read
routes still work for one release as deprecated aliases (`Deprecation: true`
and `Link: <...>; rel="successor-version"` headers); `/api/admin/*` and the
topic write routes are not versioned. `make openapi` (`python -m app.openapi`)
regenerates the snapshot; `tests/test_api_v1.py` fails while it is out of date,
so API changes are always deliberate.
