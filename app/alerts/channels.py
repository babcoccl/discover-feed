"""Delivery to webhook / ntfy / email. Secrets are read from the environment at send time
and scrubbed from every error message."""

import hashlib
import hmac
import html
import json
import os
import re
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formatdate

import httpx

from app.config import SECRET_FIELDS, AlertChannel, ChannelType, EmailTLS

SIGNATURE_HEADER = "X-Discover-Signature"
NTFY_PRIORITY = {"low": 2, "default": 3, "high": 5}
_URL_SECRETS = re.compile(
    r"(https?://)[^/\s@]+@|([?&](?:token|key|secret|auth|sig)[^=&\s]*=)[^&\s]+", re.I
)


class ChannelError(Exception):
    """A delivery failed; the message is safe to store and log (no secrets)."""


def secrets_for(channel: AlertChannel) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for field in SECRET_FIELDS:
        name = getattr(channel, field)
        if name:
            value = os.environ.get(name)
            if not value:
                raise ChannelError(f"environment variable {name} ({field}) is not set")
            out[field] = value
        else:
            out[field] = None
    return out


def redact(text: str, secrets: dict[str, str | None] | None = None) -> str:
    for value in (secrets or {}).values():
        if value:
            text = text.replace(value, "***")
    text = _URL_SECRETS.sub(
        lambda m: (m.group(1) + "***@") if m.group(1) else m.group(2) + "***", text
    )
    return text[:500]


# --- rendering ----------------------------------------------------------------------------


def title_of(payload: dict) -> str:
    prefix = "[TEST] " if payload.get("test") else ""
    if payload.get("kind") == "update":
        prefix += "Update: "
    return prefix + (payload.get("title") or "Discover Feed alert")


def plain_body(payload: dict) -> str:
    if payload.get("kind") == "digest":
        lines = [f"{len(payload.get('items', []))} alerts for rule {payload.get('rule')!r}:"]
        for item in payload.get("items", []):
            lines.append(f"- {item.get('title')} ({item.get('source')}) {item.get('url')}")
        return "\n".join(lines)
    lines = []
    if payload.get("test"):
        lines.append("This is a test notification from Discover Feed.")
    if payload.get("kind") == "update" and payload.get("new_source"):
        lines.append(f"New source: {payload['new_source']}")
    lines.append(payload.get("source") or "")
    if payload.get("lead"):
        lines.append(payload["lead"])
    if payload.get("snippet"):
        lines.append(payload["snippet"])
    if payload.get("matched"):
        lines.append("Matched: " + ", ".join(payload["matched"]))
    lines.append(
        f"Rule: {payload.get('rule')} · {payload.get('time_local') or payload.get('time')}"
    )
    if payload.get("url"):
        lines.append(payload["url"])
    return "\n".join(line for line in lines if line)


def html_body(payload: dict) -> str:
    esc = html.escape
    if payload.get("kind") == "digest":
        items = "".join(
            f'<li><a href="{esc(i.get("url") or "")}">{esc(i.get("title") or "")}</a> '
            f"({esc(i.get('source') or '')})</li>"
            for i in payload.get("items", [])
        )
        return f"<p>{esc(plain_body(payload).splitlines()[0])}</p><ul>{items}</ul>"
    parts = []
    if payload.get("test"):
        parts.append("<p><strong>Test notification from Discover Feed.</strong></p>")
    url = payload.get("url") or ""
    parts.append(f'<h2><a href="{esc(url)}">{esc(title_of(payload))}</a></h2>')
    parts.append(f"<p>{esc(payload.get('source') or '')}</p>")
    for key in ("lead", "snippet"):
        if payload.get(key):
            parts.append(f"<p>{esc(payload[key])}</p>")
    if payload.get("matched"):
        parts.append(f"<p>Matched: {esc(', '.join(payload['matched']))}</p>")
    meta = f"Rule: {payload.get('rule') or ''} · {payload.get('time_local') or ''}"
    parts.append(f"<p><small>{esc(meta)}</small></p>")
    return "".join(parts)


