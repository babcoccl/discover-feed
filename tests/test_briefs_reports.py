"""Briefs (cards) and detailed reports (story page): validators, generation, queue, API, UI."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.db import init_db, make_engine, make_session_factory
from app.demo import build_demo
from app.main import create_app
from app.models import StorySummary, SummaryJob
from app.summarize.fake import FakeLLM, fake_report
from app.summarize.prompt import PROMPT_VERSIONS, SourceDoc, build_messages
from app.summarize.validate import SummaryInvalid, validate_brief, validate_report
from app.summarize.worker import SummaryWorker
from tests.pipeline_helpers import add_article
from tests.test_summarizer import TEXT_A, TEXT_B, make_story, summarizer

SUBJECTS = ["The council", "Engineers", "Residents", "The mayor", "Inspectors", "Contractors"]
VERBS = ["reviewed", "debated", "inspected", "questioned", "approved", "delayed"]
OBJECTS = ["the bridge plan", "the river crossing", "the budget draft", "the traffic study"]
PLACES = ["at a long meeting", "near the old harbour", "in the town hall", "after the storm"]
LONG = "\n".join(
    f"{SUBJECTS[i % 6]} {VERBS[(i * 5) % 6]} {OBJECTS[i % 4]} {PLACES[(i * 3) % 4]} "
    f"while observers from district {chr(65 + i)} watched closely and took careful notes."
    for i in range(16)
)
LONG2 = "\n".join(
    f"Volunteers from the {w} club planted trees along the canal path on a {d} morning, "
    f"and organisers said the {w} group hopes to return before winter arrives."
    for w, d in zip(
        ["rowing", "chess", "hiking", "garden", "cycling", "choir", "drama", "camera"],
        ["cold", "bright", "windy", "quiet", "damp", "clear", "grey", "busy"],
        strict=True,
    )
)


def brief(**overrides) -> str:
    data = {
        "lead": "Acme Robotics raised $40 million to expand its warehouse robot fleet.",
        "lead_citations": [1],
        "bullets": [
            {"text": "Investors led by Big Fund backed the startup.", "citations": [2]},
            {"text": "The company now employs 350 people.", "citations": [1]},
            {"text": "Acme plans to grow its fleet of warehouse machines.", "citations": [1, 2]},
        ],
    }
    data.update(overrides)
    return json.dumps(data)


GROUNDING = [TEXT_A, TEXT_B]


def test_brief_validator_accepts_a_good_brief() -> None:
    valid = validate_brief(brief(), GROUNDING)
    assert valid.lead.startswith("Acme") and [b.citations for b in valid.bullets][2] == [1, 2]


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("not json", "not a valid JSON object"),
        (brief(lead=""), "lead"),
        (brief(bullets="x"), '"bullets" must be a list'),
        (brief(bullets=[{"text": "One point here.", "citations": [1]}] * 2), "exactly 3 bullets"),
        (brief(lead="Acme raised money. It hired people."), "exactly one sentence"),
        (brief(lead="Acme " + "robots " * 30 + "grew."), "the limit is 30"),
        (brief(lead_citations=[]), "lead has no citation"),
        (brief(lead_citations=[3]), "valid source indexes are 1..2"),
        (
            brief(
                bullets=[{"text": "Investors put in money.", "citations": [2]}] * 2
                + [{"text": "Acme plans to grow its warehouse machines.", "citations": [1]}]
            ),
            "repeats",
        ),
        (
            brief(bullets=[{"text": "A " + "word " * 28 + "end.", "citations": [1]}] * 3),
            "the limit is 28",
        ),
        (
            brief(
                bullets=[
                    {"text": "Investors led by Big Fund backed the startup.", "citations": [2]},
                    {"text": "The company now employs 9000 people.", "citations": [1]},
                    {"text": "Acme plans to grow its warehouse machines.", "citations": [1]},
                ]
            ),
            "9000",
        ),
        (
            brief(
                bullets=[
                    {
                        "text": "Acme Robotics raised $40 million to expand its robot fleet.",
                        "citations": [1],
                    },
                    {"text": "The company now employs 350 people.", "citations": [1]},
                    {"text": "Investors led by Big Fund backed the startup.", "citations": [2]},
                ]
            ),
            "repeats the lead",
        ),
        (
            brief(
                bullets=[
                    {"text": "Investors led by Big Fund backed the startup.", "citations": []},
                    {"text": "The company now employs 350 people.", "citations": [1]},
                    {"text": "Acme plans to grow its warehouse machines.", "citations": [1]},
                ]
            ),
            "bullet 1 has no citation",
        ),
    ],
)
def test_brief_validator_rejects(content: str, error: str) -> None:
    with pytest.raises(SummaryInvalid, match=error.replace("$", r"\$")):
        validate_brief(content, GROUNDING)


def test_duplicate_threshold_is_configurable() -> None:
    near = brief(
        bullets=[
            {"text": "Investors led by Big Fund backed the startup.", "citations": [2]},
            {"text": "Investors led by Big Fund backed the company.", "citations": [2]},
            {"text": "The company now employs 350 people.", "citations": [1]},
        ]
    )
    with pytest.raises(SummaryInvalid, match="bullet 2 repeats bullet 1"):
        validate_brief(near, GROUNDING)
    assert validate_brief(near, GROUNDING, duplicate_threshold=0.95)


def _report_prompt(texts: list[str]) -> str:
    docs = [
        SourceDoc(
            i, i, f"Outlet {chr(64 + i)}", f"Story {chr(64 + i)}", f"https://s{i}.example/", t, True
        )
        for i, t in enumerate(texts, 1)
    ]
    return build_messages(docs, kind="report")[-1]["content"]


def report_sources() -> list[str]:
    return [LONG, LONG2]


def test_report_validator_accepts_fake_report() -> None:
    from app.summarize.prompt import parse_sources

    prompt = _report_prompt(report_sources())
    assert len(parse_sources(prompt)) == 2
    valid = validate_report(json.dumps(fake_report(prompt)), report_sources())
    assert 3 <= len(valid.paragraphs) <= 5 and 225 <= valid.word_count <= 500


def _paragraphs(n: int, words: int = 80, cite=(1,)) -> str:
    return json.dumps(
        {
            "paragraphs": [
                {
                    "text": " ".join(f"w{p}x{i}" for i in range(words)) + ".",
                    "citations": list(cite),
                }
                for p in range(n)
            ]
        }
    )


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("{}", '"paragraphs" must be a list'),
        (_paragraphs(2, 150), "3-5 paragraphs"),
        (_paragraphs(6, 60), "3-5 paragraphs"),
        (_paragraphs(3, 30), "write 225-500 words"),
        (_paragraphs(5, 120), "write 225-500 words"),
        (_paragraphs(4, 80, cite=()), "paragraph 1 has no citation"),
        (_paragraphs(4, 80, cite=(3,)), "valid source indexes are 1..2"),
    ],
)
def test_report_validator_rejects(content: str, error: str) -> None:
    with pytest.raises(SummaryInvalid, match=error):
        validate_report(content, report_sources())


def _with_paragraph(text_: str, n: int = 1) -> str:
    good = json.loads(json.dumps(fake_report(_report_prompt(report_sources()))))
    good["paragraphs"][n]["text"] += " " + text_
    return json.dumps(good)


def test_report_verbatim_overlap_guard() -> None:
    copied = "planted trees along the canal path on a cold morning and organisers"
    with pytest.raises(SummaryInvalid, match="word for word"):
        validate_report(_with_paragraph(copied), report_sources())
    seven = " ".join(copied.split()[:7]) + " ... later"
    assert validate_report(_with_paragraph(seven), report_sources())


def test_report_rejects_invented_number_and_duplicates() -> None:
    with pytest.raises(SummaryInvalid, match="987654"):
        validate_report(_with_paragraph("Officials said 987654 people came."), report_sources())
    good = fake_report(_report_prompt(report_sources()))
    good["paragraphs"][3] = dict(good["paragraphs"][1])
    with pytest.raises(SummaryInvalid, match="repeats"):
        validate_report(json.dumps(good), report_sources())


# --- generation -------------------------------------------------------------------------


@pytest.fixture
def story(session_factory) -> int:
    a = add_article(
        session_factory, url="https://a.example/x", title="Bridge", source_id="a", text=LONG
    )
    b = add_article(
        session_factory,
        url="https://b.example/x?utm=1",
        title="Bridge plan",
        source_id="b",
        hours=1,
        summary="The bridge plan was debated.",
        text=LONG2,
    )
    return make_story(session_factory, [a, b])


def run(session_factory, story_id, fake, **kwargs):
    return asyncio.run(summarizer(fake).summarize_story(session_factory, story_id, **kwargs))


def rows(session_factory, story_id, kind):
    with session_factory() as session:
        stmt = select(StorySummary).where(
            StorySummary.story_id == story_id, StorySummary.kind == kind
        )
        return list(session.scalars(stmt.order_by(StorySummary.version)))


def test_report_ok_is_stored(session_factory, story) -> None:
    outcome = run(session_factory, story, FakeLLM(), kind="report")
    assert outcome.status == "ok" and outcome.kind == "report"
    [row] = rows(session_factory, story, "report")
    assert row.prompt_version == PROMPT_VERSIONS["report"] and row.status == "ok"
    assert 3 <= len(row.content_json["paragraphs"]) <= 5
    assert rows(session_factory, story, "brief") == []


@pytest.mark.parametrize(
    ("bad", "error"),
    [("wrong_bullet_count", "exactly 3 bullets"), ("duplicate_bullets", "repeats")],
)
def test_brief_retry_then_ok(session_factory, story, bad: str, error: str) -> None:
    fake = FakeLLM([bad, "ok"])
    outcome = run(session_factory, story, fake)
    assert outcome.status == "ok" and outcome.attempts == 2
    assert error in fake.requests[1]["messages"][-1]["content"]


def test_brief_falls_back_to_summary(session_factory, story) -> None:
    fake = FakeLLM(["duplicate_bullets", "duplicate_bullets", "ok"])
    outcome = run(session_factory, story, fake)
    assert outcome.status == "fallback" and fake.calls == 3
    [row] = rows(session_factory, story, "brief")
    assert row.status == "fallback" and "repeats" in row.reason
    assert row.content_json == {"style": "summary"} and row.text


@pytest.mark.parametrize(
    ("bad", "error"),
    [
        ("copied_report", "word for word"),
        ("short_report", "write 225-500 words"),
        ("invented_number", "987654"),
    ],
)
def test_report_retry_then_failed_without_substitute(session_factory, story, bad, error) -> None:
    fake = FakeLLM([bad, "ok"])
    assert run(session_factory, story, fake, kind="report").status == "ok"
    assert error in fake.requests[1]["messages"][-1]["content"]

    fake = FakeLLM(bad)
    outcome = run(session_factory, story, fake, kind="report", force=True)
    assert outcome.status == "rejected" and fake.calls == 2
    row = rows(session_factory, story, "report")[-1]
    assert row.status == "failed" and error in row.reason and row.text == ""
    assert row.content_json is None or not row.content_json.get("paragraphs")
    again = FakeLLM()
    assert run(session_factory, story, again, kind="report").status == "cached"
    assert again.calls == 0  # an unchanged input that failed validation is not retried
    assert run(session_factory, story, again, kind="report", force=True).status == "ok"


def test_report_skipped_when_text_insufficient(session_factory) -> None:
    single = make_story(
        session_factory,
        [
            add_article(
                session_factory, url="https://a.example/s", title="S", source_id="a", text=TEXT_A
            )
        ],
    )
    fake = FakeLLM()
    outcome = run(session_factory, single, fake, kind="report")
    assert outcome.status == "skipped" and outcome.reason == "insufficient_text"
    assert fake.calls == 0
    [row] = rows(session_factory, single, "report")
    assert row.status == "skipped" and row.reason == "insufficient_text"
    assert run(session_factory, single, fake).status == "ok"  # the brief is still written


def test_brief_and_report_caches_are_independent(session_factory, story) -> None:
    run(session_factory, story, FakeLLM())
    fake = FakeLLM()
    assert run(session_factory, story, fake).status == "cached" and fake.calls == 0
    assert run(session_factory, story, fake, kind="report").status == "ok" and fake.calls == 1
    assert run(session_factory, story, fake, kind="report").status == "cached"
    [b] = rows(session_factory, story, "brief")
    [r] = rows(session_factory, story, "report")
    assert b.input_hash != r.input_hash


# --- queue ------------------------------------------------------------------------------


def worker(session_factory, fake, **kwargs) -> SummaryWorker:
    return SummaryWorker(session_factory, summarizer(fake), **kwargs)


def test_briefs_run_before_reports(session_factory, story) -> None:
    w = worker(session_factory, FakeLLM())
    w.enqueue([story], kind="report")
    w.enqueue([story], kind="brief")
    result = asyncio.run(w.run(manual=True))
    assert [o.kind for o in result.outcomes] == ["brief", "report"]
    assert result.ok == 2 and result.queued == 0


def test_debounce_is_per_kind(session_factory, story) -> None:
    w = worker(session_factory, FakeLLM())
    w.enqueue([story], kind="brief")
    asyncio.run(w.run(manual=True))
    w.enqueue([story], kind="brief")
    w.enqueue([story], kind="report")
    with session_factory() as session:
        jobs = {j.kind: j for j in session.scalars(select(SummaryJob))}
    assert jobs["brief"].next_attempt_at is not None  # waits for the debounce
    assert jobs["report"].next_attempt_at is None


# --- migration --------------------------------------------------------------------------


def test_pre_report_db_is_migrated(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_story_summaries_kind"))
        conn.execute(text("ALTER TABLE story_summaries DROP COLUMN kind"))
        conn.execute(text("ALTER TABLE story_summaries DROP COLUMN content_json"))
        conn.execute(text("DROP TABLE summary_jobs"))
        conn.execute(
            text(
                "CREATE TABLE summary_jobs (story_id INTEGER PRIMARY KEY, queued_at DATETIME,"
                " force BOOLEAN, attempts INTEGER, next_attempt_at DATETIME, last_error TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO story_summaries (story_id, version, text, citations_json,"
                " article_ids, basis, model, prompt_version, input_hash, status, created_at)"
                " VALUES (7, 1, 'Old summary.', '[[1]]', '[1]', 'full_text', 'm', 'summary-v1',"
                " 'h', 'ok', '2026-10-01 00:00:00')"
            )
        )
        conn.execute(
            text("INSERT INTO summary_jobs VALUES (7, '2026-10-01 00:00:00', 0, 1, NULL, 'boom')")
        )
    init_db(engine)
    init_db(engine)  # idempotent
    with make_session_factory(engine)() as session:
        [row] = session.scalars(select(StorySummary))
        assert row.kind == "brief" and row.content_json is None and row.text == "Old summary."
        [job] = session.scalars(select(SummaryJob))
        assert (job.story_id, job.kind, job.attempts, job.last_error) == (7, "brief", 1, "boom")
    engine.dispose()


# --- API and UI (demo with the fake LLM) ------------------------------------------------


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    build = build_demo(tmp_path_factory.mktemp("reports") / "demo.db")
    with TestClient(create_app(build.settings, llm_transport=build.llm_transport)) as c:
        c.build = build
        yield c


def _feed(client) -> list[dict]:
    return client.get("/api/profiles/personal-reader/feed?limit=100").json()["items"]


def test_api_brief_report_and_deprecated_summary(demo) -> None:
    item = next(
        i
        for i in _feed(demo)
        if i["source_count"] >= 2
        and i["report"]
        and i["report"]["status"] == "ok"
        and i["brief"]["style"] == "brief"
    )
    brief_, report = item["brief"], item["report"]
    assert set(brief_) == {
        "lead",
        "lead_citations",
        "bullets",
        "status",
        "style",
        "basis",
        "model",
        "generated_at",
        "sources",
    }
    assert len(brief_["bullets"]) == 3 and brief_["model"] == "fake-llm"
    assert set(report) == {"paragraphs", "status", "model", "generated_at", "sources"}
    assert 3 <= len(report["paragraphs"]) <= 5
    story = demo.get(f"/api/stories/{item['id']}").json()
    assert story["brief"] == brief_ and story["report"] == report
    summary = story["summary"]  # deprecated: the brief's lead and bullets joined
    assert (
        summary["text"].startswith(brief_["lead"])
        and brief_["bullets"][2]["text"] in (summary["text"])
    )
    members = {m["url"]: m for m in story["members"]}
    cited = [c for p in report["paragraphs"] for c in p["citations"]]
    cited += [c for b in brief_["bullets"] for c in b["citations"]] + brief_["lead_citations"]
    numbered = {c["index"]: c for c in report["sources"]}
    for c in cited:
        assert c["url"] in members and numbered[c["index"]] == c  # raw Article.url
    for n, c in enumerate(brief_["sources"], 1):
        assert c["index"] == n and numbered[n] == c  # [n] is the same article everywhere


def test_story_page_states(demo) -> None:
    feed = _feed(demo)
    ok = next(i for i in feed if i["report"] and i["report"]["status"] == "ok")
    page = demo.get(f"/story/{ok['id']}?p=personal-reader").text
    assert 'data-report="ok"' in page and "data-report-paragraph" in page
    assert "data-regenerate" in page and 'id="source-1"' in page and "Written by fake-llm" in page
    assert page.index("data-brief-lead") < page.index('id="report"') < page.index("source-1")

    failed = next(i for i in feed if i["report"] and i["report"]["status"] == "failed")
    page = demo.get(f"/story/{failed['id']}").text
    assert 'data-report="failed"' in page and "data-retry" in page
    assert "data-report-paragraph" not in page  # no lesser substitute
    partial = demo.post(f"/story/{failed['id']}/regenerate?kind=report").text
    assert "Generating detailed report" in partial and "every" not in partial
    _wait_for_report(demo, failed["id"], "ok")

    single = next(i for i in feed if i["source_count"] == 1 and i["report"] is None)
    demo.post(f"/api/admin/summarize?story_id={single['id']}&kind=report")
    page = demo.get(f"/story/{single['id']}").text
    assert "Report unavailable (not enough source text)" in page


def _wait_for_report(client, story_id: int, status: str) -> dict:
    for _ in range(100):
        report = client.get(f"/api/stories/{story_id}").json()["report"]
        if report and report["status"] == status:
            return report
        time.sleep(0.05)
    raise AssertionError(f"report of story {story_id} never became {status}")


def test_card_styles(demo) -> None:
    html = demo.get("/p/personal-reader").text
    assert "data-brief-lead" in html and "data-brief-bullets" in html
    assert 'data-citation="1"' in html and "Feed snippet" in html
    lead_only = demo.get("/p/personal-reader?cards=lead_only").text
    assert "data-brief-lead" in lead_only and "data-brief-bullets" not in lead_only


def test_on_demand_report_from_story_page(tmp_path) -> None:
    build = build_demo(tmp_path / "demo.db", report_mode="on_demand", fake_delay=0.3)
    with TestClient(create_app(build.settings, llm_transport=build.llm_transport)) as c:
        item = next(i for i in _feed(c) if i["source_count"] >= 2)
        assert item["report"] is None and item["brief"]
        page = c.get(f"/story/{item['id']}").text
        assert "data-report-pending" in page and "Generating detailed report" in page
        assert 'hx-trigger="load delay:3s"' in page
        poll = c.get(f"/story/{item['id']}/report?polls=1")
        assert poll.status_code == 200 and "data-report-pending" in poll.text
        timeout = c.get(f"/story/{item['id']}/report?polls=999")
        assert "data-report-timeout" in timeout.text or timeout.headers.get("HX-Refresh")
        _wait_for_report(c, item["id"], "ok")
        done = c.get(f"/story/{item['id']}/report?polls=2")
        assert done.headers.get("HX-Refresh") == "true"


def test_report_mode_off(tmp_path) -> None:
    build = build_demo(tmp_path / "demo.db", report_mode="off")
    with TestClient(create_app(build.settings, llm_transport=build.llm_transport)) as c:
        item = next(i for i in _feed(c) if i["source_count"] >= 2)
        page = c.get(f"/story/{item['id']}").text
        assert 'id="report"' not in page and item["report"] is None
