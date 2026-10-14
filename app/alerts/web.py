"""The Alerts page (``/p/{slug}/alerts``, admin like Settings) and the demo sink page."""

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.alerts import repo
from app.alerts.api import notification_out, query_notifications
from app.config import SECRET_FIELDS, AlertChannel, secret_ref_name
from app.models import Notification, ProfileRecord
from app.secret_warnings import missing_secrets
from app.web import TEMPLATES, _base_context, _profile_or_404, _session

router = APIRouter(include_in_schema=False)
STATUSES = ("queued", "sent", "failed", "held", "digested")
_BLANK_ROWS = 3


def _context(request: Request, session: Session, profile: ProfileRecord, **extra) -> dict:
    params = request.query_params
    rule_filter = params.get("rule")
    status_filter = params.get("status") if params.get("status") in STATUSES else None
    rule_id = int(rule_filter) if rule_filter and rule_filter.isdigit() else None
    rules = repo.list_rules(session, profile)
    edit = extra.pop("edit", None)
    if edit is None and params.get("edit", "").isdigit():
        edit = repo.get_rule(session, int(params["edit"]), profile)
    return _base_context(
        session,
        profile,
        on_alerts=True,
        secret_warnings=missing_secrets(session),
        rules=rules,
        rule_dicts={r.id: repo.rule_dict(r) for r in rules},
        channels=repo.list_channels(session, profile),
        notifications=[
            notification_out(n)
            for n in query_notifications(session, profile, rule_id, status_filter, 100)
        ],
        rule_filter=rule_id,
        status_filter=status_filter,
        statuses=STATUSES,
        stats=repo.stats(session, profile.id),
        edit=repo.rule_dict(edit) if edit is not None else None,
        blank_rows=_BLANK_ROWS,
        preview=None,
        message=None,
        error=None,
        secret_fields=SECRET_FIELDS,
        **extra,
    )


def _page(request: Request, context: dict, status: int = 200) -> Response:
    return TEMPLATES.TemplateResponse(request, "alerts.html", context, status_code=status)


@router.get("/p/{slug}/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, slug: str) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        return _page(request, _context(request, session, profile))


def _keywords(form, prefix: str) -> list[dict]:
    out = []
    for i in range(200):
        if f"{prefix}-{i}" not in form:
            break
        text = " ".join(str(form.get(f"{prefix}-{i}", "")).split())
        if text:
            out.append(
                {"keyword": text, "case_sensitive": form.get(f"{prefix}-{i}-cs") is not None}
                if not text.startswith("$")
                else text
            )
    return out


async def _rule_form(request: Request) -> dict:
    form = await request.form()
    start, end = str(form.get("quiet_start", "")), str(form.get("quiet_end", ""))
    try:
        per_hour = max(1, int(str(form.get("max_per_hour") or 10)))
    except ValueError:
        per_hour = 10
    return {
        "name": str(form.get("name", "")),
        "enabled": form.get("enabled") is not None,
        "include": _keywords(form, "inc"),
        "exclude": _keywords(form, "exc"),
        "match": "all" if form.get("match") == "all" else "any",
        "match_in": [v for v in form.getlist("match_in") if v in ("title", "summary", "text")]
        or ["title", "summary"],
        "source_ids": [str(v) for v in form.getlist("sources")],
        "channels": [str(v) for v in form.getlist("channels")],
        "priority": str(form.get("priority") or "default"),
        "quiet_hours": {"start": start, "end": end} if start and end else None,
        "max_per_hour": per_hour,
        "notify_on_update": form.get("notify_on_update") is not None,
    }


def _save_rule(request: Request, session: Session, profile, data: dict, rule=None) -> Response:
    try:
        saved = repo.save_rule(session, profile, data, rule)
        session.commit()
    except ValueError as exc:
        session.rollback()
        draft = {**data, "id": rule.id if rule is not None else None}
        draft["include"] = repo.normalize(data["include"]) if data["include"] else []
        draft["exclude"] = repo.normalize(data["exclude"]) if data["exclude"] else []
        context = _context(request, session, profile, error=str(exc))
        context["edit"] = draft
        return _page(request, context, 422)
    return RedirectResponse(f"/p/{profile.slug}/alerts?saved={saved.id}", status_code=303)


@router.post("/p/{slug}/alerts/rules", response_class=HTMLResponse)
async def create_rule(request: Request, slug: str) -> Response:
    data = await _rule_form(request)
    with _session(request) as session:
        return _save_rule(request, session, _profile_or_404(session, slug), data)


def _rule_or_404(session: Session, profile, rule_id: int):
    rule = repo.get_rule(session, rule_id, profile)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"unknown rule {rule_id}")
    return rule


@router.post("/p/{slug}/alerts/rules/{rule_id}", response_class=HTMLResponse)
async def update_rule(request: Request, slug: str, rule_id: int) -> Response:
    data = await _rule_form(request)
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        return _save_rule(request, session, profile, data, _rule_or_404(session, profile, rule_id))


