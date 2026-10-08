import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.cluster.service import refresh_stories
from app.config import LLMRole
from app.demo import build_demo
from app.llm import LLMClient, parse_response
from app.main import create_app
from app.models import Article, Story, StorySummary, SummaryJob
from app.summarize import service
from app.summarize.context import story_context
from app.summarize.fake import FakeLLM
from app.summarize.prompt import build_messages
from app.summarize.service import Summarizer
from app.summarize.sources import select_sources
from app.summarize.validate import validate_summary
from app.summarize.worker import SummaryWorker
from tests.pipeline_helpers import T0, add_article

ROLE = LLMRole(base_url="http://llm.test:8080/v1", model="test-model")
FIXTURE = Path(__file__).parent / "fixtures" / "llm" / "real_response.json"

TEXT_A = (
    "Acme Robotics\n"
    "Acme Robotics said on Tuesday it raised $40 million to expand its warehouse robot fleet. "
    "The company now employs 350 people."
)
TEXT_B = "Investors led by Big Fund put $40 million into Acme Robotics, the startup said."


def make_story(session_factory, articles: list[int]) -> int:
    with session_factory() as session:
        story = Story(first_seen_at=T0, last_updated_at=T0)
        session.add(story)
        session.flush()
        for article_id in articles:
            session.get(Article, article_id).story_id = story.id
        session.flush()
        refresh_stories(session, {story.id})
        session.commit()
        return story.id


def add_member(session_factory, story_id: int, **kwargs) -> int:
    article_id = add_article(session_factory, **kwargs)
    with session_factory() as session:
        session.get(Article, article_id).story_id = story_id
        session.flush()
        refresh_stories(session, {story_id})
        session.commit()
    return article_id


@pytest.fixture
def story(session_factory) -> int:
    a = add_article(
        session_factory,
        url="https://a.example/acme",
        title="Acme raises $40M",
        source_id="a",
        text=TEXT_A,
    )
    b = add_article(
        session_factory,
        url="https://b.example/acme?utm=x",
        title="Acme funding",
        source_id="b",
        hours=1,
        summary="Acme got $40 million.",
        text=TEXT_B,
    )
    return make_story(session_factory, [a, b])


def summarizer(fake: FakeLLM, **kwargs) -> Summarizer:
    return Summarizer(LLMClient(ROLE, transport=fake.transport()), **kwargs)


def summarize(session_factory, story_id, fake, **kwargs):
    return asyncio.run(summarizer(fake).summarize_story(session_factory, story_id, **kwargs))


def rows(session_factory, story_id) -> list[StorySummary]:
    with session_factory() as session:
        stmt = select(StorySummary).where(StorySummary.story_id == story_id)
        return list(session.scalars(stmt.order_by(StorySummary.version)))


def test_ok_summary_is_stored_with_citations(session_factory, story) -> None:
    fake = FakeLLM()
    outcome = summarize(session_factory, story, fake)
    assert outcome.status == "ok" and fake.calls == 1
    [row] = rows(session_factory, story)
    assert row.status == "ok" and row.version == 1 and row.basis == "full_text"
    assert row.citations_json == [[1], [2]] and len(row.article_ids) == 2
    assert row.text.startswith("Acme Robotics said on Tuesday it raised $40 million")
    assert row.model == "fake-llm" and row.prompt_version == service.PROMPT_VERSION
    assert row.prompt_tokens and row.completion_tokens and row.latency_ms is not None


def test_prose_wrapped_json_is_accepted(session_factory, story) -> None:
    assert summarize(session_factory, story, FakeLLM("prose")).status == "ok"


@pytest.mark.parametrize("bad", ["malformed", "out_of_range", "invented_number", "empty"])
def test_retry_once_with_error_then_ok(session_factory, story, bad: str) -> None:
    fake = FakeLLM([bad, "ok"])
    outcome = summarize(session_factory, story, fake)
    assert outcome.status == "ok" and outcome.attempts == 2 and fake.calls == 2
    retry = fake.requests[1]["messages"]
    assert retry[-1]["role"] == "user" and "rejected" in retry[-1]["content"]


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("malformed", "not a valid JSON object"),
        ("out_of_range", "valid source indexes are 1..2"),
        ("invented_number", "987654"),
        ("empty", "empty response"),
    ],
)
def test_retry_then_fallback(session_factory, story, bad: str, reason: str) -> None:
    fake = FakeLLM(bad)
    outcome = summarize(session_factory, story, fake)
    assert outcome.status == "fallback" and fake.calls == 2
    [row] = rows(session_factory, story)
    assert row.status == "fallback" and reason in row.reason and "attempt 2" in row.reason
    assert row.basis == "feed_summary" and row.text == "Acme got $40 million."


