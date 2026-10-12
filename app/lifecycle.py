"""Story states before clustering: every article gets a one-member story at ingest
(``ingested``), which waits for text extraction (``text_pending``) until the text is in or
``extraction_wait_seconds`` pass (``text_final``); clustering then merges it into an existing
story or keeps it (``clustered``). See ``app.publish`` for the rest of the lifecycle."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Article, Story, StoryState, TextStatus

PRE_CLUSTER = (StoryState.INGESTED, StoryState.TEXT_PENDING, StoryState.TEXT_FINAL)


def ensure_stories(session: Session, now: datetime) -> int:
    """A one-member ``ingested`` story for every article without a story."""
    articles = list(session.scalars(select(Article).where(Article.story_id.is_(None))))
    for article in articles:
        story = Story(
            title=article.title,
            representative_article_id=article.id,
            first_seen_at=article.published_at,
            last_updated_at=article.published_at,
            article_count=1,
            source_count=1,
            state=StoryState.INGESTED.value,
            ingested_at=now,
        )
        session.add(story)
        session.flush()
        article.story_id = story.id
    return len(articles)


def advance_text_states(session: Session, now: datetime, wait: timedelta) -> int:
    """``ingested``/``text_pending`` -> ``text_final`` once no member's text is pending, or
    after ``wait``. Returns how many stories became ``text_final``."""
    waiting = (StoryState.INGESTED.value, StoryState.TEXT_PENDING.value)
    rows = session.execute(
        select(Story, Article)
        .join(Article, Article.story_id == Story.id)
        .where(Story.state.in_(waiting))
    ).all()
    pending: dict[int, bool] = {}
    stories: dict[int, Story] = {}
    for story, article in rows:
        stories[story.id] = story
        pending[story.id] = pending.get(story.id, False) or (
            article.text_status == TextStatus.PENDING
        )
    final = 0
    for story_id, story in stories.items():
        started = story.ingested_at or now
        if not pending[story_id]:
            story.state, story.state_reason = StoryState.TEXT_FINAL.value, None
        elif now - started >= wait:
            story.state = StoryState.TEXT_FINAL.value
            story.state_reason = "extraction_wait: text still pending"
        else:
            story.state = StoryState.TEXT_PENDING.value
            continue
        final += 1
    return final