@router.post("/p/{slug}/alerts/rules/{rule_id}/toggle")
def toggle_rule(request: Request, slug: str, rule_id: int) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        rule = _rule_or_404(session, profile, rule_id)
        rule.enabled = not rule.enabled
        session.commit()
    return RedirectResponse(f"/p/{slug}/alerts", status_code=303)


@router.post("/p/{slug}/alerts/rules/{rule_id}/delete")
def delete_rule(request: Request, slug: str, rule_id: int) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        session.delete(_rule_or_404(session, profile, rule_id))
        session.commit()
    return RedirectResponse(f"/p/{slug}/alerts", status_code=303)


@router.post("/p/{slug}/alerts/rules/{rule_id}/preview", response_class=HTMLResponse)
def preview_rule(request: Request, slug: str, rule_id: int) -> Response:
    """Dry run over the last 7 days: lists matches, sends nothing."""
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        rule = _rule_or_404(session, profile, rule_id)
        matches = request.app.state.alerts.preview(session, rule)
        if request.headers.get("hx-request"):
            return TEMPLATES.TemplateResponse(
                request, "_alert_preview.html", {"rule": rule, "matches": matches}
            )
        context = _context(request, session, profile, preview={"rule": rule, "matches": matches})
        return _page(request, context)


async def _channel_form(request: Request) -> AlertChannel:
    form = await request.form()
    data: dict = {
        "id": str(form.get("key", "")).strip(),
        "name": str(form.get("name", "")).strip(),
        "type": str(form.get("type", "")),
        "enabled": form.get("enabled") is not None,
    }
    for field in ("url", "server", "topic", "host", "sender"):
        if str(form.get(field, "")).strip():
            data[field] = str(form.get(field)).strip()
    for field in ("port", "timeout_seconds"):
        if str(form.get(field, "")).strip():
            data[field] = str(form.get(field)).strip()
    if form.get("tls"):
        data["tls"] = str(form.get("tls"))
    data["to"] = [t.strip() for t in str(form.get("to", "")).split(",") if t.strip()]
    for field in SECRET_FIELDS:
        value = str(form.get(field, "")).strip()
        if value:
            data[field] = secret_ref_name(value, field)
    return AlertChannel.model_validate(data)


@router.post("/p/{slug}/alerts/channels", response_class=HTMLResponse)
async def save_channel(request: Request, slug: str) -> Response:
    """Create or update (same key) a channel; secrets must be ``${ENV_VAR}`` references."""
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        try:
            channel = await _channel_form(request)
        except (ValidationError, ValueError) as exc:
            errors = exc.errors() if isinstance(exc, ValidationError) else None
            text = "; ".join(e["msg"] for e in errors) if errors else str(exc)
            return _page(request, _context(request, session, profile, error=text), 422)
        existing = next(
            (c for c in repo.list_channels(session, profile) if c.key == channel.id), None
        )
        if existing is None:
            session.add(repo.channel_record(profile.id, channel))
        else:
            repo.apply_channel(existing, channel)
        session.commit()
    return RedirectResponse(f"/p/{slug}/alerts#channels", status_code=303)


def _channel_or_404(session: Session, profile, channel_id: int):
    channel = repo.get_channel(session, profile, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"unknown channel {channel_id}")
    return channel


@router.post("/p/{slug}/alerts/channels/{channel_id}/delete")
def delete_channel(request: Request, slug: str, channel_id: int) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        session.delete(_channel_or_404(session, profile, channel_id))
        session.commit()
    return RedirectResponse(f"/p/{slug}/alerts#channels", status_code=303)


@router.post("/p/{slug}/alerts/channels/{channel_id}/test", response_class=HTMLResponse)
def test_channel(request: Request, slug: str, channel_id: int) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        channel = _channel_or_404(session, profile, channel_id)
        name = channel.name
    result = request.app.state.alert_sender.send_test(channel_id)
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        if result.status == "sent":
            context = _context(request, session, profile, message=f"Test sent to “{name}”.")
        else:
            context = _context(
                request, session, profile, error=f"Test to “{name}” failed: {result.last_error}"
            )
        return _page(request, context)


@router.post("/p/{slug}/alerts/notifications/{notification_id}/retry")
def retry_notification(request: Request, slug: str, notification_id: int) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        n = session.get(Notification, notification_id)
        if n is None or n.profile_id != profile.id:
            raise HTTPException(status_code=404, detail="unknown notification")
    request.app.state.alert_sender.retry(notification_id)
    return RedirectResponse(f"/p/{slug}/alerts?status=queued#log", status_code=303)


@router.get("/demo/sink", response_class=HTMLResponse)
def demo_sink(request: Request) -> Response:
    sink = getattr(request.app.state, "demo_sink", None)
    if sink is None:
        raise HTTPException(status_code=404, detail="the demo sink only runs with `make demo`")
    with _session(request) as session:
        context = _base_context(session, None, messages=list(reversed(sink.received())), sink=sink)
    return TEMPLATES.TemplateResponse(request, "demo_sink.html", context)
