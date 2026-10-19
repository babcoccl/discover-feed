# Backups and upgrades

## What to back up

Everything lives in the SQLite database plus the image cache:

- Docker: the `discover-data` volume (`/data/discover.db`, `/data/images/`).
- Local runs: `./data/` (`data/discover.db`, `data/images/`).

Backup: stop the container or copy `discover.db` with `sqlite3 .backup` /
`VACUUM INTO` for a consistent snapshot. The image cache is rebuildable — the
DB keeps the original URLs and re-fetches on demand.

```bash
sqlite3 data/discover.db "VACUUM INTO 'backup-$(date +%F).db'"
```

## Upgrades

1. `git pull` (or update the checkout).
2. `docker compose up -d --build` — **not** `restart`: `up` rebuilds the image
   and recreates the container; `restart` only restarts the existing one.
3. Migrations run in place on startup (idempotent). Old story data publishes
   under the current rules; no action needed.

Local: `source .venv/bin/activate && make install` (deps change between pulls),
then `make run`.

## Rollback

Keep the previous image (`docker images`) or check out the old tag and
rebuild. The DB migrations are forward-only — snapshot the DB before an
upgrade you might roll back.
