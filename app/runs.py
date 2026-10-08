"""Bookkeeping for the last run of each pipeline step (shown on the settings page)."""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Article, PipelineRun, utcnow

STEPS = ("ingest", "extract", "cluster")


def record_run(
    session_factory: sessionmaker, name: str, result: dict[str, Any], at: datetime | None = None
) -> None:
    with session_factory() as session:
        run = session.get(PipelineRun, name) or PipelineRun(name=name)
        run.last_run_at = at or utcnow()
        run.last_result = result
        session.add(run)
        session.commit()


def last_runs(session: Session) -> dict[str, PipelineRun]:
    return {run.name: run for run in session.scalars(select(PipelineRun))}


def text_status_counts(session: Session) -> dict[str, int]:
    counts = dict.fromkeys(("pending", "ok", "failed", "skipped"), 0)
    stmt = select(Article.text_status, func.count()).group_by(Article.text_status)
    counts.update({status: n for status, n in session.execute(stmt)})
    return counts
