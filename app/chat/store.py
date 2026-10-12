"""Conversations and messages (no accounts: a conversation is addressed by its id)."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import ChatMessage, Conversation, utcnow


def new_conversation(session: Session, story_id: int, now: datetime | None = None) -> Conversation:
    now = now or utcnow()
    conv = Conversation(id=str(uuid.uuid4()), story_id=story_id, created_at=now, last_active_at=now)
    session.add(conv)
    return conv


def messages(session: Session, conversation_id: str) -> list[ChatMessage]:
    return list(
        session.scalars(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.id)
        )
    )


def history(session: Session, conversation_id: str, *, before_id: int | None = None):
    """``(question, answer)`` pairs with a non-empty answer, oldest first."""
    pairs: list[tuple[str, str]] = []
    question: str | None = None
    for m in messages(session, conversation_id):
        if before_id is not None and m.id >= before_id:
            break
        if m.role == "user":
            question = m.content
        elif question is not None and m.content.strip() and m.status != "streaming":
            pairs.append((question, m.content))
            question = None
    return pairs


def delete_conversation(session: Session, conversation_id: str) -> bool:
    conv = session.get(Conversation, conversation_id)
    if conv is None:
        return False
    session.execute(delete(ChatMessage).where(ChatMessage.conversation_id == conversation_id))
    session.delete(conv)
    session.commit()
    return True


def cleanup(
    session_factory: sessionmaker, retention_days: float, now: datetime | None = None
) -> int:
    """Delete conversations idle for ``retention_days`` (and their messages)."""
    cutoff = (now or utcnow()) - timedelta(days=retention_days)
    with session_factory() as session:
        ids = list(
            session.scalars(select(Conversation.id).where(Conversation.last_active_at < cutoff))
        )
        if ids:
            session.execute(delete(ChatMessage).where(ChatMessage.conversation_id.in_(ids)))
            session.execute(delete(Conversation).where(Conversation.id.in_(ids)))
            session.commit()
    return len(ids)
