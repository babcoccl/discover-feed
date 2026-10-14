"""Settings > Sources UI: health list, add-feed (discover, test fetch, save), API sources,
catalog picker, OPML import/export, edit, enable/disable, remove/restore, YAML sync."""

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app import source_admin as admin
from app.config import API_PARAMS, SourceType, load_config
from app.models import ProfileRecord
from app.profiles import list_profiles
from app.web import TEMPLATES, _base_context, _profile_or_404, _session

router = APIRouter(include_in_schema=False)

TYPE_LABELS = {
    SourceType.RSS: "RSS",
    SourceType.ATOM: "Atom",
    SourceType.GUARDIAN: "Guardian",
    SourceType.NEWSDATA: "NewsData.io",
    SourceType.X: "X",
}
LIMITS = ("daily_request_budget", "max_posts_per_run", "monthly_budget_usd", "cost_per_read_usd")


def _context(session: Session, profile: ProfileRecord, **extra) -> dict:
    return _base_context(
        session,
        profile,
        on_settings=True,
        type_labels=TYPE_LABELS,
        api_params={t.value: keys for t, keys in API_PARAMS.items()},
        all_profiles=list_profiles(session),
        **extra,
    )


def _page(request: Request, name: str, context: dict, status: int = 200) -> Response:
    return TEMPLATES.TemplateResponse(request, name, context, status_code=status)


def _back(slug: str, message: str, anchor: str = "sources") -> RedirectResponse:
    return RedirectResponse(f"/p/{slug}/settings?msg={quote(message)}#{anchor}", status_code=303)


async def _source_form(request: Request) -> tuple[dict, list[str]]:
    form = await request.form()
    kind = form.get("type") or "rss"
    data: dict = {
        "name": (form.get("name") or "").strip(),
        "type": kind,
        "tags": [t.strip() for t in (form.get("tags") or "").split(",") if t.strip()],
        "refresh_minutes": int(form.get("refresh_minutes") or 30),
        "aggregator": form.get("aggregator") == "on",
        "first_party": form.get("first_party") == "on",
        "enabled": form.get("enabled", "on") == "on",
    }
    if form.get("url"):
        data["url"] = form["url"].strip()
    try:
        source_type = SourceType(kind)
    except ValueError:
        source_type = SourceType.RSS
    if source_type in API_PARAMS:
        params = {}
        for key in API_PARAMS[source_type]:
            value = (form.get(f"p_{key}") or "").strip()
            if value:
                params[key] = value
        if source_type is SourceType.X and "users" in params:
            params["users"] = [u for u in params["users"].replace(",", " ").split() if u]
        data["params"] = params
        api_key = (form.get("api_key") or "").strip().removeprefix("${").removesuffix("}")
        data["api_key"] = api_key or None
        for key in LIMITS:
            value = (form.get(key) or "").strip()
            if value:
                data[key] = float(value) if "usd" in key else int(value)
    profiles = list(form.getlist("profiles"))
    return data, profiles


def _health_rows(session: Session, profile: ProfileRecord) -> dict:
    records = admin.profile_sources(profile)
    removed = [s for s in profile.sources if s.removed_at is not None]
    return {
        "source_health": admin.source_health(session, records),
        "auto_disabled": admin.auto_disabled(session, records),
        "removed_sources": removed,
    }


@router.get("/p/{slug}/settings/sources/new", response_class=HTMLResponse)
def new_source(request: Request, slug: str) -> Response:
    kind = request.query_params.get("type", "rss")
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        context = _context(session, profile, draft={"type": kind, "profiles": [slug]})
    return _page(request, "source_new.html", context)


@router.post("/p/{slug}/settings/sources/discover", response_class=HTMLResponse)
async def discover(request: Request, slug: str) -> Response:
    form = await request.form()
    url = (form.get("url") or "").strip()
    error, candidates = None, []
    if not url.startswith(("http://", "https://")):
        error = "Enter an http(s) URL of a site or feed."
    else:
        async with request.app.state.ingestor.http_client() as client:
            try:
                candidates = await admin.discover_feeds(client, url)
            except admin.SourceError as exc:
                error = str(exc)
        if not candidates and not error:
            error = f"No RSS or Atom feed found at {url}."
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        context = _context(
            session,
            profile,
            draft={"type": "rss", "profiles": [slug], "page_url": url},
            candidates=candidates,
            error=error,
        )
    return _page(request, "source_new.html", context, 422 if error else 200)


@router.post("/p/{slug}/settings/sources/test", response_class=HTMLResponse)
async def test_source(request: Request, slug: str) -> Response:
    data, profiles = await _source_form(request)
    if not data["name"]:
        data["name"] = data.get("url") or "New source"
    preview, error = None, None
    try:
        source = admin.build_source(data)
    except admin.SourceError as exc:
        error = str(exc)
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        if error is None:
            async with request.app.state.ingestor.http_client() as client:
                preview = await admin.test_fetch(client, source, session)
            if preview.ok and preview.title and data["name"] in (data.get("url"), "New source"):
                data["name"] = preview.title
            if preview.detected_type in (SourceType.RSS, SourceType.ATOM):
                data["type"] = preview.detected_type.value
        context = _context(
            session,
            profile,
            draft={**data, "profiles": profiles or [slug], "tags": ", ".join(data["tags"])},
            preview=preview,
            error=error,
        )
    return _page(request, "source_new.html", context, 422 if error else 200)


