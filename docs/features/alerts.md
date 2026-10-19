# Alerts and notification channels

## What it does

Alert rules notify you the moment a matching article is **ingested**: they look
at the title and feed summary right after the feed is stored, before text
extraction, clustering, briefs or the publish gate (so an unpublished story can
alert; the story link is added only once the story is published).

## How it works

- Manage them at **Alerts** (`/p/<slug>/alerts`, admin like Settings): rules,
  channels with **Send test**, a 7-day **Preview** that sends nothing, and the
  notification log (filter by rule/status, **Retry** failed).
- Feed cards show a bell badge with the rules that alerted; Settings > Pipeline
  shows rules, alerts fired in 24 h, queued, held and failed counts and each
  channel's last error.
- Rules/channels in YAML are seeded into the DB once per profile; afterwards
  the DB (UI/API) is the source of truth.

```yaml
profiles:
  market-monitor:
    alert_rules:                    # seeded into the DB once; edit them in the UI afterwards
      - name: Big tech tickers
        include: [$AAPL, $NVDA]     # $AAPL = case-sensitive "AAPL" (also matches "$AAPL")
        exclude: [proposed rule]    # excludes win over includes
        match: any                  # or all
        match_in: [title, summary]  # add `text` to match the extracted text too (checked
                                    # again after extraction)
        sources: [CNBC Markets]     # optional; default all the profile's sources
        priority: high              # low | default | high
        channels: [phone, email]
        quiet_hours: {start: "22:00", end: "07:00"}   # in alerts.timezone; held -> digest
        max_per_hour: 10            # overflow is held and sent as one digest
        notify_on_update: false     # true: one update (at most hourly) when a new source joins
    alerts:
      timezone: Europe/Berlin
      public_url: http://nas.local:8000   # story links in notifications
      alert_max_age_hours: 6              # older articles never alert (no backfill floods)
      notification_retention_days: 30
      max_attempts: 5                     # exponential backoff between attempts
      channels:
        - {id: phone, type: ntfy, server: https://ntfy.sh, topic: my-markets, token: ${NTFY_TOKEN}}
        - {id: hook, type: webhook, url: https://example.com/hook, secret: ${ALERT_WEBHOOK_SECRET}}
        - id: email
          type: email
          host: smtp.example.com
          port: 587
          tls: starttls                   # starttls | ssl (port 465) | none
          username: ${SMTP_USER}
          password: ${SMTP_PASSWORD}
          sender: alerts@example.com
          to: [me@example.com]
```

All keys: [alerts block](../configuration/reference.md#alerts-block),
[alert rules](../configuration/reference.md#alert-rules),
[channels](../configuration/reference.md#alert-channels).

## Secrets

Channel secrets (`token`, `secret`, `username`, `password`) must be
`${ENV_VAR}` references: only the variable name is stored and the value is read
when sending; it never appears in the DB, payloads, logs or errors. Each
channel has `timeout_seconds` (default 10).

## Channels

- **Webhook**: JSON POST with `X-Discover-Event`, `X-Discover-Delivery`
  (notification id) and `X-Discover-Signature: sha256=<hex>` = HMAC-SHA256 of
  the raw body with the secret.
- **ntfy**: JSON publish to the server root (topic, title, message, priority
  2/3/5, tags, click = story page if published, else the article), optional
  bearer token.
- **Email**: plain text + simple HTML, `Message-ID` from the notification id.

## Exactly once

A rule fires at most once per story lineage. Merged stories keep their history:
an article joining a story (or a story redirected into another) that already
alerted for the rule does not alert again, and an article that the clusterer
would put into an alerted story is suppressed even before clustering runs. A
unique constraint on (rule, lineage, kind, slot) makes concurrent evaluations
safe. Sent notifications are never re-sent after a restart; queued ones resume
(receivers can dedupe on the delivery id / Message-ID).

## Demo sink

`make demo` / `demo.cmd` start a local fake webhook + ntfy + SMTP server, point
the market-monitor channels at it and ingest a few "live" market items, so
alerts fire (sent, held in quiet hours, and failed via a demo channel that
always fails). See what was delivered at <http://localhost:8000/demo/sink>.
Standalone: `python -m app.alerts.sink --port 8099 --smtp-port 8025`
(PowerShell: same command with `.venv\Scripts\python`).

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Nothing alerts on old articles | `alert_max_age_hours` (6) | By design; only fresh articles fire |
| Alert arrives late | `quiet_hours` held it or `max_per_hour` digest | Check the log: status `held` → digest |
| Channel failed | Wrong `${ENV}` name or unreachable target | Alerts page lists missing names; **Send test** |
| Duplicate after restart | Should not happen — dedupe on delivery id | Report with `alert_firings` rows |
