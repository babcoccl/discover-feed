"""Pluggable clusterer interface: ``assign(new_articles, existing_stories) -> assignments``."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ArticleDoc:
    id: int
    source_id: str
    url: str
    title: str
    summary: str
    published_at: datetime
    text: str | None = None
    """Extracted body (only when extraction succeeded)."""
    word_count: int | None = None

    @property
    def length(self) -> int:
        """Text length used to pick a story's representative (falls back to the feed summary)."""
        if self.text:
            return self.word_count or len(self.text.split())
        return len(self.summary.split())


@dataclass
class StoryDoc:
    id: int
    members: list[ArticleDoc] = field(default_factory=list)


@dataclass(frozen=True)
class Assignment:
    """Put ``article_id`` in existing story ``story_id``, or in new story number ``new_story``
    (0-based, numbered in creation order within this call). ``score`` is the similarity that
    justified joining (None for the article that started a story)."""

    article_id: int
    story_id: int | None = None
    new_story: int | None = None
    score: float | None = None


def representative(members: Sequence[ArticleDoc]) -> ArticleDoc:
    """Earliest article from the source with the longest text."""
    longest = max(members, key=lambda m: (m.length, -m.published_at.timestamp(), -m.id))
    same_source = [m for m in members if m.source_id == longest.source_id]
    return min(same_source, key=lambda m: (m.published_at, m.id))


class Clusterer(ABC):
    window: timedelta
    """Only stories whose time span is within this distance of an article are candidates."""

    @abstractmethod
    def assign(
        self, new_articles: Sequence[ArticleDoc], existing_stories: Sequence[StoryDoc]
    ) -> list[Assignment]:
        """Assign every new article to an existing story or a new one."""
