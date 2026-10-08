"""Server-rendered UI: Jinja2 templates, HTMX partials, Tailwind via CDN."""

import zlib
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import profiles as repo
from app.models import ProfileRecord, Topic
from app.runs import last_runs, text_status_counts
from app.summarize.views import summary_views
from app.topics import split_keywords

router = APIRouter(include_in_schema=False)
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
PAGE_SIZE = 24

try:
    _VERSION = version("discover-feed")
except PackageNotFoundError:  # pragma: no cover
    _VERSION = "0.0.0"

_PLACEHOLDER_GRADIENTS = (
    "from-sky-200 to-indigo-300 dark:from-sky-900 dark:to-indigo-900",
    "from-emerald-200 to-teal-300 dark:from-emerald-900 dark:to-teal-900",
    "from-amber-200 to-orange-300 dark:from-amber-900 dark:to-orange-900",
    "from-rose-200 to-pink-300 dark:from-rose-900 dark:to-pink-900",
    "from-violet-200 to-fuchsia-300 dark:from-violet-900 dark:to-fuchsia-900",
    "from-slate-200 to-slate-300 dark:from-slate-800 dark:to-slate-700",
)


def reltime(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return ""
    now = now or datetime.now(UTC)
    seconds = max((now - value).total_seconds(), 0)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)}d ago"
    label = f"{value:%b} {value.day}"  # no %-d: glibc-only, raises on Windows
    return label if value.year == now.year else f"{label}, {value.year}"


def snippet(text: str | None, length: int = 280) -> str:
    text = " ".join((text or "").split())
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def placeholder_gradient(key: str) -> str:
    return _PLACEHOLDER_GRADIENTS[zlib.crc32(key.encode()) % len(_PLACEHOLDER_GRADIENTS)]


TEMPLATES.env.filters.update(
    reltime=reltime, snippet=snippet, placeholder_gradient=placeholder_gradient
)


def _session(request: Request) -> Session:
    return request.app.state.session_factory()


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _profile_or_404(session: Session, slug: str) -> ProfileRecord:
    profile = repo.get_profile(session, slug)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"unknown profile {slug!r}")
    return profile


def _base_context(session: Session, profile: ProfileRecord | None, **extra: Any) -> dict:
    return {
        "profiles": repo.list_profiles(session),
        "profile": profile,
        "version": _VERSION,
        **extra,
    }


def _selected_topic(profile: ProfileRecord, topic_id: int | None) -> Topic | None:
    topic = repo.get_topic(profile, topic_id) if topic_id is not None else None
    return topic if topic is not None and topic.enabled else None


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    with _session(request) as session:
        profiles = repo.list_profiles(session)
        if profiles:
            return RedirectResponse(f"/p/{profiles[0].slug}", status_code=302)
        return TEMPLATES.TemplateResponse(request, "index.html", _base_context(session, None))


View = Literal["stories", "articles"]


def _feed_context(
    session: Session,
    profile: ProfileRecord,
    topic_id: int | None,
    cursor: str | None = None,
    view: View = "stories",
) -> dict:
    topic = _selected_topic(profile, topic_id)
    try:
        if view == "articles":
            page = repo.feed_page(session, profile, topic, limit=PAGE_SIZE, cursor=cursor)
        else:
            page = repo.story_feed_page(session, profile, topic, limit=PAGE_SIZE, cursor=cursor)
    except repo.InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    names = repo.source_names(session)
    story_ids = [item.story_id for item in page.items] if view == "stories" else []
    return _base_context(
        session,
        profile,
        topic=topic,
        tabs=[t for t in profile.topics if t.enabled],
        page=page,
        view=view,
        names=names,
        summaries=summary_views(session, story_ids, names),
    )


@router.get("/p/{slug}", response_class=HTMLResponse)
def profile_page(
    request: Request, slug: str, topic: int | None = None, view: View = "stories"
) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        context = _feed_context(session, profile, topic, view=view)
    return TEMPLATES.TemplateResponse(request, "feed.html", context)


@router.get("/p/{slug}/feed", response_class=HTMLResponse)
def feed_partial(
    request: Request,
    slug: str,
    topic: int | None = None,
    cursor: str | None = None,
    view: View = "stories",
) -> Response:
    """HTMX partial: tabs + grid for a topic, or just the next cards when ``cursor`` is set."""
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        context = _feed_context(session, profile, topic, cursor, view)
    template = "_cards.html" if cursor else "_feed.html"
    return TEMPLATES.TemplateResponse(request, template, context)


@router.get("/story/{story_id}", response_class=HTMLResponse)
def story_page(
    request: Request, story_id: int, p: str | None = None, topic: int | None = None
) -> Response:
    """All members of a story; with ``p`` only those from the profile's sources."""
    with _session(request) as session:
        profile = _profile_or_404(session, p) if p else None
        story = repo.get_story(session, story_id, profile)
        if story is None:
            raise HTTPException(status_code=404, detail=f"unknown story {story_id}")
        if profile is not None:
            selected = _selected_topic(profile, topic)
            back = f"/p/{profile.slug}" + (f"?topic={selected.id}" if selected else "")
        else:
            back = "/"
        names = repo.source_names(session)
        context = _base_context(
            session,
            profile,
            story=story,
            back=back,
            names=names,
            summary=summary_views(session, [story_id], names).get(story_id),
        )
    return TEMPLATES.TemplateResponse(request, "story.html", context)


