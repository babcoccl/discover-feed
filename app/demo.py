"""Throwaway demo DB seeded from the example profiles and fixture feeds (`make demo`, demo.cmd).

No network is used: every source URL is served from ``tests/fixtures/demo/<id>.xml`` and
article pages from ``tests/fixtures/demo/pages/`` (``index.json`` maps URL to file,
``robots.json`` overrides robots.txt per host; other pages 404) through ``httpx.MockTransport``.
After ingesting, the demo extracts text and clusters stories just like the scheduled pipeline.
The DB lives in ``.demo/`` and is recreated on every run, so the real database (``./data`` or
the Docker volume) is never touched.
"""

import argparse
import asyncio
import json
import threading
import webbrowser
from collections.abc import Sequence
from pathlib import Path

import httpx

from app.cluster.service import ClusterRunResult
from app.config import Source, load_config
from app.db import init_db, make_engine, make_session_factory
from app.extract import ExtractRunResult
from app.ingest import Ingestor, SourceRunResult
from app.pipeline import Pipeline, clusterer_from_settings, extractor_from_settings
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


def page_transport(fixture_dir: Path) -> httpx.MockTransport:
    """Article pages (and robots.txt) for the extractor, from ``<fixture_dir>/pages``."""
    pages_dir = fixture_dir / "pages"
    index = _load_json(pages_dir / "index.json")
    robots = _load_json(pages_dir / "robots.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            body = robots.get(request.url.host, "User-agent: *\nAllow: /\n")
            return httpx.Response(200, text=body, headers={"content-type": "text/plain"})
        name = index.get(str(request.url))
        if name is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(
            200,
            content=(pages_dir / name).read_bytes(),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    return httpx.MockTransport(handler)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


class DemoBuild:
    def __init__(
        self,
        settings: Settings,
        ingested: list[SourceRunResult],
        extracted: ExtractRunResult,
        clustered: ClusterRunResult,
    ) -> None:
        self.settings = settings
        self.ingested = ingested
        self.extracted = extracted
        self.clustered = clustered

    def __iter__(self):  # `settings, results = build_demo()` keeps working
        return iter((self.settings, self.ingested))


def build_demo(
    db_path: Path = DEMO_DB,
    *,
    config_path: Path = EXAMPLE_CONFIG,
    fixture_dir: Path = FIXTURE_DIR,
) -> DemoBuild:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        except PermissionError as exc:  # Windows keeps files open by a running demo locked
            raise SystemExit(
                f"Cannot replace {db_path}: is another demo still running? Stop it and retry."
            ) from exc

    settings = Settings(
        config_path=config_path,
        database_url=f"sqlite:///{db_path.as_posix()}",
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
        pipeline = Pipeline(
            session_factory,
            # Local fixtures: no need for the per-domain politeness delay.
            extractor_from_settings(
                session_factory,
                settings,
                transport=page_transport(fixture_dir),
                domain_delay=0,
                max_articles=10_000,
            ),
            clusterer_from_settings(settings),
        )
        extracted, clustered = asyncio.run(pipeline.run())
    finally:
        engine.dispose()
    return DemoBuild(settings, results, extracted, clustered)


def main() -> None:
    import uvicorn

    from app.main import create_app

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--open",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="open the demo in the default browser once it starts",
    )
    args = parser.parse_args()

    build = build_demo()
    settings = build.settings
    for r in build.ingested:
        print(f"  {r.source_id:<32} {r.status:<6} {r.new_articles} articles")
    e, c = build.extracted, build.clustered
    print(f"  text extraction: {e.ok} ok, {e.failed} failed, {e.skipped} skipped")
    print(f"  stories: {c.stories} ({c.multi_source_stories} covered by 2+ sources)")
    url = f"http://localhost:{args.port}/"
    print(f"Demo DB: {DEMO_DB}\nOpen {url}  (Ctrl+C to stop)")
    if args.open:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