@router.post("/p/{slug}/settings/sources", response_class=HTMLResponse)
async def create_source(request: Request, slug: str) -> Response:
    data, profiles = await _source_form(request)
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        try:
            source = admin.build_source(data)
            record, created = admin.create_source(session, source, profiles or [slug])
        except admin.SourceError as exc:
            context = _context(
                session,
                profile,
                draft={**data, "profiles": profiles, "tags": ", ".join(data["tags"])},
                error=str(exc),
            )
            return _page(request, "source_new.html", context, 422)
        name = record.name
    admin.reschedule(request.app.state)
    verb = "Added" if created else "Shared existing source"
    return _back(slug, f"{verb} “{name}”.")


@router.get("/p/{slug}/settings/sources/catalog", response_class=HTMLResponse)
def catalog(request: Request, slug: str) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        present = {s.url for s in admin.profile_sources(profile)}
        entries = admin.load_catalog()
        categories: dict[str, list] = {}
        for entry in entries:
            categories.setdefault(entry.category, []).append(entry)
        context = _context(
            session,
            profile,
            categories=categories,
            present=present,
            message=request.query_params.get("msg"),
        )
    return _page(request, "source_catalog.html", context)


@router.post("/p/{slug}/settings/sources/catalog", response_class=HTMLResponse)
async def add_from_catalog(request: Request, slug: str) -> Response:
    form = await request.form()
    wanted = set(form.getlist("entry"))
    entries = [e for e in admin.load_catalog() if e.id in wanted]
    added = []
    with _session(request) as session:
        _profile_or_404(session, slug)
        for entry in entries:
            record, _ = admin.create_source(session, entry.to_source(), [slug])
            added.append(record.name)
    admin.reschedule(request.app.state)
    message = f"Added {len(added)} source(s): {', '.join(added)}." if added else "Nothing selected."
    return RedirectResponse(
        f"/p/{slug}/settings/sources/catalog?msg={quote(message)}", status_code=303
    )


@router.get("/p/{slug}/settings/sources.opml")
def export_opml(request: Request, slug: str) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        text = admin.export_opml(profile)
    return Response(
        text,
        media_type="text/x-opml",
        headers={"Content-Disposition": f'attachment; filename="{slug}.opml"'},
    )


@router.post("/p/{slug}/settings/sources/opml", response_class=HTMLResponse)
async def import_opml(request: Request, slug: str) -> Response:
    form = await request.form()
    upload = form.get("file")
    text = form.get("opml") or ""
    if upload is not None and hasattr(upload, "read"):
        content = await upload.read()
        if content:
            text = content.decode("utf-8", errors="replace")
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        try:
            result, error = admin.import_opml(session, profile, text), None
        except admin.SourceError as exc:
            result, error = None, str(exc)
        context = _context(
            session, profile, draft={"type": "rss", "profiles": [slug]}, opml=result, error=error
        )
    admin.reschedule(request.app.state)
    return _page(request, "source_new.html", context, 422 if error else 200)


@router.post("/p/{slug}/settings/sources/sync-yaml")
def sync_yaml(request: Request, slug: str) -> Response:
    path = Path(request.app.state.settings.config_path)
    if not path.exists():
        return _back(slug, f"Config file {path} not found.")
    try:
        config = load_config(path)
    except Exception as exc:
        return _back(slug, f"Invalid config: {exc}")
    with _session(request) as session:
        result = admin.sync_from_yaml(session, config)
    admin.reschedule(request.app.state)
    parts = [
        f"{len(result.profiles_added)} profile(s)",
        f"{len(result.sources_added)} source(s)",
        f"{len(result.sources_linked)} link(s)",
        f"{len(result.topics_added)} topic(s)",
    ]
    return _back(
        slug, "Synced from YAML: added " + ", ".join(parts) + ". Nothing was changed or deleted."
    )


def _record_or_404(session: Session, source_id: str):
    try:
        return admin.get_record(session, source_id)
    except admin.SourceError:
        raise HTTPException(status_code=404, detail="unknown source") from None


@router.get("/p/{slug}/settings/sources/{source_id}", response_class=HTMLResponse)
def source_detail(request: Request, slug: str, source_id: str) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        record = _record_or_404(session, source_id)
        context = _context(
            session,
            profile,
            source=record,
            health=admin.source_health(session, [record])[0],
            message=request.query_params.get("msg"),
        )
    return _page(request, "source_detail.html", context)


@router.post("/p/{slug}/settings/sources/{source_id}", response_class=HTMLResponse)
async def update_source(request: Request, slug: str, source_id: str) -> Response:
    data, profiles = await _source_form(request)
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        record = _record_or_404(session, source_id)
        try:
            admin.update_source(session, record, admin.build_source(data), profiles)
        except admin.SourceError as exc:
            context = _context(
                session,
                profile,
                source=record,
                health=admin.source_health(session, [record])[0],
                error=str(exc),
            )
            return _page(request, "source_detail.html", context, 422)
    admin.reschedule(request.app.state)
    return RedirectResponse(
        f"/p/{slug}/settings/sources/{source_id}?msg={quote('Saved.')}", status_code=303
    )


_ACTIONS = {
    "enable": (lambda s, r: admin.set_enabled(s, r, True), "Enabled"),
    "disable": (lambda s, r: admin.set_enabled(s, r, False), "Disabled"),
    "delete": (admin.remove_source, "Removed"),
    "restore": (admin.restore_source, "Restored"),
}


@router.post("/p/{slug}/settings/sources/{source_id}/{action}")
def source_action(request: Request, slug: str, source_id: str, action: str) -> Response:
    if action not in _ACTIONS:
        raise HTTPException(status_code=404)
    func, verb = _ACTIONS[action]
    with _session(request) as session:
        _profile_or_404(session, slug)
        record = _record_or_404(session, source_id)
        try:
            func(session, record)
            message = f"{verb} “{record.name}”."
            if action == "delete":
                message += " Its articles and stories are kept."
        except admin.SourceError as exc:
            message = str(exc)
    admin.reschedule(request.app.state)
    return _back(slug, message)
