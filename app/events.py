"""Story events: ``story.published`` / ``story.updated`` / ``story.retracted``.

:func:`record` stores each event in ``story_events`` (ids for SSE resume, kept
``event_retention_days``) inside the transaction that changed the story, and the in-process
bus hands it to every open stream once that transaction commits. Published from any thread;
a slow stream never blocks the publisher (its queue overflows and it replays from the
table instead)."""

import asyncio
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session

from app.models import ProfileRecord, SourceRecord, StoryEventRow, profile_sources, utcnow

EVENT_TYPES = ("story.published", "story.updated", "story.retracted")
DEFAULT_RETENTION = timedelta(days=7)


@dataclass(frozen=True)
class StoryEvent:
    type: str
    """``story.published`` (first time), ``story.updated`` (a new version is served) or
    ``story.retracted`` (no longer served, e.g. merged into another story)."""
    story_id: int
    version: int | None
    source_ids: tuple[str, ...] = field(default=(), compare=False)
    id: int = field(default=0, compare=False)
    """The ``story_events`` row id (0: not stored)."""
    profiles: tuple[str, ...] = field(default=(), compare=False)

    def payload(self) -> dict:
        return {"story_id": self.story_id, "version": self.version}

    @classmethod
    def from_row(cls, row: StoryEventRow) -> "StoryEvent":
        return cls(row.type, row.story_id, row.version, (), row.id, tuple(row.profiles or ()))


class Subscription(asyncio.Queue):
    """A stream's queue; ``overflowed`` is set when events were dropped because it was full."""

    overflowed: bool = False


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    def subscribe(self, maxsize: int = 1000) -> Subscription:
        queue = Subscription(maxsize=maxsize)
        with self._lock:
            self._subscribers.append((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s[1] is not queue]

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def publish(self, item: StoryEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(_put, queue, item)
            except RuntimeError:  # loop closed: the stream went away
                self.unsubscribe(queue)


def _put(queue: asyncio.Queue, item: StoryEvent) -> None:
    if queue.full():
        queue.overflowed = True
    else:
        queue.put_nowait(item)


bus = EventBus()


def profiles_for(session: Session, source_ids: Sequence[str]) -> list[str]:
    """Slugs of the profiles that follow any of ``source_ids``."""
    if not source_ids:
        return []
    stmt = (
        select(ProfileRecord.slug)
        .join(profile_sources, profile_sources.c.profile_id == ProfileRecord.id)
        .join(SourceRecord, SourceRecord.id == profile_sources.c.source_id)
        .where(SourceRecord.id.in_(list(source_ids)))
        .distinct()
        .order_by(ProfileRecord.slug)
    )
    return list(session.scalars(stmt))


def record(
    session: Session,
    type_: str,
    story_id: int,
    version: int | None,
    source_ids: Sequence[str],
    *,
    retention: timedelta = DEFAULT_RETENTION,
) -> StoryEvent:
    """Store an event and deliver it to live streams after ``session`` commits; prunes
    events older than ``retention``."""
    row = StoryEventRow(
        type=type_,
        story_id=story_id,
        version=version,
        profiles=profiles_for(session, source_ids),
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    session.execute(delete(StoryEventRow).where(StoryEventRow.created_at < utcnow() - retention))
    item = StoryEvent(type_, story_id, version, tuple(source_ids), row.id, tuple(row.profiles))
    defer(session, item)
    return item


def latest_event_id(session: Session) -> int:
    return session.scalar(select(func.max(StoryEventRow.id))) or 0


def events_after(
    session: Session, after_id: int, *, profile: str | None = None, limit: int = 1000
) -> list[StoryEvent]:
    """Stored events with ``id > after_id``, oldest first (``profile``: only its stories)."""
    rows = session.scalars(
        select(StoryEventRow)
        .where(StoryEventRow.id > after_id)
        .order_by(StoryEventRow.id)
        .limit(limit)
    )
    found = [StoryEvent.from_row(r) for r in rows]
    return [e for e in found if profile is None or profile in e.profiles]


def defer(session: Session, item: StoryEvent) -> None:
    """Publish ``item`` once ``session`` commits (dropped on rollback)."""
    session.info.setdefault("story_events", []).append(item)


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    for item in session.info.pop("story_events", []):
        bus.publish(item)


@event.listens_for(Session, "after_soft_rollback")
def _after_rollback(session: Session, previous_transaction=None) -> None:
    session.info.pop("story_events", None)