# --- senders ------------------------------------------------------------------------------


def webhook_body(payload: dict, notification_id: int) -> bytes:
    doc = {"notification_id": notification_id, **payload}
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def deliver(
    channel: AlertChannel,
    payload: dict,
    notification_id: int,
    *,
    transport: httpx.BaseTransport | None = None,
    smtp_factory: Callable[..., smtplib.SMTP] | None = None,
) -> None:
    """Send one notification; raises ChannelError (redacted) on any failure."""
    secrets = secrets_for(channel)
    try:
        if channel.type is ChannelType.EMAIL:
            _email(channel, secrets, payload, notification_id, smtp_factory)
        else:
            with httpx.Client(timeout=channel.timeout_seconds, transport=transport) as client:
                if channel.type is ChannelType.WEBHOOK:
                    _webhook(client, channel, secrets, payload, notification_id)
                else:
                    _ntfy(client, channel, secrets, payload, notification_id)
    except ChannelError:
        raise
    except httpx.TimeoutException as exc:
        raise ChannelError(
            redact(f"timeout after {channel.timeout_seconds:g}s: {type(exc).__name__}", secrets)
        ) from None
    except httpx.HTTPStatusError as exc:
        raise ChannelError(
            redact(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}", secrets)
        ) from None
    except Exception as exc:
        raise ChannelError(redact(f"{type(exc).__name__}: {exc}", secrets)) from None


def _webhook(client, channel, secrets, payload, notification_id) -> None:
    body = webhook_body(payload, notification_id)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "discover-feed-alerts",
        "X-Discover-Event": payload.get("kind", "alert"),
        "X-Discover-Delivery": str(notification_id),
    }
    if secrets.get("secret"):
        headers[SIGNATURE_HEADER] = signature(secrets["secret"], body)
    client.post(str(channel.url), content=body, headers=headers).raise_for_status()


def _ntfy(client, channel, secrets, payload, notification_id) -> None:
    """ntfy's JSON publish (POST to the server root) so titles need no header encoding."""
    tags = ["test_tube"] if payload.get("test") else ["rotating_light"]
    tags += [t for t in (payload.get("matched") or [])[:3]]
    doc = {
        "topic": channel.topic,
        "title": title_of(payload),
        "message": plain_body(payload),
        "priority": NTFY_PRIORITY.get(payload.get("priority", "default"), 3),
        "tags": tags,
    }
    if payload.get("url"):
        doc["click"] = payload["url"]
    headers = {"X-Discover-Delivery": str(notification_id)}
    if secrets.get("token"):
        headers["Authorization"] = f"Bearer {secrets['token']}"
    server = str(channel.server).rstrip("/")
    client.post(server, json=doc, headers=headers).raise_for_status()


def _email(channel, secrets, payload, notification_id, smtp_factory) -> None:
    message = EmailMessage()
    message["Subject"] = title_of(payload)
    message["From"] = channel.sender
    message["To"] = ", ".join(channel.to)
    message["Date"] = formatdate(localtime=False)
    domain = (channel.sender or "discover-feed").rsplit("@", 1)[-1]
    message["Message-ID"] = f"<discover-notification-{notification_id}@{domain}>"
    message.set_content(plain_body(payload))
    message.add_alternative(f"<html><body>{html_body(payload)}</body></html>", subtype="html")
    timeout = channel.timeout_seconds
    if smtp_factory is not None:
        smtp = smtp_factory(channel.host, channel.port, timeout=timeout)
    elif channel.tls is EmailTLS.SSL:
        smtp = smtplib.SMTP_SSL(
            channel.host, channel.port, timeout=timeout, context=ssl.create_default_context()
        )
    else:
        smtp = smtplib.SMTP(channel.host, channel.port, timeout=timeout)
    with smtp:
        if channel.tls is EmailTLS.STARTTLS:
            smtp.starttls(context=ssl.create_default_context())
        if secrets.get("username") and secrets.get("password"):
            smtp.login(secrets["username"], secrets["password"])
        smtp.send_message(message)
