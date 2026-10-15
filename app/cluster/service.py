"""Run a clusterer over the DB: incremental by default, or ``rebuild`` everything."""

import logging
import threading
from collections.abc import Sequence
from datetime import timedelta

from pydantic import BaseModel
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.cluster.base import ArticleDoc, Clusterer, StoryDoc, representative
from app.events import record
from app.lifecycle import PRE_CLUSTER, advance_text_states, ensure_stories
from app.models import (
    ENRICHMENT,
    Article,
    Story,
    StoryState,
    StorySummary,
    StoryView,
    SummaryJob,
    utcnow,
)

logger = logging.getLogger(__name__)

_lock = threading.Lock()


class Joined(BaseModel):
    article_id: int
    story_id: int
    score: float


class ClusterRunResult(BaseModel):
    status: str = "ok"
    rebuild: bool = False
    processed: int = 0
    joined_existing: int = 0
    new_stories: int = 0
    stories: int = 0
    multi_source_stories: int = 0
    joins: list[Joined] = []
    """Every article that joined a story, with the similarity score that justified it."""
    story_ids: list[int] = []
    """Stories created or given new members by this run (to be re-summarized)."""


def doc(article: Article) -> ArticleDoc:
    return ArticleDoc(
        id=article.id,
        source_id=article.source_id,
        url=article.url,
        title=article.title or "",
        summary=article.summary_raw or "",
        published_at=article.published_at,
        text=article.text if article.text_status == "ok" else None,
        word_count=article.word_count,
    )


def run_clustering(
    session_factory: sessionmaker,
    clusterer: Clusterer,
    *,
    rebuild: bool = False,
    extraction_wait: timedelta = timedelta(0),
) -> ClusterRunResult:
    """Cluster every one-member story whose text is final (``extraction_wait``: how long a
    story waits for pending text first); idempotent (a second run is a no-op)."""
    if not _lock.acquire(blocking=False):
        return ClusterRunResult(status="busy", rebuild=rebuild)
    try:
        with session_factory() as session:
            result = _run(session, clusterer, rebuild=rebuild, wait=extraction_wait)
            session.commit()
            return result
    finally:
        _lock.release()


def _run(
    session: Session, clusterer: Clusterer, *, rebuild: bool, wait: timedelta = timedelta(0)
) -> ClusterRunResult:
    now = utcnow()
    if rebuild:
        session.execute(update(Article).values(story_id=None))  # enrichment re-pinned below
        session.execute(delete(Story))
        session.execute(delete(StoryView))
        # Story ids are reassigned: detach old summaries (still reusable by input hash).
        session.execute(update(StorySummary).values(story_id=None))
        session.execute(delete(SummaryJob))
        session.flush()
        ensure_stories(session, now)
        session.execute(update(Story).values(state=StoryState.TEXT_FINAL.value))
    else:
        ensure_stories(session, now)
    session.flush()
    advance_text_states(session, now, wait)
    session.flush()

    new = list(
        session.scalars(
            select(Article)
            .join(Story, Article.story_id == Story.id)
            .where(Story.state == StoryState.TEXT_FINAL, Article.origin != ENRICHMENT)
            .order_by(Article.id)
        )
    )
    result = ClusterRunResult(rebuild=rebuild, processed=len(new))
    if new:
        stories = _candidate_stories(session, new, clusterer.window)
        assignments = clusterer.assign([doc(a) for a in new], stories)
        by_id = {a.id: a for a in new}
        groups: dict[int, list[Article]] = {}
        touched: set[int] = set()
        for assignment in assignments:
            article = by_id[assignment.article_id]
            if assignment.story_id is not None:
                _merge(session, article.story_id, assignment.story_id)
                article.story_id = assignment.story_id
                result.joined_existing += 1
                touched.add(article.story_id)
            else:
                groups.setdefault(assignment.new_story, []).append(article)
            if assignment.score is not None:
                result.joins.append(
                    Joined(
                        article_id=article.id,
                        story_id=article.story_id,
                        score=round(assignment.score, 3),
                    )
                )
        for group in groups.values():
            keep = min(a.story_id for a in group)  # the oldest story id survives
            for article in group:
                if article.story_id != keep:
                    _merge(session, article.story_id, keep)
                    article.story_id = keep
            story = session.get(Story, keep)
            story.state, story.state_reason = StoryState.CLUSTERED.value, None
            story.clustered_at = now
            touched.add(keep)
        for join in result.joins:  # ids after merging new stories
            join.story_id = by_id[join.article_id].story_id
        for story_id in touched:
            story = session.get(Story, story_id)
            if story.state not in PRE_CLUSTER:
                story.state = StoryState.CLUSTERED.value  # regenerate; published version stays
        result.new_stories = len(groups)
        session.flush()
        refresh_stories(session, touched)
        result.story_ids = sorted(touched)
    if rebuild:
        repinned = _repin_enrichment(session, now)
        refresh_stories(session, repinned)
        result.story_ids = sorted(set(result.story_ids) | repinned)

    session.flush()
    clustered = Story.state.not_in([s.value for s in PRE_CLUSTER] + [StoryState.MERGED.value])
    result.stories = session.query(Story).filter(clustered).count()
    result.multi_source_stories = (
        session.query(Story).filter(clustered, Story.source_count >= 2).count()
    )
    return result


