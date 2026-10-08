from pathlib import Path

from sqlalchemy import text

from app.db import init_db, make_engine


def test_init_db_adds_and_backfills_article_url(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    init_db(engine)
    insert = text(
        "INSERT INTO articles (source_id, url, canonical_url, title, summary_raw, published_at,"
        " fetched_at, content_hash, raw_json) VALUES ('s', '', :canon, '', '',"
        " '2026-10-07 00:00:00', '2026-10-07 00:00:00', :canon, :raw)"
    )
    with engine.begin() as conn:
        conn.execute(
            insert,
            {
                "canon": "https://a.example/x",
                "raw": '{"link": " https://a.example/x/?utm_source=r "}',
            },
        )
        conn.execute(insert, {"canon": "https://b.example/y", "raw": "{}"})
        conn.execute(insert, {"canon": "https://c.example/z", "raw": '{"link": "/relative"}'})
        conn.execute(text("ALTER TABLE articles DROP COLUMN url"))  # pre-`url` schema

    init_db(engine)
    init_db(engine)  # idempotent

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT canonical_url, url FROM articles")).all())
    assert rows == {
        "https://a.example/x": "https://a.example/x/?utm_source=r",
        "https://b.example/y": "https://b.example/y",
        "https://c.example/z": "https://c.example/z",
    }
    engine.dispose()
