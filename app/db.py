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
    _add_story_lifecycle(engine)
    _add_attempt_quality_columns(engine)
    _add_source_flags(engine)
    _add_alert_columns(engine)
    _add_source_management_columns(engine)
    _add_image_columns(engine)
    _add_enrichment_columns(engine)
    _add_context_columns(engine)


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


_STORY_COLUMNS = {
    # Stories from before the publish gate were already clustered.
    "state": "VARCHAR(20) NOT NULL DEFAULT 'clustered'",
    "state_reason": "TEXT",
    "priority": "INTEGER NOT NULL DEFAULT 0",
    "merged_into": "INTEGER",
    "ingested_at": "DATETIME",
    "clustered_at": "DATETIME",
    "brief_ready_at": "DATETIME",
    "published_version": "INTEGER",
    "published_at": "DATETIME",
    "published_sort_at": "DATETIME",
}


def _add_story_lifecycle(engine: Engine) -> None:
    """Pre-publish-gate DBs: add the story state columns (``story_views`` is built from the
    old summaries at startup, see ``app.publish.migrate``)."""
    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(conn).get_columns("stories")}
        for name, ddl in _STORY_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE stories ADD COLUMN {name} {ddl}"))
        for name in ("state", "published_version", "published_sort_at"):
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_stories_{name} ON stories ({name})"))


_ATTEMPT_COLUMNS = {
    "run_id": "VARCHAR(32)",
    "attempt": "INTEGER",
    "prompt": "VARCHAR(10)",
    "temperature": "FLOAT",
    "warnings": "JSON",
}


def _add_attempt_quality_columns(engine: Engine) -> None:
    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(conn).get_columns("generation_attempts")}
        for name, ddl in _ATTEMPT_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE generation_attempts ADD COLUMN {name} {ddl}"))


def _add_source_flags(engine: Engine) -> None:
    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(conn).get_columns("sources")}
        for name in ("aggregator", "first_party"):
            if name not in existing:
                conn.execute(
                    text(f"ALTER TABLE sources ADD COLUMN {name} BOOLEAN NOT NULL DEFAULT 0")
                )


def _add_alert_columns(engine: Engine) -> None:
    """Pre-alerts DBs: event payloads and the once-only alert seeding marker."""
    with engine.begin() as conn:
        if "data" not in {c["name"] for c in inspect(conn).get_columns("story_events")}:
            conn.execute(text("ALTER TABLE story_events ADD COLUMN data JSON"))
        if "alerts_seeded_at" not in {c["name"] for c in inspect(conn).get_columns("profiles")}:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN alerts_seeded_at DATETIME"))


def _add_source_management_columns(engine: Engine) -> None:
    """Pre-source-management DBs: API params/limits, soft delete and auto-disable markers."""
    columns = {
        "params": "JSON",
        "api_key_env": "VARCHAR(100)",
        "limits": "JSON",
        "removed_at": "DATETIME",
        "auto_disabled_at": "DATETIME",
        "disabled_reason": "TEXT",
        "created_at": "DATETIME",
    }
    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(conn).get_columns("sources")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE sources ADD COLUMN {name} {ddl}"))
        if "items_last_run" not in {c["name"] for c in inspect(conn).get_columns("source_status")}:
            conn.execute(text("ALTER TABLE source_status ADD COLUMN items_last_run INTEGER"))


def ping(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


_IMAGE_COLUMNS = {
    "articles": {
        "page_image_url": "VARCHAR(2048)",
        "icon_url": "VARCHAR(2048)",
        "page_scanned_at": "DATETIME",
        "image_source": "VARCHAR(10)",
        "image_checked_at": "DATETIME",
    },
    "stories": {"image_source": "VARCHAR(10)"},
}


def _add_image_columns(engine: Engine) -> None:
    """Pre-image DBs: article/story image columns (old articles are picked up by the image
    backfill)."""
    with engine.begin() as conn:
        for table, columns in _IMAGE_COLUMNS.items():
            existing = {c["name"] for c in inspect(conn).get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        for name in ("page_image_url", "image_checked_at", "image_url"):
            conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS ix_articles_{name} ON articles ({name})")
            )


_ENRICHMENT_COLUMNS = {
    "articles": {
        "origin": "VARCHAR(12) NOT NULL DEFAULT 'feed'",
        "discovered_for_story_id": "INTEGER",
        "anchor_article_id": "INTEGER",
    },
    "stories": {
        "enrich_status": "VARCHAR(10)",
        "enrich_started_at": "DATETIME",
        "enriched_at": "DATETIME",
        "enrich_added": "INTEGER NOT NULL DEFAULT 0",
        "enrich_regens": "INTEGER NOT NULL DEFAULT 0",
        "enrich_reason": "TEXT",
        "enrich_next_attempt_at": "DATETIME",
        "enrich_attempts": "INTEGER NOT NULL DEFAULT 0",
        "enrich_wait_until": "DATETIME",
        "enrich_passes": "INTEGER NOT NULL DEFAULT 0",
        "enrich_regen_at": "DATETIME",
    },
    "enrichment_candidates": {
        "title_similarity": "FLOAT",
        "shared_entities": "INTEGER",
        "flagged": "VARCHAR(40) NOT NULL DEFAULT ''",
    },
    "profiles": {"enrichment_enabled": "BOOLEAN"},
}


def _add_enrichment_columns(engine: Engine) -> None:
    """Pre-enrichment DBs: article origin/pinning, story enrichment state, profile toggle."""
    with engine.begin() as conn:
        for table, columns in _ENRICHMENT_COLUMNS.items():
            existing = {c["name"] for c in inspect(conn).get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_articles_discovered_for_story_id "
                "ON articles (discovered_for_story_id)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_stories_enrich_status ON stories (enrich_status)")
        )


_CONTEXT_COLUMNS = {
    "articles": {
        "context_note": "VARCHAR(300)",
        "context_angle": "VARCHAR(120)",
        "context_kind": "VARCHAR(10)",
    },
    "stories": {
        "context_status": "VARCHAR(10)",
        "context_at": "DATETIME",
        "context_attempts": "INTEGER NOT NULL DEFAULT 0",
        "context_next_attempt_at": "DATETIME",
        "context_reason": "TEXT",
    },
    "profiles": {"context_enabled": "BOOLEAN"},
    "context_candidates": {
        "flagged": "VARCHAR(40) NOT NULL DEFAULT ''",
        "is_opinion": "BOOLEAN NOT NULL DEFAULT 0",
        "judge_same_event": "BOOLEAN",
        "same_event_signal": "VARCHAR(20) NOT NULL DEFAULT ''",
    },
}


def _add_context_columns(engine: Engine) -> None:
    """Pre-context DBs: article note/angle/kind, story context state, profile toggle
    (``context_candidates`` itself is created by ``Base.metadata.create_all``; its
    flag/opinion columns are added here when an older DB already has the table)."""
    with engine.begin() as conn:
        for table, columns in _CONTEXT_COLUMNS.items():
            existing = {c["name"] for c in inspect(conn).get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_stories_context_status ON stories (context_status)")
        )
