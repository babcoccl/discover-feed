"""Settings > Workflow: the diagnostic view (Phase 16) — server-rendered Jinja + HTMX
polling, charts as inline SVG. Admin-only via the settings guard (``app.auth.web_guard``).
Read-only: it never touches the pipeline."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app import telemetry
from app.auth import is_admin
from app.telemetry import queries

router = APIRouter(include_in_schema=False)

RANGE_HOURS = {"1h": 1, "24h": 24, "7d": 24 * 7}


def _context(request: Request, session, profile, hours: float, viz_only: bool = False) -> dict:
    from app.web import _base_context

    return _base_context(
        session,
        profile,
        on_settings=True,
        hours=hours,
        viz_only=viz_only,
        enabled=telemetry.enabled(),
        flow=queries.flow_overview(session, hours=hours),
        viz=queries.pipeline_viz(session, hours=hours),
        span=queries.data_span(session, hours=hours),
        llm=queries.llm_stats(session, hours=hours),
        funnel=queries.funnel(session, hours=hours),
        insights=queries.insights(session, hours=hours),
        enrich_worker=_enrich_status(request),
        telemetry={
            "app_version": telemetry.app_version(),
            "config_hash": telemetry.config_hash(),
        },
    )


def _enrich_status(request: Request) -> dict | None:
    from app.api_v1 import enrich_status

    try:
        return enrich_status(request.app)
    except Exception:
        return None


@router.get("/p/{slug}/settings/workflow", response_class=HTMLResponse)
def workflow_page(request: Request, slug: str, hours: int = 24, viz: int = 0) -> Response:
    """Full page: flow overview, LLM usage, backlogs, funnel, insights and versions.
    ``?viz=1`` renders only the pipeline visualizer strip (for a quick glance)."""
    from app.web import TEMPLATES, _profile_or_404, _session

    hours = min(hours or 24, 24 * 30)
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        context = _context(request, session, profile, hours, viz_only=bool(viz))
    return TEMPLATES.TemplateResponse(request, "workflow.html", context)


@router.get("/p/{slug}/settings/workflow/panel", response_class=HTMLResponse)
def workflow_panel(request: Request, slug: str, hours: int = 24, viz: int = 0) -> Response:
    """HTMX partial refreshed by polling."""
    from app.web import TEMPLATES, _profile_or_404, _session

    hours = min(hours or 24, 24 * 30)
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        context = _context(request, session, profile, hours, viz_only=bool(viz))
    return TEMPLATES.TemplateResponse(request, "_workflow.html", context)


@router.post("/p/{slug}/settings/telemetry", response_class=HTMLResponse)
def telemetry_toggle(request: Request, slug: str, hours: int = 24) -> Response:
    """Admin toggle: record telemetry on/off (persisted in ``app_settings``)."""
    from app.web import TEMPLATES, _is_htmx, _profile_or_404, _session

    hours = min(hours or 24, 24 * 30)
    telemetry.set_enabled(not telemetry.enabled())
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        context = _context(request, session, profile, hours)
    if _is_htmx(request):
        return TEMPLATES.TemplateResponse(request, "_workflow.html", context)
    return RedirectResponse(f"/p/{slug}/settings/workflow", status_code=303)


@router.get("/p/{slug}/settings/workflow/story", response_class=HTMLResponse)
def workflow_story_search(request: Request, slug: str, q: str = "", stage: str = "") -> Response:
    """Search a story by id, headline or URL — or list stories waiting in ``stage``."""
    from app.models import Story, SummaryJob
    from app.web import TEMPLATES, _base_context, _profile_or_404, _session

    q = (q or "").strip()
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        context = _base_context(session, profile, on_settings=True)
        context["q"] = q
        context["stage"] = stage
        context["results"] = []
        context["trace"] = None
        if stage in ("brief", "report", "followups"):
            context["results"] = list(
                session.scalars(
                    select(Story)
                    .join(SummaryJob, SummaryJob.story_id == Story.id)
                    .where(SummaryJob.kind == stage)
                    .order_by(SummaryJob.queued_at)
                    .limit(50)
                )
            )
        elif stage == "enrich":
            context["results"] = list(
                session.scalars(
                    select(Story)
                    .where(Story.enrich_status.in_(("pending", "retry")))
                    .order_by(Story.first_seen_at)
                    .limit(50)
                )
            )
        elif stage == "context":
            context["results"] = list(
                session.scalars(
                    select(Story)
                    .where(Story.context_status.in_(("pending", "retry")))
                    .order_by(Story.first_seen_at)
                    .limit(50)
                )
            )
        if q:
            if q.isdigit():
                story = session.get(Story, int(q))
                if story is not None:
                    return RedirectResponse(
                        f"/p/{slug}/settings/workflow/story/{story.id}", status_code=303
                    )
            like = f"%{q}%"
            context["results"] = list(
                session.scalars(
                    select(Story)
                    .where(Story.title.ilike(like))
                    .order_by(Story.last_updated_at.desc())
                    .limit(20)
                )
            )
            if not context["results"]:
                from app.models import Article

                ids = session.scalars(
                    select(Article.story_id).where(Article.url.ilike(like)).limit(20)
                ).all()
                context["results"] = [
                    s for sid in ids if (s := session.get(Story, sid)) is not None
                ]
    return TEMPLATES.TemplateResponse(request, "workflow_story.html", context)


@router.get("/p/{slug}/settings/workflow/story/{story_id}", response_class=HTMLResponse)
def workflow_story(request: Request, slug: str, story_id: int) -> Response:
    """One story's inspector: Gantt timeline, LLM rows, summary bar."""
    from app.web import TEMPLATES, _base_context, _profile_or_404, _session

    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        trace = queries.story_trace(session, story_id)
        if trace["story"] is None:
            return RedirectResponse(f"/p/{slug}/settings/workflow/story", status_code=303)
        context = _base_context(session, profile, on_settings=True)
        context["trace"] = trace
        context["is_admin"] = is_admin(request)
        # Gantt scale: events positioned against the trace's own window
        t0 = trace.get("trace_start") or datetime.now(UTC) - timedelta(hours=1)
        t1 = t0 + timedelta(milliseconds=max(trace["wall_ms"] or 0, 60_000))
        context["gantt"] = (t0, t1, max((t1 - t0).total_seconds() * 1000, 1.0))
    return TEMPLATES.TemplateResponse(request, "workflow_story.html", context)
