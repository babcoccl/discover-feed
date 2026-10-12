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
from app import story_views as views
from app.config import CardStyle
from app.demo_pages import FIXTURE_DIR, page_html
from app.models import ProfileRecord, Topic
from app.pipeline_panel import pipeline_queue, quality_metrics, recent_attempts
from app.runs import last_runs, text_status_counts
from app.topics import TopicRule, split_keywords


def _card_style(request: Request) -> dict:
    """``card_style`` of the viewed profile's ``summaries`` (``?cards=`` overrides it)."""
    override = request.query_params.get("cards")
    if override in {c.value for c in CardStyle}:
        return {"card_style": override}
    slug = request.path_params.get("slug") or request.query_params.get("p")
    config = getattr(request.app.state, "config", None)
    profile = config.get_profile(slug) if config is not None and slug else None
    return {"card_style": profile.summaries.card_style.value if profile else "lead_bullets"}


router = APIRouter(include_in_schema=False)
TEMPLATES = Jinja2Templates(
    directory=Path(__file__).parent / "templates", context_processors=[_card_style]
)
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


def _story_page(
    session: Session, profile: ProfileRecord, topic: Topic | None, cursor: str | None
) -> views.Page:
    """Published stories only (newest ``last_updated_at`` first)."""
    rule = repo.topic_rule(topic) if topic else TopicRule()
    source_ids = {s.id for s in profile.sources}
    if rule.source_ids:
        source_ids &= set(rule.source_ids)
    if not source_ids:
        return views.Page(items=[], next_cursor=None)
    position = repo.decode_cursor(cursor) if cursor else None
    found, more = views.published_page(
        session, sorted(source_ids), rule, limit=PAGE_SIZE, cursor=position
    )
    next_cursor = repo.encode_cursor(found[-1][0], found[-1][1]) if more and found else None
    return views.Page(items=[story for _, _, story in found], next_cursor=next_cursor)


def _worker(request: Request):
    worker = getattr(request.app.state, "summaries", None)
    return worker if worker is not None and worker.enabled else None


def blockers(request: Request) -> list[str]:
    """Why nothing is being generated right now (shown on the feed and Pipeline panel)."""
    worker = _worker(request)
    health = getattr(request.app.state, "llm_health", None)
    if worker is None:
        return [
            "No LLM endpoint is set: add an llm.summarizer role to a profile "
            "(e.g. LOCAL_LLM_BASE_URL), then restart."
        ]
    found = []
    state = worker.state
    if health is not None and health.state == "unreachable":
        found.append(f"The LLM endpoint {health.host} is unreachable: {health.detail}")
    if (health is not None and health.state == "unauthorized") or "401" in (state.last_error or ""):
        found.append(
            f"The LLM endpoint {health.host if health else ''} answered HTTP 401: "
            "check the API key."
        )
    now = datetime.now(UTC)
    if state.paused_by_admin:
        found.append("Generation is paused (Settings > Pipeline > Resume).")
    elif state.paused_until and state.paused_until > now:
        found.append(
            f"Generation paused after {state.consecutive_failures} endpoint failures in a row "
            f"(last: {state.last_error}); retrying {reltime(state.paused_until, now)}."
        )
    return found


def preparing(session: Session) -> dict:
    """Live counts for "Preparing your first stories..."."""
    counts = views.stage_counts(session)
    done = counts["published"]
    total = done + counts["clustered"] + counts["brief_ready"] + counts["report_final"]
    return {
        "ingested": counts["ingested"],
        "extracting": counts["text_pending"],
        "clustering": counts["text_final"],
        "summarizing": (min(done + 1, total) if total > done else done, total),
        "published": done,
    }


def _feed_context(
    request: Request,
    session: Session,
    profile: ProfileRecord,
    topic_id: int | None,
    cursor: str | None = None,
    view: View = "stories",
) -> dict:
    topic = _selected_topic(profile, topic_id)
    try:
        if view == "articles":
            page = repo.feed_page(
                session, profile, topic, limit=PAGE_SIZE, cursor=cursor, published_only=True
            )
        else:
            page = _story_page(session, profile, topic, cursor)
    except repo.InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    context = _base_context(
        session,
        profile,
        topic=topic,
        tabs=[t for t in profile.topics if t.enabled],
        page=page,
        view=view,
        names=repo.source_names(session),
        blockers=blockers(request),
        stream_url=f"/api/profiles/{profile.slug}/stream",
    )
    if not page.items and cursor is None:
        context["preparing"] = preparing(session)
    return context


@router.get("/p/{slug}", response_class=HTMLResponse)
def profile_page(
    request: Request, slug: str, topic: int | None = None, view: View = "stories"
) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        context = _feed_context(request, session, profile, topic, view=view)
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
        context = _feed_context(request, session, profile, topic, cursor, view)
    template = "_cards.html" if cursor else "_feed.html"
    return TEMPLATES.TemplateResponse(request, template, context)


