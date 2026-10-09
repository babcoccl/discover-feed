from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    connect_args: dict = {}
    if url.get_backend_name() == "sqlite":
        connect_args["check_same_thread"] = False
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(database_url, connect_args=connect_args)


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    from app import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(engine)
    _add_article_url(engine)
    _add_article_pipeline_columns(engine)
    _add_summary_kind(engine)


def _add_article_url(engine: Engine) -> None:
    """Pre-`url` DBs: add the column, backfilled from the stored feed entry's link, else from
    canonical_url."""
    with engine.begin() as conn:
        if "url" in {c["name"] for c in inspect(conn).get_columns("articles")}:
            return
        conn.execute(text("ALTER TABLE articles ADD COLUMN url VARCHAR(2048) NOT NULL DEFAULT ''"))
        conn.execute(
            text(
                """
                UPDATE articles SET url = CASE
                    WHEN json_valid(raw_json)
                         AND lower(trim(json_extract(raw_json, '$.link'))) LIKE 'http%'
                    THEN trim(json_extract(raw_json, '$.link'))
                    ELSE canonical_url
                END
                """
            )
        )


_PIPELINE_COLUMNS = {
    "text": "TEXT",
    "text_status": "VARCHAR(10) NOT NULL DEFAULT 'pending'",
    "text_fetched_at": "DATETIME",
    "word_count": "INTEGER",
    "story_id": "INTEGER REFERENCES stories(id) ON DELETE SET NULL",
}


def _add_article_pipeline_columns(engine: Engine) -> None:
    """Pre-extraction DBs: add the text/story columns (existing rows start as `pending`)."""
    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(conn).get_columns("articles")}
        for name, ddl in _PIPELINE_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE articles ADD COLUMN {name} {ddl}"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_articles_text_status ON articles (text_status)")
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_articles_story_id ON articles (story_id)"))


def _add_summary_kind(engine: Engine) -> None:
    """Pre-report DBs: summaries get ``kind`` (existing rows are briefs) and ``content_json``
    (NULL: old plain-text summaries stay readable); ``summary_jobs`` is keyed by
    ``(story_id, kind)``, rebuilt keeping queued stories as brief jobs."""
    with engine.begin() as conn:
        columns = {c["name"] for c in inspect(conn).get_columns("story_summaries")}
        if "kind" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE story_summaries ADD COLUMN kind VARCHAR(10) NOT NULL "
                    "DEFAULT 'brief'"
                )
            )
        if "content_json" not in columns:
            conn.execute(text("ALTER TABLE story_summaries ADD COLUMN content_json JSON"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_story_summaries_kind ON story_summaries (kind)")
        )
        if "kind" in {c["name"] for c in inspect(conn).get_columns("summary_jobs")}:
            return
        conn.execute(text("ALTER TABLE summary_jobs RENAME TO summary_jobs_old"))
    from app.models import SummaryJob

    SummaryJob.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO summary_jobs (story_id, kind, queued_at, force, attempts,"
                " next_attempt_at, last_error) SELECT story_id, 'brief', queued_at, force,"
                " attempts, next_attempt_at, last_error FROM summary_jobs_old"
            )
        )
        conn.execute(text("DROP TABLE summary_jobs_old"))


def ping(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