def _merge(session: Session, old_id: int | None, target_id: int) -> None:
    """``old_id`` (a one-member story) joined ``target_id``: old links redirect there."""
    if old_id is None or old_id == target_id:
        return
    old = session.get(Story, old_id)
    if old is None:
        return
    if old.published_version is not None:
        sources = session.scalars(
            select(Article.source_id).where(Article.story_id.in_([old_id, target_id])).distinct()
        )
        record(session, "story.retracted", old_id, old.published_version, list(sources))
    session.execute(  # enrichment members stay with their story
        update(Article)
        .where(Article.story_id == old_id, Article.origin == ENRICHMENT)
        .values(story_id=target_id, discovered_for_story_id=target_id)
    )
    old.state, old.merged_into = StoryState.MERGED.value, target_id
    old.article_count = old.source_count = 0
    old.published_version = None
    session.execute(update(Story).where(Story.merged_into == old_id).values(merged_into=target_id))


def _candidate_stories(
    session: Session, new: Sequence[Article], window: timedelta
) -> list[StoryDoc]:
    earliest = min(a.published_at for a in new) - window
    latest = max(a.published_at for a in new) + window
    stmt = (
        select(Article)
        .join(Story, Article.story_id == Story.id)
        .where(
            Story.last_updated_at >= earliest,
            Story.first_seen_at <= latest,
            Story.state.not_in([s.value for s in PRE_CLUSTER] + [StoryState.MERGED.value]),
            Article.origin != ENRICHMENT,
        )
        .order_by(Article.published_at, Article.id)
    )
    stories: dict[int, StoryDoc] = {}
    for article in session.scalars(stmt):
        stories.setdefault(article.story_id, StoryDoc(id=article.story_id)).members.append(
            doc(article)
        )
    return [stories[k] for k in sorted(stories)]


def refresh_stories(session: Session, story_ids: set[int]) -> None:
    """Recompute title, representative, counts and time span from the members."""
    if not story_ids:
        return
    members: dict[int, list[Article]] = {}
    stmt = select(Article).where(Article.story_id.in_(story_ids))
    for article in session.scalars(stmt):
        members.setdefault(article.story_id, []).append(article)
    for story_id in story_ids:
        story = session.get(Story, story_id)
        articles = members.get(story_id)
        if story is None or story.state == StoryState.MERGED:
            continue
        if not articles:
            session.delete(story)
            continue
        feed = [a for a in articles if a.origin != ENRICHMENT] or articles
        rep_id = representative([doc(a) for a in feed]).id
        rep = next(a for a in articles if a.id == rep_id)
        story.representative_article_id = rep.id
        story.title = rep.title
        story.first_seen_at = min(a.published_at for a in articles)
        story.last_updated_at = max(a.published_at for a in articles)
        story.article_count = len(articles)
        story.source_count = len({a.source_id for a in articles})


def _repin_enrichment(session: Session, now) -> set[int]:
    """After a rebuild: every enrichment member rejoins its anchor's (new) story."""
    session.flush()
    stories: set[int] = set()
    pinned = session.scalars(
        select(Article).where(Article.origin == ENRICHMENT, Article.story_id.is_(None))
    )
    for article in list(pinned):
        anchor = session.get(Article, article.anchor_article_id or 0)
        if anchor is None or anchor.story_id is None:
            continue  # its story is gone: stays detached (never a card of its own)
        article.story_id = article.discovered_for_story_id = anchor.story_id
        stories.add(anchor.story_id)
    for story_id in stories:
        story = session.get(Story, story_id)
        story.enrich_status, story.enriched_at = "done", now
    session.flush()
    return stories


def orphaned_story_ids(session: Session) -> list[int]:
    stmt = select(Story.id).where(
        Story.state != StoryState.MERGED,
        or_(Story.article_count == 0, ~Story.id.in_(select(Article.story_id).distinct())),
    )
    return list(session.scalars(stmt))
