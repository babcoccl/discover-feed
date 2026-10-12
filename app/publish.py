"""Write side of the publish gate.

After every brief/report change (in the same transaction) and on every worker sweep,
``sync_story`` derives the story's state (``clustered`` -> ``brief_ready`` ->
``report_final`` -> ``published``), writes a new ``story_views`` version when the document
changed, and moves ``stories.published_version`` to it only when it satisfies the publish
rule. The previously published version keeps being served until then.

Publish rule (per profile ``summaries:``): a valid brief and a final report (ready, skipped
or failed); or a valid brief after ``publish_max_wait_minutes`` (report still pending); or,
when the brief failed, the feed snippet after ``brief_fallback_hours``.
"""

import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import profiles as repo
from app.config import ReportMode, SummariesConfig
from app.events import StoryEvent, defer
from app.lifecycle import PRE_CLUSTER, ensure_stories
from app.models import (
    Story,
    StoryState,
    StorySummary,
    StoryView,
    SummaryJob,
    SummaryKind,
    SummaryStatus,
    utcnow,
)
from app.story_views import SCHEMA_VERSION
from app.summarize.service import Summarizer, report_eligibility
from app.summarize.views import _style, build_report, build_view, latest_ok, latest_reports
from app.web import snippet

logger = logging.getLogger(__name__)

REPORT_PENDING_LABEL = "Detailed report still generating"
REPORTS_OFF = "reports_off"


@dataclass(frozen=True)
class Policy:
    requires_report: bool = True
    max_wait: timedelta = timedelta(minutes=30)
    brief_fallback: timedelta = timedelta(hours=6)
    report_mode: ReportMode = ReportMode.AUTO
    extraction_wait: timedelta = timedelta(seconds=120)

    @classmethod
    def from_config(cls, options: SummariesConfig, report_mode: str | None = None) -> "Policy":
        return cls(
            requires_report=options.publish_requires_report,
            max_wait=timedelta(minutes=options.publish_max_wait_minutes),
            brief_fallback=timedelta(hours=options.brief_fallback_hours),
            report_mode=ReportMode(report_mode or options.report_mode),
            extraction_wait=timedelta(seconds=options.extraction_wait_seconds),
        )

    def __post_init__(self) -> None:
        if self.report_mode is ReportMode.ON_DEMAND:  # deprecated: pages never generate
            object.__setattr__(self, "report_mode", ReportMode.AUTO)

    @property
    def reports_off(self) -> bool:
        return self.report_mode is ReportMode.OFF


@dataclass
class Assessment:
    brief: str
    """``valid`` | ``pending`` | ``failed``."""
    brief_reason: str | None
    report: str
    """``ready`` | ``pending`` | ``skipped`` | ``failed``."""
    report_reason: str | None
    brief_row: StorySummary | None
    report_row: StorySummary | None


def _current(session: Session, story_id: int, kind: str, key: str) -> StorySummary | None:
    return session.scalar(
        select(StorySummary)
        .where(
            StorySummary.story_id == story_id,
            StorySummary.kind == kind,
            StorySummary.input_hash == key,
        )
        .order_by(StorySummary.id.desc())
        .limit(1)
    )


