"""Server-rendered UI: Jinja2 templates, HTMX partials, Tailwind via CDN."""

import logging
import zlib
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import images
from app import profiles as repo
from app import story_views as views
from app.alerts import repo as alert_repo
from app.config import CardStyle, NoImageStyle
from app.demo_images import demo_image
from app.demo_pages import FIXTURE_DIR, page_html
from app.images.cache import image_key, is_key
from app.images.service import ImageWorker, image_stats
from app.models import ProfileRecord, StorySummary, Topic
from app.pipeline_panel import pipeline_queue, quality_metrics, recent_attempts
from app.runs import last_runs, text_status_counts
from app.secret_warnings import missing_secrets
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


def _image_style(request: Request) -> dict:
    """``images.no_image_style`` (``?no_image=`` overrides it, for previews)."""
    override = request.query_params.get("no_image")
    if override in {s.value for s in NoImageStyle}:
        return {"no_image_style": override}
    return {"no_image_style": images.no_image_style().value}


logger = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
TEMPLATES = Jinja2Templates(
    directory=Path(__file__).parent / "templates",
    context_processors=[_card_style, _image_style],
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


def image_src(image: dict | None) -> str | None:
    """The cached WebP (``/img/{key}``) when there is one, else the original URL."""
    if not image or not image.get("url"):
        return None
    if image.get("key") and images.config().cache:
        return f"/img/{image['key']}"
    return image["url"]


def article_image(article) -> str | None:
    """An article card's image: the checked page/feed image (cached when possible), or the
    unchecked feed image."""
    if article.image_checked_at is None:
        return article.image_url
    url = {"page": article.page_image_url, "feed": article.image_url}.get(
        article.image_source or ""
    )
    if url and images.config().cache:
        return f"/img/{image_key(url)}"
    return url


def megabytes(n: int | None) -> str:
    return f"{(n or 0) / (1024 * 1024):.1f} MB"


TEMPLATES.env.filters.update(
    reltime=reltime,
    snippet=snippet,
    placeholder_gradient=placeholder_gradient,
    image_src=image_src,
    article_image=article_image,
    megabytes=megabytes,
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
        alert_badges=alert_repo.badges(
            session, profile, [i.story_id for i in page.items if getattr(i, "story_id", None)]
        ),
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
            **_chat_context(request, session, story.story_id, p),
        )
    return TEMPLATES.TemplateResponse(request, "story.html", context)


def _chat_context(request: Request, session: Session, story_id: int, slug: str | None) -> dict:
    """The "Ask a follow-up" panel: availability (``unset``/``unreachable`` disable it with an
    explanation) and the stored suggested questions (never generated here)."""
    hub = getattr(request.app.state, "chat", None)
    state, message = hub.state(slug) if hub is not None else ("unset", "Q&A is off.")
    row = session.scalar(
        select(StorySummary)
        .where(
            StorySummary.story_id == story_id,
            StorySummary.kind == "followups",
            StorySummary.status == "ok",
        )
        .order_by(StorySummary.id.desc())
        .limit(1)
    )
    questions = list((row.content_json or {}).get("questions") or [])[:3] if row else []
    service = hub.for_profile(slug) if hub is not None else None
    return {
        "chat_state": state,
        "chat_message": message,
        "chat_model": service.model if service is not None else None,
        "chat_profile": service.profile if service is not None else None,
        "followups": questions,
    }


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
    from app.sources_web import TYPE_LABELS, _health_rows

    return _base_context(
        session,
        profile,
        on_settings=True,
        topics=sorted(profile.topics, key=lambda t: t.position),
        **_health_rows(session, profile),
        type_labels=TYPE_LABELS,
        auto_disable_failures=request.app.state.settings.source_auto_disable_failures,
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


def x_spend(session: Session) -> list[dict]:
    """Month-to-date spend of every X source against its cap (Pipeline panel)."""
    from app import source_admin

    records = [r for r in source_admin.list_sources(session) if r.type == "x"]
    return [
        {
            "name": h.name,
            "spent": h.spend_this_month_usd,
            "cap": h.monthly_budget_usd,
            "reads": h.reads_this_month,
            "enabled": h.enabled,
        }
        for h in source_admin.source_health(session, records)
    ]


def _pipeline_context(request: Request, session: Session) -> dict:
    worker = getattr(request.app.state, "summaries", None)
    enricher = _enricher(request)
    profile_slug = request.path_params.get("slug")
    record = session.scalar(select(ProfileRecord).where(ProfileRecord.slug == profile_slug))
    return {
        "enrich_on": bool(enricher and record and enricher.profile_enabled(record)),
        "text_counts": text_status_counts(session),
        "runs": last_runs(session),
        "summarizer": worker.status() if worker is not None else None,
        "blockers": blockers(request),
        "stages": preparing(session),
        "queue": pipeline_queue(session, _worker(request)),
        "attempts": recent_attempts(session),
        "quality": quality_metrics(session),
        "alert_stats": alert_repo.stats(session),
        "secret_warnings": missing_secrets(session),
        "x_spend": x_spend(session),
        "image_stats": image_stats(session, _image_cache(request)),
        "image_backfill": _image_backfill(request),
        "enrichment": _enrichment_stats(request, session),
    }


def _enricher(request: Request):
    pipeline = getattr(request.app.state, "pipeline", None)
    return getattr(pipeline, "enricher", None)


def _enrichment_stats(request: Request, session: Session) -> dict | None:
    enricher = _enricher(request)
    if enricher is None:
        return None
    try:
        return enricher.stats(session)
    except Exception:  # the panel never fails on a metrics query
        logger.exception("enrichment stats failed")
        return None


def _image_worker(request: Request) -> ImageWorker | None:
    pipeline = getattr(request.app.state, "pipeline", None)
    return getattr(pipeline, "images", None)


def _image_cache(request: Request):
    worker = _image_worker(request)
    return worker.cache if worker is not None else None


def _image_backfill(request: Request) -> dict | None:
    worker = _image_worker(request)
    if worker is None or worker.last_backfill is None:
        return None
    return worker.last_backfill.model_dump()


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
        context["source_message"] = request.query_params.get("msg")
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


IMMUTABLE = "public, max-age=31536000, immutable"


@router.get("/img/{key}")
def cached_image(request: Request, key: str) -> Response:
    """A validated image from the local cache (WebP, cached for a year by the browser); an
    evicted or uncached one redirects to its original URL. Unknown keys are 404."""
    key = key.removesuffix(".webp")
    worker = _image_worker(request)
    if worker is None or not is_key(key):
        raise HTTPException(404, "no such image")
    row = worker.touch(key)
    if row is None or row.status != "ok":
        raise HTTPException(404, "no such image")
    path = worker.cache.path(key)
    if row.cached_bytes and path.is_file():
        return FileResponse(
            path,
            media_type="image/webp",
            headers={"Cache-Control": IMMUTABLE, "Referrer-Policy": "no-referrer"},
        )
    return RedirectResponse(
        row.url,
        status_code=302,
        headers={"Cache-Control": "public, max-age=3600", "Referrer-Policy": "no-referrer"},
    )


@router.get("/demo-image/{name}")
def demo_image_route(name: str) -> Response:
    """Generated demo images and icons (``app.demo_images``) the demo pages reference."""
    found = demo_image(name)
    if found is None:
        raise HTTPException(404, "no such demo image")
    body, ctype = found
    return Response(body, media_type=ctype, headers={"Cache-Control": IMMUTABLE})


@router.get("/demo-article/{article_id}", response_class=HTMLResponse)
def demo_article(request: Request, article_id: str) -> HTMLResponse:
    """Local demo fixture page (the demo's feed items link here), marked "Demo article"."""
    base = str(request.base_url)
    body = page_html(FIXTURE_DIR, article_id, banner=True, base=base)
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


@router.post("/p/{slug}/settings/pipeline/enrich")
def pipeline_enrich(request: Request, slug: str, story_id: int | None = None) -> Response:
    """Enrich now: every eligible story, or one story regardless of the re-enrich interval."""
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None or pipeline.enricher is None:
        raise HTTPException(status_code=409, detail="enrichment is not configured")
    ids = [story_id] if story_id is not None else None
    pipeline.enrich(ids, force=story_id is not None)
    return _pipeline_response(request, slug)


@router.post("/p/{slug}/settings/pipeline/reenrich-errors")
def pipeline_reenrich_errors(request: Request, slug: str) -> Response:
    """One-time admin action: retry stories whose enrichment ended with provider errors."""
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None or pipeline.enricher is None:
        raise HTTPException(status_code=409, detail="enrichment is not configured")
    pipeline.enricher.requeue_provider_errors()
    return _pipeline_response(request, slug)


@router.post("/p/{slug}/settings/enrichment")
async def settings_enrichment(request: Request, slug: str) -> Response:
    """The per-profile web-search enrichment toggle (overrides the YAML default)."""
    form = await request.form()
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        profile.enrichment_enabled = form.get("enabled") in ("1", "on", "true")
        session.commit()
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
