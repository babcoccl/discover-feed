"""Settings > Pipeline data: the generation queue (state, reason, ETA) and recent attempts.
Read-only queries; imports no summarization code."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GenerationAttempt, Story, StoryState

QUEUE_STATES = (
    StoryState.INGESTED.value,
    StoryState.TEXT_PENDING.value,
    StoryState.TEXT_FINAL.value,
    StoryState.CLUSTERED.value,
    StoryState.BRIEF_READY.value,
    StoryState.REPORT_FINAL.value,
)


@dataclass
class QueueRow:
    story_id: int
    title: str
    state: str
    reason: str | None
    priority: int
    published: bool
    position: int | None
    """Place in the generation order (None: not up for generation yet)."""
    eta: datetime | None


def median_seconds(session: Session) -> dict[str, float | None]:
    seconds: dict[str, float | None] = {}
    for kind in ("brief", "report"):
        values = sorted(
            v
            for v in session.scalars(
                select(GenerationAttempt.latency_ms)
                .where(GenerationAttempt.kind == kind, GenerationAttempt.outcome == "ok")
                .order_by(GenerationAttempt.id.desc())
                .limit(20)
            )
            if v
        )
        seconds[kind] = values[len(values) // 2] / 1000 if values else None
    return seconds


def pipeline_queue(session: Session, worker=None, limit: int = 50) -> list[QueueRow]:
    """Stories not yet (re)published, in generation order, with an ETA from median times."""
    order = worker.candidates(session) if worker is not None else []
    position = {sid: i for i, sid in enumerate(order)}
    medians = median_seconds(session)
    brief_s = medians["brief"] or 0
    report_s = 0 if worker is not None and worker.policy.reports_off else medians["report"] or 0
    stories = {
        s.id: s
        for s in session.scalars(
            select(Story)
            .where(Story.state.in_(QUEUE_STATES))
            .order_by(Story.priority.desc(), Story.last_updated_at.desc(), Story.id.desc())
            .limit(limit * 2)
        )
    }
    for sid in order[:limit]:
        if sid not in stories:
            story = session.get(Story, sid)
            if story is not None:
                stories[sid] = story
    now, elapsed = datetime.now(UTC), 0.0
    eta: dict[int, datetime | None] = {}
    for sid in order:
        story = stories.get(sid) or session.get(Story, sid)
        cost = report_s + (brief_s if story is None or story.state == "clustered" else 0)
        elapsed += cost
        eta[sid] = now + timedelta(seconds=elapsed) if (brief_s or report_s) else None
    rows = [
        QueueRow(
            story_id=s.id,
            title=s.title,
            state=s.state,
            reason=s.state_reason,
            priority=s.priority,
            published=s.published_version is not None,
            position=position.get(s.id),
            eta=eta.get(s.id),
        )
        for s in stories.values()
    ]
    rows.sort(key=lambda r: (r.position is None, r.position or 0))
    return rows[:limit]


def recent_attempts(session: Session, limit: int = 20) -> list[GenerationAttempt]:
    return list(
        session.scalars(
            select(GenerationAttempt).order_by(GenerationAttempt.id.desc()).limit(limit)
        )
    )