def assess(
    session: Session,
    story_id: int,
    item: repo.StoryItem,
    names: dict[str, str],
    summarizer: Summarizer | None,
    policy: Policy,
) -> Assessment:
    """Where the story's brief and report stand for its current members. Without a
    summarizer (no endpoint configured) the newest stored rows count."""
    if summarizer is None:
        brief_row = latest_ok(session, [story_id]).get(story_id)
        report_row = latest_reports(session, [story_id]).get(story_id)
    else:
        rows = {}
        for kind in (SummaryKind.BRIEF, SummaryKind.REPORT):
            sources = summarizer.sources_for(item, names, kind)
            rows[kind] = _current(session, story_id, kind, summarizer.key_for(item, sources, kind))
        brief_row, report_row = rows[SummaryKind.BRIEF], rows[SummaryKind.REPORT]

    if brief_row is None:
        brief, brief_reason = "pending", None
    elif brief_row.status == SummaryStatus.OK or (
        brief_row.status == SummaryStatus.FALLBACK and _style(brief_row) == "summary"
    ):
        brief, brief_reason = "valid", None
    else:
        brief, brief_reason = "failed", brief_row.reason or brief_row.status

    report_job = session.get(SummaryJob, (story_id, SummaryKind.REPORT.value))
    if policy.reports_off:
        report, report_reason = "skipped", REPORTS_OFF
    elif report_row is None:
        report, report_reason = "pending", None
    elif report_row.status == SummaryStatus.OK:
        report, report_reason = "ready", None
    elif report_row.status == SummaryStatus.SKIPPED:
        report, report_reason = "skipped", report_row.reason
    elif report_job is not None:  # failed, retry scheduled
        report, report_reason = "pending", report_row.reason
    else:
        report, report_reason = "failed", report_row.reason
    return Assessment(brief, brief_reason, report, report_reason, brief_row, report_row)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _members(item: repo.StoryItem, names: dict[str, str]) -> list[dict]:
    return [
        {
            "id": m.id,
            "source_id": m.source_id,
            "source": names.get(m.source_id, m.source_id),
            "title": m.title,
            "url": m.url,
            "published_at": _iso(m.published_at),
            "image_url": m.image_url,
            "summary": m.summary_raw,
            "text_status": m.text_status,
            "words": m.word_count or 0,
        }
        for m in item.members
    ]


def _point(point) -> dict:
    return {"text": point.text, "citations": [c.index for c in point.citations]}


def _citations(citations) -> list[dict]:
    return [dataclasses.asdict(c) for c in citations]


def build_doc(
    session: Session,
    story_id: int,
    item: repo.StoryItem,
    names: dict[str, str],
    a: Assessment,
    *,
    fallback: bool,
) -> dict:
    """Everything the card and the story page render, as JSON."""
    articles = {m.id: m for m in item.members}
    brief = None
    if a.brief == "valid":
        view = build_view(a.brief_row, articles, names)
        if view is not None:
            brief = {
                "style": view.style,
                "lead": [_point(p) for p in view.lead],
                "bullets": [_point(p) for p in view.bullets],
                "citations": _citations(view.citations),
                "basis": view.basis,
                "model": view.model,
                "generated_at": _iso(view.generated_at),
                "status": view.status,
            }
    elif fallback:
        brief = {
            "style": "snippet",
            "lead": [{"text": snippet(item.snippet_source) or item.title, "citations": []}],
            "bullets": [],
            "citations": [],
            "basis": "feed_summary",
            "model": a.brief_row.model if a.brief_row else "",
            "generated_at": _iso(a.brief_row.created_at if a.brief_row else None),
            "status": "fallback",
            "label": f"Summary unavailable: {a.brief_reason or 'no valid brief'}",
        }
    report: dict = {"state": a.report, "reason": a.report_reason}
    if a.report == "ready" and a.report_row is not None:
        view = build_report(a.report_row, articles, names)
        if view is not None:
            report.update(
                paragraphs=[_point(p) for p in view.paragraphs],
                citations=_citations(view.citations),
                model=view.model,
                generated_at=_iso(view.generated_at),
            )
    elif a.report == "pending":
        report["label"] = REPORT_PENDING_LABEL
    elif a.report == "skipped" and a.report_reason and a.report_reason != REPORTS_OFF:
        report["details"] = [
            f"{names.get(m.source_id, m.source_id)}: {m.text_status}, {m.word_count or 0} words"
            for m in item.members
        ]
    rows = [r for r in (a.brief_row, a.report_row) if r is not None]
    generated = max((r.created_at for r in rows), default=None)
    return {
        "schema_version": SCHEMA_VERSION,
        "story_id": story_id,
        "title": item.title,
        "representative_id": item.representative.id,
        "image_url": item.image_url,
        "first_seen_at": _iso(item.first_seen_at),
        "last_updated_at": _iso(item.last_updated_at),
        "article_count": len(item.members),
        "source_count": item.source_count,
        "members": _members(item, names),
        "brief": brief,
        "report": report,
        "model": (a.brief_row.model if a.brief_row else "") or "",
        "generated_at": _iso(generated),
    }


def decide(story: Story, a: Assessment, policy: Policy, now: datetime) -> tuple[str, bool]:
    """``(view state, publishable)``."""
    final = a.report in ("ready", "skipped", "failed")
    if a.brief == "valid":
        if final:
            return "complete", True
        ready_at = story.brief_ready_at or now
        waited = now - ready_at >= policy.max_wait
        return "report_pending", (not policy.requires_report) or waited
    if a.brief == "failed":
        since = story.clustered_at or story.first_seen_at
        if now - since >= policy.brief_fallback:
            return "brief_fallback", True
    return "draft", False


