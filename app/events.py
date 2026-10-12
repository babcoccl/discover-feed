"""In-process pub/sub for live arrivals: ``story.published`` / ``story.updated`` events go to
every open ``/api/profiles/{slug}/stream`` (SSE). Published from any thread, after the
transaction that moved the story's published pointer commits."""

import asyncio
import threading
from dataclasses import dataclass, field

from sqlalchemy import event
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class StoryEvent:
    type: str
    """``story.published`` (first time) or ``story.updated`` (a new version is served)."""
    story_id: int
    version: int
    source_ids: tuple[str, ...] = field(default=(), compare=False)

    def payload(self) -> dict:
        return {"story_id": self.story_id, "version": self.version}


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
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
    if not queue.full():
        queue.put_nowait(item)


bus = EventBus()


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
