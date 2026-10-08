"""Run a clusterer over the DB: incremental by default, or ``rebuild`` everything."""

import logging
import threading
from collections.abc import Sequence
from datetime import timedelta

from pydantic import BaseModel
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.cluster.base import ArticleDoc, Clusterer, StoryDoc, representative
from app.models import Article, Story

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
    session_factory: sessionmaker, clusterer: Clusterer, *, rebuild: bool = False
) -> ClusterRunResult:
    """Assign every article with ``story_id IS NULL``; idempotent (a second run is a no-op)."""
    if not _lock.acquire(blocking=False):
        return ClusterRunResult(status="busy", rebuild=rebuild)
    try:
        with session_factory() as session:
            result = _run(session, clusterer, rebuild=rebuild)
            session.commit()
            return result
    finally:
        _lock.release()


def _run(session: Session, clusterer: Clusterer, *, rebuild: bool) -> ClusterRunResult:
    if rebuild:
        session.execute(update(Article).values(story_id=None))
        session.execute(delete(Story))
        session.flush()

    new = list(session.scalars(select(Article).where(Article.story_id.is_(None))))
    result = ClusterRunResult(rebuild=rebuild, processed=len(new))
    if new:
        stories = _candidate_stories(session, new, clusterer.window)
        assignments = clusterer.assign([doc(a) for a in new], stories)
        by_id = {a.id: a for a in new}
        created: dict[int, Story] = {}
        touched: set[int] = set()
        for assignment in assignments:
            article = by_id[assignment.article_id]
            if assignment.story_id is not None:
                article.story_id = assignment.story_id
                result.joined_existing += 1
            else:
                story = created.get(assignment.new_story)
                if story is None:
                    story = Story(
                        first_seen_at=article.published_at, last_updated_at=article.published_at
                    )
                    session.add(story)
                    session.flush()
                    created[assignment.new_story] = story
                article.story_id = story.id
            touched.add(article.story_id)
            if assignment.score is not None:
                result.joins.append(
                    Joined(
                        article_id=article.id,
                        story_id=article.story_id,
                        score=round(assignment.score, 3),
                    )
                )
        result.new_stories = len(created)
        session.flush()
        refresh_stories(session, touched)

    session.flush()
    result.stories = session.query(Story).count()
    result.multi_source_stories = session.query(Story).filter(Story.source_count >= 2).count()
    return result


def _candidate_stories(
    session: Session, new: Sequence[Article], window: timedelta
) -> list[StoryDoc]:
    earliest = min(a.published_at for a in new) - window
    latest = max(a.published_at for a in new) + window
    stmt = (
        select(Article)
        .join(Story, Article.story_id == Story.id)
        .where(Story.last_updated_at >= earliest, Story.first_seen_at <= latest)
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
        if story is None:
            continue
        if not articles:
            session.delete(story)
            continue
        rep_id = representative([doc(a) for a in articles]).id
        rep = next(a for a in articles if a.id == rep_id)
        story.representative_article_id = rep.id
        story.title = rep.title
        story.first_seen_at = min(a.published_at for a in articles)
        story.last_updated_at = max(a.published_at for a in articles)
        story.article_count = len(articles)
        story.source_count = len({a.source_id for a in articles})


def orphaned_story_ids(session: Session) -> list[int]:
    stmt = select(Story.id).where(
        or_(Story.article_count == 0, ~Story.id.in_(select(Article.story_id).distinct()))
    )
    return list(session.scalars(stmt))
