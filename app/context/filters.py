"""Candidate filters for context links ("related, not the same event").

``context_pre_filter`` drops hits before any fetch: same domain as any story member,
social/aggregator/deny-listed, already-ingested, language. ``context_text_check`` runs
after extraction: minimum length, near-duplicates of members or of each other, a shared
entity with the story, and a text similarity *between* ``floor`` and ``ceiling`` — below
the floor the page is off topic, at/above the ceiling it is coverage of the same event
(which is enrichment's job, not context's).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.enrich.domains import blocked_reason, canonical_domain
from app.enrich.filters import (
    StoryContext,
    overlap,
    shared_entities,
    shingles,
    similarity,
)
from app.normalize import canonicalize_url
from app.search.base import SearchHit


def context_pre_filter(
    hit: SearchHit,
    story: StoryContext,
    *,
    deny: Sequence[str],
    language: str | None,
    aliases: dict[str, str] | None = None,
    social: Sequence[str] = (),
    aggregators: Sequence[str] = (),
) -> str | None:
    """Why ``hit`` is rejected before any fetch (None: keep it). Unlike ``pre_filter``,
    no ``too_far_in_time``: background pages are supposed to be older."""
    if not hit.url.startswith(("http://", "https://")):
        return "bad_url"
    blocked = blocked_reason(hit.url, deny, social=social, aggregators=aggregators)
    if blocked:
        return blocked
    if canonical_domain(hit.url, aliases) in story.domains:
        return "same_domain"
    canonical = canonicalize_url(hit.url)
    if canonical in story.canonical_urls:
        return "duplicate_url"
    if language and hit.language and hit.language != language:
        return "language"
    return None


@dataclass
class ContextVerdict:
    """The post-extraction decision: a rejection reason (None: keep), the text
    similarity and the shared-entity count (recorded on the candidate)."""

    reason: str | None
    score: float
    entities: int = 0


def context_text_check(
    title: str,
    text: str,
    story: StoryContext,
    *,
    floor: float,
    ceiling: float,
    min_entities: int,
    duplicate_overlap: float,
    accepted_texts: Sequence[str] = (),
) -> ContextVerdict:
    """Accepted when the page is on-topic-but-not-the-event: ``floor`` <= text
    similarity < ``ceiling``, ``min_entities`` shared names/numbers, and not a copy of
    a member or of an already accepted page (5-word shingle overlap)."""
    mine = shingles(text)
    for other in [*story.texts, *accepted_texts]:
        if overlap(mine, shingles(other)) >= duplicate_overlap:
            return ContextVerdict("near_duplicate", 0.0, 0)
    score = round(similarity(f"{title} {text}", story.document), 4)
    entities = shared_entities(f"{title} {text[:2000]}", story)
    if entities < min_entities:
        return ContextVerdict("few_shared_entities", score, entities)
    if score < floor:
        return ContextVerdict("off_topic", score, entities)
    if score >= ceiling:
        return ContextVerdict("same_event", score, entities)
    return ContextVerdict(None, score, entities)


SAME_EVENT_LEAD = timedelta(hours=12)
"""Coverage can lead the story's first member by a little (timezone/embargo)."""
SAME_EVENT_TITLE = 0.45
"""Title vs. the story headline this similar inside the event window => same event."""
SAME_EVENT_ENTITIES = 4
SAME_EVENT_TEXT = 0.35
"""... or this many shared entities with text this similar (below the ceiling)."""


def same_event_check(
    hit: SearchHit,
    story: StoryContext,
    *,
    published_at: datetime | None,
    entities: int,
    text_similarity: float,
    now: datetime,
) -> str | None:
    """``same_event`` when a *dated* candidate was published inside the story's event
    window — ``[first_at - SAME_EVENT_LEAD, now]`` — and looks like its coverage
    (matching headline, or many shared entities with body similarity). Undated pages
    are never rejected here: backgrounders are often old or undated."""
    if published_at is None or not story.first_at - SAME_EVENT_LEAD <= published_at <= now:
        return None
    if similarity(hit.title, " ".join(story.headlines)) >= SAME_EVENT_TITLE:
        return "same_event"
    if entities >= SAME_EVENT_ENTITIES and text_similarity >= SAME_EVENT_TEXT:
        return "same_event"
    return None
