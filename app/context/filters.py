"""Candidate filters for context links ("related, not the same event").

``context_pre_filter`` drops hits before any fetch: same domain as any story member,
social/aggregator/deny-listed, already-ingested, language. ``context_text_check`` runs
after extraction: minimum length, near-duplicates of members or of each other, a shared
entity with the story, and a text similarity *between* ``floor`` and ``ceiling`` — below
the floor the page is off topic, at/above the ceiling it is coverage of the same event
(which is enrichment's job, not context's). ``same_event_signals`` adds deterministic
same-event rules before the (expensive, inconsistent) LLM judge: shared specific
numbers, a stricter similarity band for contemporaneous pages, and a headline
entities + event-word check.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.cluster.tokens import normalize, words
from app.context.rules import SameEventRules
from app.enrich.domains import blocked_reason, canonical_domain
from app.enrich.filters import (
    StoryContext,
    overlap,
    shared_entities,
    shingles,
    similarity,
)
from app.normalize import canonicalize_url

if TYPE_CHECKING:
    # runtime import would cycle: app.config -> this module -> app.search.base -> app.config
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


_NUMBER = re.compile(r"\d[\d,.]*\d|\d")
_YEAR = re.compile(r"(19|20)\d{2}")


def _numbers(text: str) -> set[str]:
    """Distinct numeric tokens in ``text``, canonicalized (commas/periods stripped):
    "1,068" -> "1068", "$6.2 billion" -> "6.2". Single digits and 4-digit years
    (19xx/20xx) are excluded — they are everywhere and carry no event identity."""
    out = set()
    for raw in _NUMBER.findall(text or ""):
        token = raw.replace(",", "").rstrip(".")
        if len(token) == 1 or _YEAR.fullmatch(token):
            continue
        out.add(token)
    return out


def _event_stem(token: str) -> str:
    """Crude suffix stem so inflections of one event word meet ("unveils"/
    "unveiled" -> "unveil", "raises"/"raised" -> "rais", "approves"/
    "approved" -> "approv")."""
    w = normalize(token)
    for suffix in ("ing", "ies", "ed", "es", "s", "d", "e"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    return w


def same_event_signals(
    hit: SearchHit,
    story: StoryContext,
    *,
    brief: str,
    text: str,
    published_at: datetime | None,
    entities: int,
    text_similarity: float,
    rules: SameEventRules,
) -> str | None:
    """Which deterministic signal marks this page as coverage of the same event:
    ``"numbers"`` / ``"date_band"`` / ``"entities_verb"`` — None when none fires.
    Each signal alone is enough to reject; the returned tag is recorded for the
    evaluate/diagnose breakdown."""
    story_numbers = _numbers(f"{story.title} {brief}")
    if story_numbers:
        shared = story_numbers & _numbers(f"{hit.title} {text[: rules.numbers_text_chars]}")
        if len(shared) >= rules.numbers_min_shared:
            return "numbers"
    contemporaneous = (
        published_at is not None
        and abs(published_at - story.first_at) <= timedelta(hours=rules.window_hours)
    ) or (published_at is None and entities >= rules.undated_min_entities)
    if contemporaneous:
        title_similarity = similarity(hit.title, " ".join(story.headlines))
        if (
            title_similarity >= rules.max_title_similarity
            or text_similarity >= rules.max_text_similarity
        ):
            return "date_band"
    if shared_entities(hit.title, story) >= rules.entities_min:
        stems = {_event_stem(t) for t in rules.event_terms}
        headline_events = {_event_stem(w) for w in words(" ".join(story.headlines))} & stems
        title_events = {_event_stem(w) for w in words(hit.title)} & stems
        if headline_events & title_events:
            return "entities_verb"
    return None
