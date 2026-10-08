"""`make demo`: a throwaway DB seeded from the example profiles and loaded from fixture feeds.

No network is used for feeds: every source URL is served from ``tests/fixtures/demo/<id>.xml``
through ``httpx.MockTransport``. The DB lives in ``.demo/`` and is recreated on every run,
so the real database (``./data`` or the Docker volume) is never touched.
"""

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

import httpx

from app.config import Source, load_config
from app.db import init_db, make_engine, make_session_factory
from app.ingest import Ingestor, SourceRunResult
from app.profiles import active_sources, seed_from_config
from app.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
DEMO_DB = ROOT / ".demo" / "demo.db"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "demo"
EXAMPLE_CONFIG = ROOT / "config" / "profiles.example.yaml"


def fixture_transport(sources: Sequence[Source], fixture_dir: Path) -> httpx.MockTransport:
    files = {str(httpx.URL(str(s.url))): fixture_dir / f"{s.id}.xml" for s in sources}

    def handler(request: httpx.Request) -> httpx.Response:
        path = files.get(str(request.url))
        if path is None or not path.exists():
            return httpx.Response(404)
        return httpx.Response(
            200, content=path.read_bytes(), headers={"content-type": "application/rss+xml"}
        )

    return httpx.MockTransport(handler)


def build_demo(
    db_path: Path = DEMO_DB,
    *,
    config_path: Path = EXAMPLE_CONFIG,
    fixture_dir: Path = FIXTURE_DIR,
) -> tuple[Settings, list[SourceRunResult]]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)

    settings = Settings(
        config_path=config_path,
        database_url=f"sqlite:///{db_path}",
        scheduler_enabled=False,
    )
    engine = make_engine(settings.database_url)
    try:
        init_db(engine)
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            seed_from_config(session, load_config(config_path))
            sources = active_sources(session)
        ingestor = Ingestor(session_factory, transport=fixture_transport(sources, fixture_dir))
        results = asyncio.run(ingestor.run(sources, force=True))
    finally:
        engine.dispose()
    return settings, results


def main() -> None:
    import uvicorn

    from app.main import create_app

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    settings, results = build_demo()
    for r in results:
        print(f"  {r.source_id:<32} {r.status:<6} {r.new_articles} articles")
    print(f"Demo DB: {DEMO_DB}\nOpen http://localhost:{args.port}/")
    uvicorn.run(create_app(settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
