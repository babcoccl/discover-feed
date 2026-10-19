# First-run checklist

Things to confirm on a fresh deployment.

## Configuration

- [ ] `config/profiles.yaml` exists (copied from `config/profiles.example.yaml`,
      saved as UTF-8) or `DISCOVER_CONFIG_PATH` points at your file.
- [ ] `llm.summarizer.base_url` is reachable from the app host; the startup log
      shows the `GET {base_url}/models` health check result.
- [ ] Secrets live in `.env` or the environment, never in `profiles.yaml`
      itself — YAML values reference them as `${VAR}` or `${VAR:-default}`.
- [ ] `DISCOVER_CONTACT_EMAIL` is set if you ingest SEC feeds (they expect a
      contact in the User-Agent; startup warns otherwise).

## Pipeline

- [ ] Feeds are fetched within about a minute of startup
      (`DISCOVER_REFRESH_JITTER_SECONDS`, default 60, spreads first runs).
- [ ] Settings > Pipeline shows stories moving
      `ingested → text → clustered → brief → report → published`.
- [ ] The first publish can take a while — generation is one story at a time by
      default (`summaries.max_inflight: 1`).

## Housekeeping

- [ ] The `discover-data` volume (or `data/` directory locally) is backed up:
      it holds the SQLite DB and the image cache. See
      [backups](../operations/backups.md).
- [ ] If the port leaves localhost: `DISCOVER_API_KEYS` + `DISCOVER_ADMIN_KEY`
      set, TLS reverse proxy in front. See
      [authentication](../features/authentication.md).