def sync_story(
    session: Session,
    story_id: int,
    *,
    summarizer: Summarizer | None,
    policy: Policy,
    now: datetime | None = None,
) -> StoryView | None:
    """Derive state, write a view version if it changed, publish it if the rule allows.
    Call inside the transaction that changed the story's brief/report; does not commit."""
    now = now or utcnow()
    story = session.get(Story, story_id)
    if story is None or story.state in PRE_CLUSTER or story.state == StoryState.MERGED:
        return None
    item = repo.get_story(session, story_id)
    if item is None:
        return None
    names = repo.source_names(session)
    a = assess(session, story_id, item, names, summarizer, policy)
    if a.brief == "valid":
        if story.brief_ready_at is None:
            story.brief_ready_at = min(a.brief_row.created_at, now)
    else:
        story.brief_ready_at = None
    view_state, publishable = decide(story, a, policy, now)
    doc = build_doc(session, story_id, item, names, a, fallback=view_state == "brief_fallback")
    latest = session.scalar(
        select(StoryView)
        .where(StoryView.story_id == story_id)
        .order_by(StoryView.version.desc())
        .limit(1)
    )
    if latest is None or latest.doc != doc or latest.state != view_state:
        latest = StoryView(
            story_id=story_id,
            version=(latest.version + 1) if latest else 1,
            schema_version=SCHEMA_VERSION,
            state=view_state,
            doc=doc,
            updated_at=now,
        )
        session.add(latest)
        session.flush()
    if publishable and story.published_version != latest.version:
        first = story.published_version is None
        story.published_version = latest.version
        story.published_at = story.published_at or now
        story.published_sort_at = item.last_updated_at
        latest.published_at = now
        defer(
            session,
            StoryEvent(
                "story.published" if first else "story.updated",
                story_id,
                latest.version,
                tuple(item.source_ids),
            ),
        )
    if story.published_version == latest.version:
        story.state = StoryState.PUBLISHED.value
    elif a.brief == "valid":
        final = a.report in ("ready", "skipped", "failed")
        story.state = (StoryState.REPORT_FINAL if final else StoryState.BRIEF_READY).value
    else:
        story.state = StoryState.CLUSTERED.value
    story.state_reason = _reason(a, view_state, publishable, story, policy)
    return latest


def _reason(a: Assessment, view_state: str, publishable: bool, story: Story, policy) -> str | None:
    if publishable:
        if view_state == "report_pending":
            return "report pending" + (f" ({a.report_reason})" if a.report_reason else "")
        if view_state == "brief_fallback":
            return f"brief failed: {a.brief_reason}"
        return None
    if a.brief == "failed":
        return f"brief failed: {a.brief_reason}"
    if a.brief == "valid":
        return "waiting for report" + (f" ({a.report_reason})" if a.report_reason else "")
    return None


def migrate(
    session_factory: sessionmaker,
    *,
    summarizer: Summarizer | None,
    policy: Policy,
    now: datetime | None = None,
) -> int:
    """Startup, idempotent: one-member stories for unclustered articles, and a published
    view for every pre-publish-gate story with stored summaries (built from its newest
    rows; the worker regenerates stale ones while that version is served)."""
    now = now or utcnow()
    with session_factory() as session:
        ensure_stories(session, now)
        ids = list(
            session.scalars(
                select(Story.id).where(
                    Story.published_version.is_(None),
                    Story.state.not_in([s.value for s in PRE_CLUSTER] + ["merged"]),
                    Story.id.in_(select(StorySummary.story_id).where(StorySummary.story_id > 0)),
                )
            )
        )
        published = 0
        for story_id in ids:
            story = session.get(Story, story_id)
            story.clustered_at = story.clustered_at or story.first_seen_at
            view = sync_story(session, story_id, summarizer=None, policy=policy, now=now)
            published += int(view is not None and story.published_version == view.version)
        session.commit()
    if ids:
        logger.info("publish gate: migrated %d stories, %d published", len(ids), published)
    return published


__all__ = ["Policy", "assess", "build_doc", "decide", "migrate", "report_eligibility", "sync_story"]
