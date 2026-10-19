# Authentication and admin keys

## What it does

Optional auth for exposing the port (NAS, reverse proxy). Off by default.

## Configure

Set both in `.env`:

```bash
DISCOVER_API_KEYS=key-for-app-1,key-for-app-2   # /api/v1 and the legacy read API
DISCOVER_ADMIN_KEY=a-long-random-admin-key       # /api/admin/*, topic changes, Settings
```

Generate keys:

=== "Linux / macOS"

    ```bash
    python -c "import secrets; print(secrets.token_urlsafe(32))"
    ```

=== "Windows"

    ```powershell
    .venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"
    ```

## How it works

- Clients send `Authorization: Bearer <key>` or `X-API-Key: <key>`.
- With `DISCOVER_ADMIN_KEY` set, Settings (and every button that changes
  something) redirects to `/login`, which sets a signed, HttpOnly,
  SameSite=Strict cookie for 30 days; the admin key also works as an API key.
- Feed and story pages stay readable, and the id-only
  `/api/profiles/{slug}/stream` and `/published` endpoints the pages use stay
  open.
- Keys are compared in constant time, never logged and never stored; startup
  logs only whether auth is enabled.
- Put a TLS reverse proxy in front if the port leaves your LAN.
