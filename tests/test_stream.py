"""/api/v1/stream: stored events, SSE format, Last-Event-ID / since_id replay, heartbeat,
filters, and slow consumers never blocking the publisher."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api_v1 import StreamFilter, stream_events
from app.demo import build_demo
from app.events import bus, events_after, latest_event_id, record
from app.main import create_app
from app.models import Story


@pytest.fixture
def app_client(tmp_path):
    build = build_demo(tmp_path / "demo.db")
    with TestClient(create_app(build.settings, llm_transport=build.llm_transport)) as c:
        yield c


def parse(chunks: list[str]) -> list[dict]:
    events = []
    for chunk in chunks:
        fields: dict = {}
        for line in chunk.strip().splitlines():
            key, _, value = line.partition(": ")
            fields[key] = value
        if "data" in fields:
            fields["data"] = json.loads(fields["data"])
        events.append(fields)
    return events


async def take(gen, n: int, timeout: float = 5) -> list[str]:
    out = []

    async def run():
        async for chunk in gen:
            out.append(chunk)
            if len(out) >= n:
                break

    await asyncio.wait_for(run(), timeout)
    await gen.aclose()
    return out


def collect(factory, f, *, after_id, n, heartbeat=15, queue=None):
    async def go():
        q = queue or bus.subscribe()
        try:
            return await take(
                stream_events(factory, f, q, after_id=after_id, heartbeat=heartbeat), n
            )
        finally:
            bus.unsubscribe(q)

    return parse(asyncio.run(go()))


def test_replay_format_and_increasing_ids(app_client) -> None:
    factory = app_client.app.state.session_factory
    with factory() as session:
        stored = events_after(session, 0)
        latest = latest_event_id(session)
    assert stored and latest == stored[-1].id
    assert {e.type for e in stored} <= {"story.published", "story.updated", "story.retracted"}
    events = collect(factory, StreamFilter(), after_id=0, n=2 + len(stored))
    assert events[0] == {"retry": "5000"}
    assert events[1]["event"] == "hello" and events[1]["data"]["latest_id"] == latest
    ids = [int(e["id"]) for e in events[2:]]
    assert ids == [e.id for e in stored] and ids == sorted(set(ids))
    first = events[2]
    assert first["event"] == first["data"]["type"] and first["data"]["id"] == ids[0]
    assert set(first["data"]) == {"id", "type", "story_id", "version", "profiles"}


def test_since_id_replays_only_newer(app_client) -> None:
    factory = app_client.app.state.session_factory
    with factory() as session:
        stored = events_after(session, 0)
    middle = stored[len(stored) // 2].id
    newer = [e.id for e in stored if e.id > middle]
    events = collect(factory, StreamFilter(), after_id=middle, n=2 + len(newer))
    assert [int(e["id"]) for e in events[2:]] == newer


def test_no_resume_point_starts_live_and_heartbeats(app_client) -> None:
    factory = app_client.app.state.session_factory
    events = collect(factory, StreamFilter(), after_id=None, n=3, heartbeat=0.05)
    assert events[1]["event"] == "hello"
    assert events[2]["event"] == "heartbeat" and "id" not in events[2]


def test_live_event_after_replay_and_card_include(app_client) -> None:
    factory = app_client.app.state.session_factory
    with factory() as session:
        story_id, version = session.execute(
            select(Story.id, Story.published_version).where(Story.published_version.is_not(None))
        ).first()
        latest = latest_event_id(session)

    async def go():
        queue = bus.subscribe()
        gen = stream_events(
            factory, StreamFilter(include="card", base="http://x"), queue, after_id=latest
        )
        out = [await gen.__anext__(), await gen.__anext__()]

        def publish():
            with factory() as session:
                source_ids = [
                    m["source_id"] for m in session.get(Story, story_id).__dict__.get("members", [])
                ]
                record(session, "story.updated", story_id, version, source_ids or ["the-verge"])
                session.commit()

        await asyncio.to_thread(publish)
        out.append(await asyncio.wait_for(gen.__anext__(), 5))
        await gen.aclose()
        bus.unsubscribe(queue)
        return parse(out)

    events = asyncio.run(go())
    live = events[2]
    assert int(live["id"]) == latest + 1 and live["event"] == "story.updated"
    assert live["data"]["story"]["id"] == story_id and live["data"]["story"]["url"].startswith(
        "http://x/story/"
    )


def test_profile_filter(app_client) -> None:
    factory = app_client.app.state.session_factory
    with factory() as session:
        stored = events_after(session, 0)
    for slug in ("personal-reader", "market-monitor"):
        mine = [e.id for e in stored if slug in e.profiles]
        assert mine
        f = StreamFilter(profile=slug)
        events = collect(factory, f, after_id=0, n=2 + len(mine))
        assert [int(e["id"]) for e in events[2:]] == mine
        assert all(slug in e["data"]["profiles"] for e in events[2:])


def test_slow_consumer_never_blocks_and_catches_up(app_client) -> None:
    factory = app_client.app.state.session_factory
    with factory() as session:
        start = latest_event_id(session)

    async def go():
        queue = bus.subscribe(maxsize=1)
        t0 = time.monotonic()
        with factory() as session:
            story_id = session.scalar(select(Story.id).limit(1))
            for n in range(20):
                record(session, "story.updated", story_id, n, ["the-verge"])
            session.commit()  # bus.publish runs here, for a consumer that is not reading
        elapsed = time.monotonic() - t0
        await asyncio.sleep(0.05)
        assert queue.overflowed and elapsed < 2
        gen = stream_events(factory, StreamFilter(), queue, after_id=start)
        out = await take(gen, 22)
        bus.unsubscribe(queue)
        return parse(out)

    events = asyncio.run(go())
    assert [int(e["id"]) for e in events[2:]] == list(range(start + 1, start + 21))


def test_endpoint_headers_and_validation(app_client) -> None:
    assert app_client.get("/api/v1/stream?topic=1").status_code == 422
    assert app_client.get("/api/v1/stream?profile=nope").status_code == 404
    bad = app_client.get("/api/v1/stream", headers={"Last-Event-ID": "abc"})
    assert bad.status_code == 422


def test_retention_prunes_old_events(app_client) -> None:
    from datetime import timedelta

    from app.models import StoryEventRow, utcnow

    factory = app_client.app.state.session_factory
    with factory() as session:
        for row in session.scalars(select(StoryEventRow)):
            row.created_at = utcnow() - timedelta(days=8)
        session.commit()
        record(session, "story.updated", 1, 1, [], retention=timedelta(days=7))
        session.commit()
        assert [e.type for e in events_after(session, 0)] == ["story.updated"]