@pytest.mark.parametrize("mode", ["http_500", "timeout"])
def test_endpoint_errors_are_failed_and_not_cached(session_factory, story, mode: str) -> None:
    fake = FakeLLM(mode)
    outcome = summarize(session_factory, story, fake)
    assert outcome.status == "failed" and fake.calls == 1
    assert rows(session_factory, story)[0].status == "failed"
    assert summarize(session_factory, story, FakeLLM()).status == "ok"


def test_cache_skips_unchanged_input(session_factory, story) -> None:
    summarize(session_factory, story, FakeLLM())
    fake = FakeLLM()
    assert summarize(session_factory, story, fake).status == "cached" and fake.calls == 0
    assert summarize(session_factory, story, fake, force=True).status == "ok"
    assert [r.version for r in rows(session_factory, story)] == [1, 2]  # versions are kept


def test_new_member_regenerates(session_factory, story) -> None:
    summarize(session_factory, story, FakeLLM())
    add_member(
        session_factory,
        story,
        url="https://c.example/acme",
        title="Acme robots",
        source_id="c",
        hours=2,
        summary="Acme Robotics raised money.",
    )
    fake = FakeLLM()
    assert summarize(session_factory, story, fake).status == "ok" and fake.calls == 1
    latest = rows(session_factory, story)[-1]
    assert latest.version == 2 and len(latest.article_ids) == 3 and latest.basis == "mixed"


def test_prompt_version_change_regenerates(session_factory, story, monkeypatch) -> None:
    summarize(session_factory, story, FakeLLM())
    monkeypatch.setattr(service, "PROMPT_VERSION", "summary-v999")
    fake = FakeLLM()
    assert summarize(session_factory, story, fake).status == "ok" and fake.calls == 1
    assert rows(session_factory, story)[-1].prompt_version == "summary-v999"


def test_input_assembly(session_factory) -> None:
    ids = [
        add_article(
            session_factory,
            url=f"https://s{i % 3}.example/{i}",
            title=f"T{i}",
            source_id=f"s{i % 3}",
            hours=i,
            summary=f"feed {i}",
            text=" ".join(["word"] * 50) if i == 0 else None,
        )
        for i in range(7)
    ]
    with session_factory() as session:
        members = [session.get(Article, i) for i in ids]
        sources = select_sources(members, {"s0": "Zero"}, max_articles=5, max_words=10)
    assert [s.index for s in sources] == [1, 2, 3, 4, 5]
    assert {s.source for s in sources[:3]} == {"Zero", "s1", "s2"}  # one per source first
    assert sources[0].full_text and sources[0].text == " ".join(["word"] * 10)
    assert not sources[1].full_text and sources[1].text == "feed 1"  # text_status != ok
    prompt = build_messages(sources)[1]["content"]
    assert "[1] Source: Zero\nHeadline: T0\nText: word" in prompt


def test_validator_sees_headline_and_text_sent(session_factory, story) -> None:
    with session_factory() as session:
        members = list(session.scalars(select(Article).where(Article.story_id == story)))
        sources = select_sources(members, {}, max_articles=5, max_words=1200)
    content = json.dumps({"summary": "Acme raised $40 million.", "citations": [[2]]})
    assert validate_summary(content, [s.grounding for s in sources])


# --- worker -------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = T0 + timedelta(days=1)

    def __call__(self):
        return self.now


def worker(session_factory, fake, clock, **kwargs) -> SummaryWorker:
    s = Summarizer(LLMClient(ROLE, transport=fake.transport()), clock=clock)
    return SummaryWorker(session_factory, s, clock=clock, **kwargs)


def jobs(session_factory) -> list[SummaryJob]:
    with session_factory() as session:
        return list(session.scalars(select(SummaryJob)))


def test_worker_runs_newest_first_and_respects_max_per_run(session_factory) -> None:
    ids = [
        make_story(
            session_factory,
            [
                add_article(
                    session_factory,
                    url=f"https://x/{i}",
                    title=f"Story {i}",
                    hours=i,
                    summary=f"Story {i} happened.",
                )
            ],
        )
        for i in range(4)
    ]
    clock = Clock()
    w = worker(session_factory, FakeLLM(), clock, max_per_run=3)
    assert w.enqueue(ids) == 4
    result = asyncio.run(w.run())
    assert [o.story_id for o in result.outcomes] == ids[::-1][:3]
    assert result.ok == 3 and result.queued == 1


