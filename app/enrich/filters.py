"""Relevance and duplicate checks for search hits (before and after extraction)."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sklearn.feature_extraction.text import TfidfVectorizer

from app.cluster.tokens import significant_tokens, words
from app.enrich.domains import blocked_reason, registrable_domain
from app.normalize import canonicalize_url
from app.search.base import SearchHit

SHINGLE = 5


@dataclass
class StoryContext:
    """What the filters compare a hit against: the story's current members."""

    title: str
    headlines: list[str]
    texts: list[str]
    """Member body text (or feed summary) - internal only, never sent to a search API."""
    domains: set[str]
    canonical_urls: set[str]
    first_at: datetime
    last_at: datetime
    language: str | None = None

    @property
    def document(self) -> str:
        return " ".join([*self.headlines, *self.texts])

    @property
    def tokens(self) -> set[str]:
        return significant_tokens(" ".join(self.headlines))


def similarity(a: str, b: str) -> float:
    """TF-IDF cosine of two texts (fitted on just the pair, like the clusterer's tokens)."""
    if not words(a) or not words(b):
        return 0.0
    vectorizer = TfidfVectorizer(analyzer=words, sublinear_tf=True)
    matrix = vectorizer.fit_transform([a, b])
    return float((matrix[0] @ matrix[1].T).toarray()[0][0])


def pre_filter(
    hit: SearchHit,
    story: StoryContext,
    *,
    deny: Sequence[str],
    max_gap: timedelta,
    language: str | None,
) -> str | None:
    """Why ``hit`` is rejected before any fetch (None: keep it)."""
    if not hit.url.startswith(("http://", "https://")):
        return "bad_url"
    blocked = blocked_reason(hit.url, deny)
    if blocked:
        return blocked
    if registrable_domain(hit.url) in story.domains:
        return "same_domain"
    canonical = canonicalize_url(hit.url)
    if canonical in story.canonical_urls:
        return "duplicate_url"
    if hit.published_at is not None and (
        hit.published_at < story.first_at - max_gap or hit.published_at > story.last_at + max_gap
    ):
        return "too_far_in_time"
    if language and hit.language and hit.language != language:
        return "language"
    return None


def snippet_check(
    hit: SearchHit, story: StoryContext, threshold: float
) -> tuple[str | None, float]:
    score = similarity(f"{hit.title} {hit.snippet}", " ".join(story.headlines))
    return ("low_similarity" if score < threshold else None), round(score, 4)


def title_check(
    hit: SearchHit, story: StoryContext, threshold: float, min_entities: int = 1
) -> tuple[str | None, float, int]:
    """Hits without a snippet (GDELT returns titles only): the title must share an entity
    with the story and be similar enough to its headlines. The text check after extraction
    has the final say. Returns ``(reason, title_similarity, shared_entities)``."""
    score = round(similarity(hit.title, " ".join(story.headlines)), 4)
    entities = shared_entities(hit.title, story)
    if entities < min_entities:
        return "no_shared_entity", score, entities
    if score < threshold:
        return "low_title_similarity", score, entities
    return None, score, entities


def shared_entities(text: str, story: StoryContext) -> int:
    return len(significant_tokens(text) & story.tokens)


def shingles(text: str) -> set[tuple[str, ...]]:
    tokens = re.findall(r"\w+", (text or "").lower())
    if len(tokens) < SHINGLE:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i : i + SHINGLE]) for i in range(len(tokens) - SHINGLE + 1)}


def overlap(a: set, b: set) -> float:
    """Share of the smaller text's shingles found in the other (wire copies score ~1)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def text_check(
    title: str,
    text: str,
    story: StoryContext,
    *,
    threshold: float,
    min_entities: int,
    duplicate_overlap: float,
    accepted_texts: Sequence[str] = (),
) -> tuple[str | None, float]:
    """Post-extraction checks: body similarity, shared entities, not a copy of a member or of
    an already accepted candidate."""
    score = round(similarity(f"{title} {text}", story.document), 4)
    if score < threshold:
        return "low_text_similarity", score
    if shared_entities(f"{title} {text[:2000]}", story) < min_entities:
        return "few_shared_entities", score
    mine = shingles(text)
    for other in [*story.texts, *accepted_texts]:
        if overlap(mine, shingles(other)) >= duplicate_overlap:
            return "near_duplicate", score
    return None, score