@router.get("/story/{story_id}", response_class=HTMLResponse)
def story_page(
    request: Request, story_id: int, p: str | None = None, topic: int | None = None
) -> Response:
    """A published story; with ``p`` only members from the profile's sources. Merged ids
    redirect to the story they joined; unpublished stories are 404 (see the Pipeline)."""
    with _session(request) as session:
        profile = _profile_or_404(session, p) if p else None
        target = views.resolve(session, story_id)
        if target is not None and target != story_id:
            query = request.url.query
            return RedirectResponse(
                f"/story/{target}" + (f"?{query}" if query else ""), status_code=301
            )
        source_ids = [s.id for s in profile.sources] if profile is not None else None
        story = views.published_story(session, story_id, source_ids)
        if story is None:
            raise HTTPException(status_code=404, detail=f"story {story_id} is not published")
        if profile is not None:
            selected = _selected_topic(profile, topic)
            back = f"/p/{profile.slug}" + (f"?topic={selected.id}" if selected else "")
        else:
            back = "/"
        context = _base_context(
            session,
            profile,
            story=story,
            back=back,
            names=repo.source_names(session),
            brief=story.brief,
            summary=story.brief,
            **_report_context(request, story),
        )
    return TEMPLATES.TemplateResponse(request, "story.html", context)


def _report_context(request: Request, story, state: str | None = None) -> dict:
    report = story.report if story is not None else None
    return {
        "story_id": story.story_id if story is not None else None,
        "report": report,
        "report_state": state or (report.state if report is not None else "off"),
        "can_regenerate": _worker(request) is not None,
        "p": request.query_params.get("p"),
    }


@router.post("/story/{story_id}/regenerate", response_class=HTMLResponse)
def story_regenerate(request: Request, story_id: int, kind: str = "both") -> Response:
    """Regenerate / Retry (admin): queue a forced brief and/or report for this story first.
    The published version stays until the new one passes the publish rule."""
    worker = _worker(request)
    if worker is None:
        raise HTTPException(status_code=409, detail="no summarizer configured")
    if kind not in ("brief", "report", "both"):
        raise HTTPException(status_code=422, detail="kind must be brief, report or both")
    if not worker.retry(story_id, kind):
        raise HTTPException(status_code=404, detail=f"unknown story {story_id}")
    worker.start(story_id=story_id, manual=True)
    with _session(request) as session:
        story = views.published_story(session, story_id)
    context = {**_report_context(request, story, "queued"), "story_id": story_id}
    return TEMPLATES.TemplateResponse(request, "_report.html", context)


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
        "blockers": blockers(request),
        "stages": preparing(session),
        "queue": pipeline_queue(session, _worker(request)),
        "attempts": recent_attempts(session),
        "quality": quality_metrics(session),
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


@router.get("/demo-article/{article_id}", response_class=HTMLResponse)
def demo_article(article_id: str) -> HTMLResponse:
    """Local demo fixture page (the demo's feed items link here), marked "Demo article"."""
    body = page_html(FIXTURE_DIR, article_id, banner=True)
    if body is None:
        raise HTTPException(404, "no such demo article")
    return HTMLResponse(body)


# --- pipeline admin actions (Settings > Pipeline) -----------------------------------------


def _pipeline_response(request: Request, slug: str) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        context = _base_context(session, profile, **_pipeline_context(request, session))
    return TEMPLATES.TemplateResponse(request, "_pipeline.html", context)


def _admin_worker(request: Request):
    worker = _worker(request)
    if worker is None:
        raise HTTPException(status_code=409, detail="no summarizer configured")
    return worker


@router.post("/p/{slug}/settings/pipeline/stories/{story_id}/prioritize")
def pipeline_prioritize(request: Request, slug: str, story_id: int) -> Response:
    worker = _admin_worker(request)
    if not worker.prioritize(story_id):
        raise HTTPException(status_code=404, detail=f"unknown story {story_id}")
    worker.start()
    return _pipeline_response(request, slug)


@router.post("/p/{slug}/settings/pipeline/stories/{story_id}/retry")
def pipeline_retry(request: Request, slug: str, story_id: int, kind: str = "both") -> Response:
    worker = _admin_worker(request)
    if not worker.retry(story_id, kind):
        raise HTTPException(status_code=404, detail=f"unknown story {story_id}")
    worker.start(story_id=story_id, manual=True)
    return _pipeline_response(request, slug)


@router.post("/p/{slug}/settings/pipeline/pause")
def pipeline_pause(request: Request, slug: str) -> Response:
    _admin_worker(request).set_paused(True)
    return _pipeline_response(request, slug)


@router.post("/p/{slug}/settings/pipeline/resume")
def pipeline_resume(request: Request, slug: str) -> Response:
    worker = _admin_worker(request)
    worker.set_paused(False)
    worker.start()
    return _pipeline_response(request, slug)