def test_debounce(session_factory, story) -> None:
    clock = Clock()
    w = worker(session_factory, FakeLLM(), clock, debounce_minutes=30)
    w.enqueue([story])
    assert asyncio.run(w.run()).ok == 1
    add_member(
        session_factory,
        story,
        url="https://c.example/x",
        title="More",
        source_id="c",
        hours=2,
        summary="Acme more.",
    )
    clock.now += timedelta(minutes=10)
    w.enqueue([story])
    assert asyncio.run(w.run()).processed == 0 and w.queue_counts()["waiting"] == 1
    clock.now += timedelta(minutes=21)
    assert asyncio.run(w.run()).ok == 1 and not jobs(session_factory)


def test_worker_isolates_failures_and_backs_off(session_factory, story, monkeypatch) -> None:
    other = make_story(
        session_factory,
        [
            add_article(
                session_factory, url="https://y/1", title="Other", summary="Other news.", hours=5
            )
        ],
    )
    clock = Clock()
    w = worker(session_factory, FakeLLM(), clock, failure_limit=2, pause_minutes=10)
    real = w.summarizer.summarize_story

    async def flaky(session_factory, story_id, **kwargs):
        if story_id == other:
            raise RuntimeError("boom")
        return await real(session_factory, story_id, **kwargs)

    monkeypatch.setattr(w.summarizer, "summarize_story", flaky)
    w.enqueue([story, other])
    result = asyncio.run(w.run())
    assert (result.ok, result.failed) == (1, 1)
    [job] = jobs(session_factory)
    assert job.story_id == other and job.attempts == 1 and "boom" in job.last_error
    assert job.next_attempt_at > clock.now and w.status().last_error == "error: boom"


def test_worker_pauses_after_repeated_endpoint_failures(session_factory) -> None:
    ids = [
        make_story(
            session_factory,
            [
                add_article(
                    session_factory, url=f"https://z/{i}", title=f"Z{i}", hours=i, summary="z."
                )
            ],
        )
        for i in range(4)
    ]
    clock = Clock()
    fake = FakeLLM("http_500")
    w = worker(session_factory, fake, clock, failure_limit=2, pause_minutes=10)
    w.enqueue(ids)
    result = asyncio.run(w.run())
    assert result.status == "paused" and result.failed == 2 and fake.calls == 2
    assert w.status().paused_until == clock.now + timedelta(minutes=10)
    assert asyncio.run(w.run()).status == "paused"  # scheduled runs wait
    fake.modes = ["ok"]
    assert asyncio.run(w.run(story_id=ids[0], manual=True)).ok == 1  # "Summarize now" doesn't


# --- context and API ----------------------------------------------------------------------


def test_story_context_keeps_summary_numbering(session_factory, story) -> None:
    with session_factory() as session:
        before = story_context(session, story)
    assert before.summary is None and [s.index for s in before.sources] == [1, 2]
    summarize(session_factory, story, FakeLLM())
    late = add_member(
        session_factory,
        story,
        url="https://0.example/early",
        title="Early",
        source_id="z",
        hours=-5,
        summary="Acme early.",
    )
    with session_factory() as session:
        context = story_context(session, story)
    assert [c.url for c in context.summary.citations] == [s.url for s in context.sources[:2]]
    assert context.sources[2].article_id == late and context.sources[2].index == 3
    prompt = context.prompt()
    assert "[1] Source: a\nHeadline: Acme raises $40M" in prompt and "[3] Source: z" in prompt
    assert "Current summary: Acme Robotics said" in prompt and "[1] " in prompt


@pytest.fixture(scope="module")
def demo_client(tmp_path_factory):
    build = build_demo(tmp_path_factory.mktemp("summaries") / "demo.db")
    with TestClient(create_app(build.settings, llm_transport=build.llm_transport)) as c:
        c.build = build
        yield c


def test_demo_summarizes_with_fake_llm(demo_client) -> None:
    result = demo_client.build.summarized
    assert result.ok > 0 and result.fallback > 0 and result.failed == 0 and result.queued == 0


def test_api_summary_shape_and_citation_urls(demo_client) -> None:
    feed = demo_client.get("/api/profiles/personal-reader/feed?limit=100").json()["items"]
    with_summary = [i for i in feed if i["summary"]]
    assert with_summary and any(i["summary"] is None for i in feed)
    item = next(i for i in with_summary if i["source_count"] >= 2)
    summary = item["summary"]
    assert set(summary) == {"text", "citations", "sentences", "basis", "model", "generated_at"}
    assert summary["model"] == "fake-llm" and summary["basis"] in ("full_text", "mixed")
    assert "[1]" in summary["text"]
    story = demo_client.get(f"/api/stories/{item['id']}").json()
    assert story["summary"] == summary
    members = {m["url"]: m for m in story["members"]}
    for n, citation in enumerate(summary["citations"], 1):
        assert citation["index"] == n and set(citation) == {"index", "source", "headline", "url"}
        assert citation["url"] in members  # the raw Article.url
        assert members[citation["url"]]["source"] == citation["source"]
    sb = demo_client.build.settings
    from app.db import make_engine, make_session_factory

    engine = make_engine(sb.database_url)
    with make_session_factory(engine)() as session:
        for citation in summary["citations"]:
            article = session.scalar(select(Article).where(Article.url == citation["url"]))
            assert article.title == citation["headline"]
    engine.dispose()


