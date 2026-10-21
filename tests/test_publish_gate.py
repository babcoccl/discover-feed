"""Publish gate: story states from ingest, one story at a time, publish policies, views."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.cluster import TfidfClusterer
from app.cluster.service import run_clustering
from app.config import LLMRole
from app.events import StoryEvent, bus, defer
from app.lifecycle import advance_text_states, ensure_stories
from app.llm.client import INFLIGHT, LLMClient, set_max_inflight
from app.models import Article, GenerationAttempt, Story, StoryView, SummaryJob
from app.publish import Policy, migrate
from app.story_views import published_story, resolve
from app.summarize.fake import FakeLLM
from app.summarize.service import Summarizer
from app.summarize.worker import SummaryWorker

SECRET = "sk-test-secret-123"
ROLE = LLMRole(base_url="http://llm.test:8080/v1", model="fake-llm", api_key=SECRET)
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
TEXT = (
    "The city council approved a new budget on Monday that funds 40 new buses and repairs "
    "to the central station. Officials said the plan was debated for three months. "
) * 8


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def add(factory, n: int, *, title: str, text: str | None = TEXT, status: str = "ok", source="a"):
    with factory() as session:
        article = Article(
            source_id=source,
            url=f"https://news.example/{n}",
            canonical_url=f"https://news.example/{n}",
            title=title,
            summary_raw=f"{title}. A short feed snippet.",
            published_at=NOW - timedelta(minutes=n),
            fetched_at=NOW,
            content_hash=hashlib.sha256(title.encode()).hexdigest(),
            text=text,
            text_status=status,
            word_count=len(text.split()) if text else None,
        )
        session.add(article)
        session.flush()
        ensure_stories(session, NOW)
        session.commit()
        return article.id, article.story_id


def cluster(factory) -> None:
    run_clustering(factory, TfidfClusterer())


def make_worker(factory, fake, clock=None, policy=None, **kw) -> SummaryWorker:
    clock = clock or Clock()
    summarizer = Summarizer(LLMClient(ROLE, transport=fake.transport()), clock=clock)
    return SummaryWorker(factory, summarizer, clock=clock, policy=policy or Policy(), **kw)


def story_of(factory, story_id: int) -> Story:
    with factory() as session:
        return session.get(Story, story_id)


def test_story_exists_from_ingest_and_waits_for_text(session_factory) -> None:
    _, sid = add(session_factory, 1, title="Council approves bus budget", status="pending")
    assert story_of(session_factory, sid).state == "ingested"
    with session_factory() as session:
        advance_text_states(session, NOW, timedelta(seconds=120))
        session.commit()
    assert story_of(session_factory, sid).state == "text_pending"
    with session_factory() as session:
        advance_text_states(session, NOW + timedelta(seconds=121), timedelta(seconds=120))
        session.commit()
    story = story_of(session_factory, sid)
    assert story.state == "text_final" and story.state_reason.startswith("extraction_wait")
    cluster(session_factory)
    assert story_of(session_factory, sid).state == "clustered"


def test_merge_keeps_oldest_id_and_redirects(session_factory) -> None:
    _, first = add(session_factory, 1, title="Council approves new bus budget for city")
    cluster(session_factory)
    _, second = add(session_factory, 2, title="City council approves new bus budget", source="b")
    cluster(session_factory)
    assert story_of(session_factory, second).state == "merged"
    with session_factory() as session:
        assert resolve(session, second) == first
        assert session.get(Story, first).source_count == 2


def test_one_story_at_a_time_brief_report_publish(session_factory) -> None:
    _, s1 = add(session_factory, 1, title="Council approves bus budget")
    _, s2 = add(session_factory, 2, title="Museum reopens after flood repairs")
    cluster(session_factory)
    fake = FakeLLM()
    w = make_worker(session_factory, fake)
    result = asyncio.run(w.run())
    assert [(o.story_id, o.kind) for o in result.outcomes] == [
        (s1, "brief"),
        (s1, "report"),
        (s2, "brief"),
        (s2, "report"),
    ]  # newest first, each to its publish decision before the next
    with session_factory() as session:
        story = published_story(session, s1)
        assert story.brief.style == "brief" and story.report.state in ("ready", "failed")
        assert session.get(Story, s1).state == "published"


def test_report_wait_then_publish_with_pending_report(session_factory) -> None:
    _, sid = add(session_factory, 1, title="Council approves bus budget")
    cluster(session_factory)
    clock = Clock()
    w = make_worker(session_factory, FakeLLM(["ok", "timeout"]), clock)
    asyncio.run(w.run())
    assert story_of(session_factory, sid).published_version is None
    assert story_of(session_factory, sid).state == "brief_ready"
    clock.now += timedelta(minutes=31)
    w.sweep()
    with session_factory() as session:
        story = published_story(session, sid)
        assert story.report.state == "pending" and story.report.label
        assert "still generating" in story.report.label


def test_publish_without_report_when_not_required(session_factory) -> None:
    _, sid = add(session_factory, 1, title="Council approves bus budget")
    cluster(session_factory)
    w = make_worker(
        session_factory, FakeLLM(["ok", "timeout"]), policy=Policy(requires_report=False)
    )
    asyncio.run(w.run())
    assert story_of(session_factory, sid).state == "published"


def test_brief_fallback_publishes_snippet_after_fallback_hours(session_factory) -> None:
    _, sid = add(session_factory, 1, title="Council approves bus budget")
    cluster(session_factory)
    clock = Clock()
    w = make_worker(session_factory, FakeLLM("invented_number"), clock)
    asyncio.run(w.run())
    story = story_of(session_factory, sid)
    assert story.published_version is None and story.state_reason.startswith("brief failed")
    clock.now += timedelta(hours=6, minutes=1)
    w.sweep()
    with session_factory() as session:
        brief = published_story(session, sid).brief
        assert brief.style == "snippet" and brief.label.startswith("Summary unavailable: ")


def test_insufficient_text_skips_report_without_llm(session_factory) -> None:
    _, sid = add(
        session_factory, 1, title="Council approves bus budget", text=None, status="failed"
    )
    cluster(session_factory)
    fake = FakeLLM()
    w = make_worker(session_factory, fake)
    asyncio.run(w.run())
    assert fake.calls == 1  # the brief only
    with session_factory() as session:
        report = published_story(session, sid).report
        assert report.state == "skipped" and report.reason == "insufficient_text"
        assert report.details == ["a: failed, 0 words"]


def test_published_version_kept_while_replacement_generates(session_factory) -> None:
    _, sid = add(session_factory, 1, title="Council approves new bus budget for city")
    cluster(session_factory)
    w = make_worker(session_factory, FakeLLM())
    asyncio.run(w.run())
    v1 = story_of(session_factory, sid).published_version
    add(session_factory, 2, title="City council approves new bus budget", source="b")
    cluster(session_factory)
    assert story_of(session_factory, sid).state == "clustered"
    w2 = make_worker(session_factory, FakeLLM("http_500"))
    asyncio.run(w2.run())
    with session_factory() as session:
        assert session.get(Story, sid).published_version == v1
        assert len(published_story(session, sid).members) == 1  # old version still served
    asyncio.run(make_worker(session_factory, FakeLLM()).run(manual=True))
    with session_factory() as session:
        assert session.get(Story, sid).published_version > v1
        assert len(published_story(session, sid).members) == 2


def test_backfill_limits(session_factory) -> None:
    words = ["volcano", "orchestra", "satellite", "harvest", "glacier"]
    ids = [
        add(session_factory, n, title=f"{w.title()} news {n}", text=f"{w} report. " * 60)[1]
        for n, w in enumerate(words)
    ]
    cluster(session_factory)
    clock = Clock()
    w = make_worker(session_factory, FakeLLM(), clock, backfill_max=2)
    with session_factory() as session:
        assert w.candidates(session) == ids[:2]
    clock.now += timedelta(hours=25)
    with session_factory() as session:
        assert w.candidates(session) == []  # older than summarize_backfill_hours


def test_inflight_cap_is_global() -> None:
    set_max_inflight(1)
    INFLIGHT.peak = 0
    clients = [LLMClient(ROLE, transport=FakeLLM(delay=0.05).transport()) for _ in range(3)]

    async def go():
        await asyncio.gather(
            *(c.chat([{"role": "user", "content": "hi"}], purpose="brief") for c in clients)
        )

    asyncio.run(go())
    assert INFLIGHT.peak == 1


def test_attempt_reasons_and_no_key_leak(session_factory) -> None:
    for mode, reason in (
        ("truncated", "truncated"),
        ("reasoning_only", "reasoning_consumed_budget"),
        ("http_401", "http_401"),
        ("empty", "empty_content"),
    ):
        _, sid = add(session_factory, 1 + len(mode), title=f"Headline about {mode} today")
        cluster(session_factory)
        asyncio.run(make_worker(session_factory, FakeLLM(mode)).run(story_id=sid, manual=True))
        with session_factory() as session:
            rows = list(
                session.scalars(select(GenerationAttempt).where(GenerationAttempt.story_id == sid))
            )
        assert rows and rows[0].reason == reason, (mode, [r.reason for r in rows])
        for row in rows:
            assert row.endpoint_host == "llm.test:8080"
            assert SECRET not in f"{row.raw_content}{row.reason}{row.members}{row.endpoint_host}"
            assert row.members and row.members[0]["text_status"] == "ok"


def test_report_prompt_contains_every_included_members_text(session_factory) -> None:
    add(session_factory, 1, title="Council approves new bus budget for city")
    add(
        session_factory,
        2,
        title="City council approves new bus budget",
        source="b",
        text=TEXT + " Riders welcomed the city council bus budget vote.",
    )
    cluster(session_factory)
    fake = FakeLLM()
    asyncio.run(make_worker(session_factory, fake).run())
    report_prompts = [r for r in fake.requests if FakeLLM.kind_of(r) == "report"]
    user = report_prompts[0]["messages"][-1]["content"]
    assert "The city council approved a new budget" in user and "Riders welcomed" in user


def test_events_publish_after_commit_only(session_factory) -> None:
    async def go():
        queue = bus.subscribe()
        with session_factory() as session:
            session.execute(select(1))  # inside a transaction, as in the worker
            defer(session, StoryEvent("story.published", 1, 1, ("a",)))
            session.rollback()
            defer(session, StoryEvent("story.updated", 2, 3, ("a",)))
            session.commit()
        item = await asyncio.wait_for(queue.get(), 1)
        bus.unsubscribe(queue)
        return item

    item = asyncio.run(go())
    assert item.type == "story.updated" and item.payload() == {"story_id": 2, "version": 3}


def test_migration_is_idempotent(tmp_path: Path) -> None:
    from app.db import make_engine, make_session_factory
    from app.demo import build_demo

    build = build_demo(tmp_path / "demo.db")
    factory = make_session_factory(make_engine(build.settings.database_url))
    with factory() as session:  # back to a pre-publish-gate DB
        published = session.scalar(select(func.count()).where(Story.published_version.is_not(None)))
        session.query(StoryView).delete()
        session.query(SummaryJob).delete()
        session.query(Story).filter(Story.state != "merged").update(
            {"published_version": None, "state": "clustered", "brief_ready_at": None}
        )
        session.commit()
    policy = Policy(brief_fallback=timedelta(0))
    assert migrate(factory, summarizer=None, policy=policy) == published
    with factory() as session:
        views = session.scalar(select(func.count()).select_from(StoryView))
    assert migrate(factory, summarizer=None, policy=policy) == 0
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(StoryView)) == views


def test_diagnose_dry_run(tmp_path: Path) -> None:
    from app.demo import build_demo
    from app.summarize.diagnose import diagnose

    build = build_demo(tmp_path / "demo.db")
    text = asyncio.run(
        diagnose(
            ["--auto", "--dry-run", "--markdown"],
            settings=build.settings,
            transport=FakeLLM().transport(),
        )
    )
    assert "## Story" in text and "text_status=ok" in text and "prompt:" in text
    assert "outcome=" not in text  # dry run: no model call
