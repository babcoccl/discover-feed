# profiles.yaml

One YAML file defines every profile. `config/profiles.example.yaml` is a
complete commented example — copy it to `config/profiles.yaml` (git-ignored) or
point `DISCOVER_CONFIG_PATH` at your own path. Missing file = the app starts
with no profiles.

## Layout

```yaml
profiles:
  - id: personal-reader        # slug; feed lives at /p/personal-reader
    name: Personal Reader
    description: Long-form tech reading.
    topics: [...]              # feed tabs (keyword rules)
    keywords: [...]            # profile-level keywords
    sources: [...]             # feeds and API adapters
    alert_rules: [...]         # keyword alerts (seeded once)
    alerts: {...}              # channels and alert settings
    llm: {...}                 # summarizer + chat endpoints
    summaries: {...}           # brief/report behaviour
    enrichment: {...}          # web-search enrichment
    context: {...}             # background/context links
images: {...}                  # top level: card images
```

Every block and its keys are in the [config reference](reference.md).

## Environment variables in YAML

Any string may reference an env var: `${VAR}` or `${VAR:-default}` (the default
is used when `VAR` is unset **or empty**). Put secrets in `.env` or the
environment — never literal values in the file.

Secret fields are stricter: channel `token`/`secret`/`username`/`password` and
source `api_key` **must** be `${ENV_VAR}` references. Only the variable name is
stored; the value is read from the environment when needed and never reaches the
DB, logs, payloads or errors.

`.env` is read by docker compose (`env_file`) and by local runs (uvicorn,
`make demo`, `demo.cmd`, the `python -m app.*` tools) — `app/settings.py` loads
it at import without overriding variables already set in the shell. Point
elsewhere with `DISCOVER_ENV_FILE`.

## Seed once, then the DB wins

On first start the profiles, their sources, topics, alert rules and alert
channels are seeded into the database. From then on the **DB is the source of
truth**: edit topics in Settings or via the API; the YAML's topic/rule/channel
lists are ignored for an existing DB (start fresh with `docker compose down -v`
to reseed).

Two exceptions that still apply live:

- **Sources**: "Sync from YAML" adds profiles/sources/links the DB lacks — it
  never overwrites or deletes.
- **Per-request blocks** (`enrichment`, `context`, `summaries`, `alerts`
  settings, `images`, `llm`) are read from the YAML on every start — Settings >
  Pipeline toggles can override `enrichment.enabled` / `context.enabled` for an
  existing DB.

## Encoding

Save the file as **UTF-8**. Copy it between machines with `cp`/`scp` — not
PowerShell `Get-Content`/`Set-Content`, which can corrupt non-ASCII text and
produce `UnicodeDecodeError` on load.
