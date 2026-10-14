"""``${ENV}`` secrets referenced by alert channels and API sources but missing from the
environment. Messages name the variable, never a value."""

import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AlertChannelRecord, SourceRecord

logger = logging.getLogger(__name__)


def missing_secrets(session: Session) -> list[str]:
    warnings: list[str] = []
    channels = session.scalars(
        select(AlertChannelRecord)
        .where(AlertChannelRecord.enabled.is_(True))
        .order_by(AlertChannelRecord.id)
    )
    for channel in channels:
        for field, name in sorted((channel.secret_env or {}).items()):
            if name and not os.environ.get(name, "").strip():
                warnings.append(
                    f"Alert channel “{channel.name}” ({channel.type}) {field}: "
                    f"environment variable {name} is not set"
                )
    sources = session.scalars(
        select(SourceRecord)
        .where(SourceRecord.removed_at.is_(None), SourceRecord.api_key_env.is_not(None))
        .order_by(SourceRecord.id)
    )
    for source in sources:
        name = source.api_key_env
        if name and not os.environ.get(name, "").strip():
            warnings.append(
                f"Source “{source.name}” ({source.type}) api_key: "
                f"environment variable {name} is not set"
            )
    return warnings


def log_missing_secrets(session: Session) -> list[str]:
    warnings = missing_secrets(session)
    for warning in warnings:
        logger.warning("%s", warning)
    return warnings
