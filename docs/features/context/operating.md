# Context links — operating

## Pipeline panel

Settings > Pipeline shows the context toggle plus live stats (eligible /
waiting / done / stories with links / links added / judge accept rate /
rejections by reason) and a "Find context now" button.
`POST /api/admin/context[?story_id=N]` triggers a run for all eligible stories
or a single one (admin key).

## Diagnose and evaluate

```bash
python -m app.context.diagnose --story-id 42            # one story, writes nothing
python -m app.context.diagnose --story-id 42 --dry-run  # queries + decisions, no writes
python -m app.context.evaluate --stories 30 --markdown  # dry run over 30 recent stories
# inside Docker:
docker exec discover-feed python -m app.context.evaluate --stories 30 --markdown
```

=== "Windows"

    ```powershell
    .venv\Scripts\python -m app.context.evaluate --stories 30 --markdown
    ```

## Reasons table

`context_reason` on a story that kept nothing:

| Reason | Meaning |
| --- | --- |
| `no_angles` | No search angles could be built (LLM absent and no entities) |
| `skipped_cooldown` | Providers were cooling down; pass paused, story stays `pending` |
| `provider_errors:<kind>` | Providers failed; story is `retry` with backoff |
| `no_hits` | Searches ran, nothing came back |
| `all_rejected` | Candidates found, all rejected |

Candidate rejection reasons (in `context_candidates`): `same_domain`,
`social`, `aggregator`, `denied`, `bad_url`, `duplicate_url`,
`already_ingested`, `language`, `off_topic`, `same_event` (with
`same_event_signal`), `near_duplicate`, `min_words`, `flagged`, `domain_cap`,
`opinion_cap`, `not_article`, `judge_rejected`, `judge_failed`,
`judge_same_event`, `robots`, fetch/paywall failures.
