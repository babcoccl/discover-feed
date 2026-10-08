"""Helpers for extraction/clustering tests: insert articles directly into a test DB."""

import hashlib
from datetime import UTC, datetime, timedelta

from app.models import Article

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def add_article(
    session_factory,
    *,
    url: str,
    title: str = "Title",
    summary: str = "",
    source_id: str = "src",
    hours: float = 0,
    text: str | None = None,
) -> int:
    with session_factory() as session:
        article = Article(
            source_id=source_id,
            url=url,
            canonical_url=url,
            title=title,
            summary_raw=summary,
            published_at=T0 + timedelta(hours=hours),
            fetched_at=T0,
            content_hash=hashlib.sha256(f"{source_id}{url}".encode()).hexdigest(),
            raw_json={},
            text=text,
            text_status="ok" if text else "pending",
            word_count=len(text.split()) if text else None,
        )
        session.add(article)
        session.commit()
        return article.id
