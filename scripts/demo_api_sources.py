"""Fake Guardian + NewsData.io demo: API items become stories (no network, no real keys).

    python scripts/demo_api_sources.py [--db .demo/api-demo.db]

Builds the normal demo DB, adds a Guardian and a NewsData source (keys are fake values in
this process's environment), serves both APIs from local fakes, ingests, extracts, clusters
and prints which stories the API items joined. Serve the result with:

    DISCOVER_DATABASE_URL=sqlite:///.demo/api-demo.db DISCOVER_SCHEDULER_ENABLED=false \
        uvicorn app.main:app
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import Source
from app.db import make_engine, make_session_factory
from app.demo import DEMO_PROFILE, build_demo
from app.ingest import Ingestor
from app.models import Article, ProfileRecord, SourceRecord, Story
from app.pipeline import Pipeline, clusterer_from_settings, extractor_from_settings


def fake_apis(titles: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "content.guardianapis.com":
            assert request.headers["api-key"] == os.environ["GUARDIAN_API_KEY"]
            return httpx.Response(
                200,
                json={
                    "response": {
                        "status": "ok",
                        "pages": 1,
                        "results": [
                            {
                                "webTitle": t,
                                "webUrl": f"https://www.theguardian.com/business/demo-{i}",
                                "webPublicationDate": "2026-10-12T07:00:00Z",
                                "fields": {
                                    "trailText": f"Guardian coverage: {t}",
                                    "bodyText": (f"{t}. " * 40).strip(),
                                },
                            }
                            for i, t in enumerate(titles)
                        ],
                    }
                },
            )
        if request.url.host == "newsdata.io":
            assert request.headers["X-ACCESS-KEY"] == os.environ["NEWSDATA_API_KEY"]
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "nextPage": None,
                    "results": [
                        {
                            "title": t,
                            "link": f"https://example.com/newsdata/{i}",
                            "description": f"NewsData: {t}",
                            "pubDate": "2026-10-12 07:30:00",
                        }
                        for i, t in enumerate(titles)
                    ],
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".demo/api-demo.db")
    args = parser.parse_args()
    os.environ.setdefault("GUARDIAN_API_KEY", "fake-guardian-key")
    os.environ.setdefault("NEWSDATA_API_KEY", "fake-newsdata-key")
    build = build_demo(Path(args.db))
    engine = make_engine(build.settings.database_url)
    factory = make_session_factory(engine)
    sources = [
        Source(
            name="Guardian API business",
            type="guardian",
            api_key="GUARDIAN_API_KEY",
            params={"q": "rates", "section": "business"},
            daily_request_budget=100,
        ),
        Source(
            name="NewsData.io markets",
            type="newsdata",
            api_key="NEWSDATA_API_KEY",
            params={"q": "rates", "language": "en"},
            daily_request_budget=50,
        ),
    ]
    with factory() as session:
        profile = session.scalar(select(ProfileRecord).where(ProfileRecord.slug == DEMO_PROFILE))
        newest = session.scalars(
            select(Article)
            .where(Article.story_id.is_not(None))
            .order_by(Article.published_at.desc())
            .limit(3)
        ).all()
        titles = [a.title for a in newest]
        for source in sources:
            record = SourceRecord.from_config(source)
            session.add(record)
            profile.sources.append(record)
        session.commit()
    results = asyncio.run(Ingestor(factory, transport=fake_apis(titles)).run(sources, force=True))
    for r in results:
        print(f"ingest {r.source_id}: {r.status}, {r.new_articles} new")
    pipeline = Pipeline(
        factory,
        extractor_from_settings(
            factory,
            build.settings,
            transport=httpx.MockTransport(lambda r: httpx.Response(404)),
            domain_delay=0,
        ),
        clusterer_from_settings(build.settings),
    )
    asyncio.run(pipeline.run())
    ok = True
    with factory() as session:
        for source in sources:
            articles = session.scalars(select(Article).where(Article.source_id == source.id)).all()
            for a in articles:
                story = session.get(Story, a.story_id) if a.story_id else None
                print(
                    f"  {source.name}: {a.title!r} text={a.text_status} -> "
                    f"story #{a.story_id} ({story.article_count if story else 0} articles)"
                )
            ok = ok and bool(articles) and all(a.story_id for a in articles)
    engine.dispose()
    print("OK: API items joined stories" if ok else "FAIL: some API items have no story")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