# --- settings ------------------------------------------------------------------------------


def _settings_context(
    request: Request,
    session: Session,
    profile: ProfileRecord,
    *,
    error: str | None = None,
    message: str | None = None,
    message_topic: Topic | None = None,
    draft: dict | None = None,
) -> dict:
    source_ids = [s.id for s in profile.sources]
    return _base_context(
        session,
        profile,
        on_settings=True,
        topics=sorted(profile.topics, key=lambda t: t.position),
        statuses=repo.source_statuses(session, source_ids),
        **_pipeline_context(request, session),
        error=error,
        message=message,
        message_topic=message_topic,
        draft=draft or {},
    )


def _settings_response(request: Request, context: dict) -> Response:
    if _is_htmx(request):
        return TEMPLATES.TemplateResponse(request, "_topics.html", context)
    if context["error"]:
        return TEMPLATES.TemplateResponse(request, "settings.html", context, status_code=422)
    return RedirectResponse(f"/p/{context['profile'].slug}/settings", status_code=303)


async def _topic_form(request: Request) -> dict:
    form = await request.form()
    return {
        "name": str(form.get("name", "")),
        "include": split_keywords(str(form.get("include", ""))),
        "exclude": split_keywords(str(form.get("exclude", ""))),
        "source_ids": [str(v) for v in form.getlist("sources")],
        "enabled": form.get("enabled") is not None,
    }


def _pipeline_context(request: Request, session: Session) -> dict:
    worker = getattr(request.app.state, "summaries", None)
    return {
        "text_counts": text_status_counts(session),
        "runs": last_runs(session),
        "summarizer": worker.status() if worker is not None else None,
    }


@router.get("/p/{slug}/settings/pipeline", response_class=HTMLResponse)
def settings_pipeline(request: Request, slug: str) -> Response:
    """HTMX partial: the read-only Pipeline panel (reloaded after an admin button runs)."""
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        context = _base_context(session, profile, **_pipeline_context(request, session))
    return TEMPLATES.TemplateResponse(request, "_pipeline.html", context)


@router.get("/p/{slug}/settings", response_class=HTMLResponse)
def settings_page(request: Request, slug: str) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        context = _settings_context(request, session, profile)
    return TEMPLATES.TemplateResponse(request, "settings.html", context)


@router.post("/p/{slug}/settings/topics", response_class=HTMLResponse)
async def settings_create_topic(request: Request, slug: str) -> Response:
    data = await _topic_form(request)
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        try:
            topic = repo.create_topic(session, profile, **data)
            context = _settings_context(
                request,
                session,
                profile,
                message=f"Added topic “{topic.name}”.",
                message_topic=topic,
            )
        except repo.TopicError as exc:
            context = _settings_context(request, session, profile, error=str(exc), draft=data)
    return _settings_response(request, context)


def _topic_or_404(profile: ProfileRecord, topic_id: int) -> Topic:
    topic = repo.get_topic(profile, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail=f"unknown topic {topic_id}")
    return topic


@router.post("/p/{slug}/settings/topics/{topic_id}", response_class=HTMLResponse)
async def settings_update_topic(request: Request, slug: str, topic_id: int) -> Response:
    data = await _topic_form(request)
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        topic = _topic_or_404(profile, topic_id)
        try:
            repo.update_topic(session, profile, topic, **data)
            context = _settings_context(
                request, session, profile, message=f"Saved “{topic.name}”.", message_topic=topic
            )
        except repo.TopicError as exc:
            session.rollback()
            session.refresh(profile)
            context = _settings_context(request, session, profile, error=str(exc))
    return _settings_response(request, context)


@router.post("/p/{slug}/settings/topics/{topic_id}/move", response_class=HTMLResponse)
def settings_move_topic(request: Request, slug: str, topic_id: int, direction: str) -> Response:
    if direction not in ("up", "down"):
        raise HTTPException(status_code=422, detail="direction must be up or down")
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        repo.move_topic(
            session, profile, _topic_or_404(profile, topic_id), -1 if direction == "up" else 1
        )
        context = _settings_context(request, session, profile)
    return _settings_response(request, context)


@router.post("/p/{slug}/settings/topics/{topic_id}/delete", response_class=HTMLResponse)
def settings_delete_topic(request: Request, slug: str, topic_id: int) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        topic = _topic_or_404(profile, topic_id)
        name = topic.name
        repo.delete_topic(session, profile, topic)
        context = _settings_context(request, session, profile, message=f"Deleted “{name}”.")
    return _settings_response(request, context)
