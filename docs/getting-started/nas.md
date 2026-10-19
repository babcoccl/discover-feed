# Deploy on a NAS or home server

The app is designed for a small always-on box: one container, one SQLite
volume, no external services beyond your LLM endpoint.

## Compose

`docker-compose.yml` ships ready to run: it mounts `./config` read-only, passes
`.env` through, and puts the database + image cache on the `discover-data`
volume.

=== "Linux / NAS"

    ```bash
    cp config/profiles.example.yaml config/profiles.yaml
    cp .env.example .env
    docker compose up -d          # start (builds first time)
    docker compose up -d --build  # rebuild after pulling changes
    docker compose restart        # same image, just restart the process
    docker compose down           # stop, keep data
    docker compose down -v        # stop and delete the database + image cache
    ```

=== "Windows (Docker Desktop)"

    ```powershell
    copy config\profiles.example.yaml config\profiles.yaml
    copy .env.example .env
    docker compose up -d
    ```

!!! note "`up -d` vs `restart`"
    `docker compose up -d` recreates the container when the compose file or
    image changed — use it for upgrades. `docker compose restart` only restarts
    the existing container: config changes in `profiles.yaml` are re-read
    (it's a mount), but new env vars or image changes need `up -d`.

## Config and env files

- `config/` is mounted read-only; edit `profiles.yaml` on the host and
  `docker compose restart` (or hit Settings — most options re-read live).
- Save YAML as **UTF-8**. Copy it to the NAS with `cp`/`scp` — do not rewrite it
  with PowerShell `Get-Content`/`Set-Content`, which can corrupt encoding and
  cause `UnicodeDecodeError` at startup.
- `.env` is read by compose (`env_file`) and by local runs; variables already
  set in the shell win.

## Volumes and backups

The `discover-data` volume holds `/data/discover.db` (all state) and
`/data/images` (the card-image cache). Back it up by copying the volume or the
files while the container is stopped — see
[backups and upgrades](../operations/backups.md).

## Exposing the port

Before binding the port beyond localhost, set `DISCOVER_API_KEYS` and
`DISCOVER_ADMIN_KEY` in `.env` (feed pages stay readable; writes and Settings
require the admin key). Put a TLS reverse proxy in front if it leaves your LAN.
Details: [authentication](../features/authentication.md).

## No Docker?

Python 3.12+ works directly:

=== "Linux / macOS"

    ```bash
    python3.12 -m venv .venv && source .venv/bin/activate
    make install
    make run            # uvicorn on :8000
    ```

=== "Windows"

    ```powershell
    py -3.12 -m venv .venv; .venv\Scripts\activate
    python -m pip install -e ".[dev]"
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 5
    ```