def test_ui_shows_summary_markers_and_fallback(demo_client) -> None:
    html = demo_client.get("/p/personal-reader").text
    assert "data-summary" in html and 'data-citation="1"' in html
    assert "Summarized by fake-llm" in html and "Feed snippet" in html
    feed = demo_client.get("/api/profiles/personal-reader/feed?limit=100").json()["items"]
    item = next(i for i in feed if i["summary"] and i["source_count"] >= 2)
    page = demo_client.get(f"/story/{item['id']}?p=personal-reader").text
    assert 'id="source-1"' in page and "Summarized by fake-llm" in page
    for citation in item["summary"]["citations"]:
        assert f'href="{citation["url"]}"' in page


def test_admin_summarize_and_pipeline_panel(demo_client) -> None:
    feed = demo_client.get("/api/profiles/personal-reader/feed?limit=100").json()["items"]
    story_id = next(i["id"] for i in feed if i["summary"])
    cached = demo_client.post(f"/api/admin/summarize?story_id={story_id}").json()
    assert cached["result"]["cached"] == 1 and cached["status"]["enabled"]
    forced = demo_client.post(f"/api/admin/summarize?story_id={story_id}&force=true").json()
    assert forced["result"]["ok"] == 1
    assert demo_client.post("/api/admin/summarize?story_id=999999").status_code == 404
    waited = demo_client.post("/api/admin/summarize?wait=true").json()
    assert waited["result"]["status"] == "ok" and waited["status"]["queued"] == 0
    settings = demo_client.get("/p/personal-reader/settings").text
    assert 'hx-post="/api/admin/summarize"' in settings and "Summarize now" in settings
    panel = demo_client.get("/p/personal-reader/settings/pipeline").text
    assert "Story summaries" in panel and "queued" in panel and "tokens/s" in panel


def test_admin_summarize_without_summarizer(tmp_path) -> None:
    from app.settings import Settings
    from tests.conftest import EXAMPLE_CONFIG

    settings = Settings(
        config_path=EXAMPLE_CONFIG,
        database_url=f"sqlite:///{(tmp_path / 'x.db').as_posix()}",
        scheduler_enabled=False,
        summarize_enabled=False,
    )
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/admin/summarize").status_code == 409
        assert "No summarizer configured" in c.get("/p/personal-reader/settings/pipeline").text


@pytest.mark.skipif(not FIXTURE.exists(), reason="capture one with python -m app.summarize.capture")
def test_captured_real_response_parses() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = parse_response(data.get("response", data))
    assert result.content and result.model


# --- evaluation commands (offline with --fake) ---------------------------------------------


def test_compare_parses_endpoints() -> None:
    from app.summarize.compare import parse_endpoints

    assert parse_endpoints("a=http://h:8080/v1:llama3.1:8b, b=https://api.x.com/v1:gpt-4o") == [
        ("a", "http://h:8080/v1", "llama3.1:8b"),
        ("b", "https://api.x.com/v1", "gpt-4o"),
    ]
    with pytest.raises(ValueError):
        parse_endpoints("nomodel=http://h:8080/v1")


def test_smoke_compare_capture_with_fake(demo_client, tmp_path, capsys) -> None:
    from app.summarize import capture, compare, smoke

    db = demo_client.build.settings.database_url.removeprefix("sqlite:///")
    common = ["--db", db, "--fake", "--profile", "personal-reader"]
    assert smoke.main([*common, "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "result: ok" in out and "[1] " in out and "2/2 passed validation" in out
    endpoints = "one=http://a:8080/v1:m1,two=http://b:8080/v1:m2"
    assert compare.main([*common, "--endpoints", endpoints, "--stories", "2", "--markdown"]) == 0
    out = capsys.readouterr().out
    assert "| one | m1 | 2/2 (100%) |" in out and "| Story | one | two |" in out
    path = tmp_path / "real_response.json"
    assert capture.main([*common, "--out", str(path)]) == 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert parse_response(saved["response"]).content and "api_key" not in json.dumps(saved)
