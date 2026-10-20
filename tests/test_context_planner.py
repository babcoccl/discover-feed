"""Planner-mode context (``context.query_mode: planner``): the LLM task planner, the
deterministic retrieval recipes, verified extraction, the composer, the per-story
trace, and the throughput knobs. Fakes: a stub LLM transport answering by
``json_schema.name``, FakeSearch and fake pages from ``test_context``."""

import asyncio
import json
from datetime import timedelta

import httpx
from sqlalchemy import select

from app.config import ContextConfig, LLMRole, SearchProviderConfig
from app.context import catalog
from app.context.compose import DetailItem, validate_composed
from app.context.extract import (
    Extraction,
    numbers_in_text,
    quote_in_text,
    subject_attribution,
    verify,
)
from app.context.planner import ContextTask, fallback_tasks, parse_tasks
from app.models import ContextTrace, StorySummary
from tests.test_context import (
    HITS,
    NOW,
    FakeSearch,
    context_articles,
    make_finder,
    no_sleep,
    page,
    seed,
)

SUBJECTS = ["Mayor Ortiz", "Springfield", "electric buses"]
FIRST = NOW - timedelta(hours=2)


def planner_cfg(**kw) -> ContextConfig:
    base = {
        "enabled": True,
        "providers": [SearchProviderConfig(name="tinyfish", min_interval_seconds=0)],
        "min_publishers": 1,
        "min_words": 0,
    }
    return ContextConfig(**{**base, **kw})


def task(kind: str, subject: str = "Mayor Ortiz") -> ContextTask:
    return ContextTask(type=kind, subject=subject, question="What should readers know?")


def recipes(t: ContextTask, cfg: ContextConfig | None = None) -> catalog.TaskQueries:
    return catalog.recipes(
        t,
        terms=["Springfield", "electric buses", "Mayor Ortiz"],
        own_domains=("news.example", "wire.example"),
        first_at=FIRST,
        now=NOW,
        cfg=cfg or planner_cfg(),
    )


# --- planner parsing ---------------------------------------------------------------


def test_parse_tasks_drops_invalid_types_and_subjects():
    out = parse_tasks(
        {
            "tasks": [
                {"type": "history", "subject": "Mayor Ortiz", "question": "q", "priority": 1},
                {"type": "gossip", "subject": "Mayor Ortiz", "question": "q", "priority": 1},
                {"type": "quotes", "subject": "Nobody Here", "question": "q", "priority": 2},
                {"type": "quotes", "subject": "Mayor Ortiz", "question": "q", "priority": 2},
            ]
        },
        max_tasks=3,
        known_subjects=SUBJECTS,
    )
    assert [(t.type, t.subject) for t in out] == [
        ("history", "Mayor Ortiz"),
        ("quotes", "Mayor Ortiz"),
    ]


def test_parse_tasks_caps_and_dedupes():
    out = parse_tasks(
        {
            "tasks": [
                {"type": "history", "subject": "Mayor Ortiz", "question": "a", "priority": 2},
                {"type": "history", "subject": "Mayor Ortiz", "question": "b", "priority": 1},
                {"type": "quotes", "subject": "Mayor Ortiz", "question": "c", "priority": 3},
                {"type": "financials", "subject": "Springfield", "question": "d", "priority": 1},
            ]
        },
        max_tasks=2,
        known_subjects=SUBJECTS,
    )
    assert len(out) == 2 and out[0].priority == 1


def test_fallback_tasks_cover_top_entity():
    tasks = fallback_tasks(["Mayor Ortiz"])
    assert [t.type for t in tasks] == ["history", "related_developments"]
    assert all(t.subject == "Mayor Ortiz" for t in tasks)


# --- retrieval recipes ---------------------------------------------------------------


def test_history_recipe_windows_filters_and_order():
    queries = recipes(task("history")).queries
    news = [q for q in queries if q.domain_type == "news"]
    web = [q for q in queries if q.domain_type == "web"]
    assert len(news) == 3 and len(web) == 3
    assert queries[:3] == news  # news first, then web
    recent, earlier, long_ago = news
    assert recent.before == FIRST - timedelta(days=3)
    assert recent.after == FIRST - timedelta(days=30)
    assert earlier.after == FIRST - timedelta(days=180)
    assert long_ago.before == FIRST - timedelta(days=180)
    assert all(q.exclude == ("news.example", "wire.example") for q in news)
    assert "Mayor Ortiz" in recent.query


def test_quotes_financials_related_recipes():
    q = recipes(task("quotes")).queries[0]
    assert q.query.startswith('"Mayor Ortiz"') and q.domain_type == "news"
    assert q.after == NOW - timedelta(days=90)
    f = recipes(task("financials", "Globex")).queries[0]
    assert '"Globex"' in f.query and "funding" in f.query
    assert f.after == NOW - timedelta(days=180)
    r = recipes(task("related_developments")).queries[0]
    assert r.after == NOW - timedelta(days=30)
    assert r.exclude == ("news.example", "wire.example")


def test_background_recipe_web_and_research():
    q = recipes(task("background")).queries[0]
    assert q.domain_type == "web" and q.after is None
    cfg = planner_cfg(actions={"background": {"research": True}})
    q = recipes(task("background"), cfg).queries[0]
    assert q.domain_type == "research_paper"
    assert q.pub_year_max == NOW.year and q.pub_year_min == NOW.year - 10


def test_archive_off_drops_older_windows():
    queries = recipes(task("history"), planner_cfg(archive=False)).queries
    assert all(q.window == "recent" for q in queries)


def test_action_disables_a_type():
    cfg = planner_cfg(actions={"history": {"enabled": False}})
    assert recipes(task("history"), cfg).queries == []


# --- extraction verification ----------------------------------------------------------

PAGE = (
    "Mayor Ortiz said the fleet will cost 12 million dollars.\n\n"
    "Ortiz told reporters the purchase was approved last year."
)
FIRST_AT = NOW - timedelta(days=1)


def extraction(**kw) -> Extraction:
    base = {
        "relevant": True,
        "kind": "history",
        "fact": "Ortiz said the fleet cost 12 million dollars",
        "quote": "Ortiz told reporters the purchase was approved last year",
        "speaker": "Ortiz",
        "date_from_page": "",
    }
    return Extraction(**{**base, **kw})


def check(ex, page_text=PAGE, date=None, **kw):
    return verify(
        ex,
        task_type=kw.get("task_type", "history"),
        subject=kw.get("subject", "Ortiz"),
        page_text=page_text,
        page_date=date if date is not None else FIRST_AT - timedelta(days=10),
        first_at=FIRST_AT,
        min_age_days=kw.get("min_age_days", 3),
        now=NOW,
    )


def test_quote_must_be_verbatim():
    assert quote_in_text("Ortiz told reporters the purchase was approved last year", PAGE)
    ok, _ = check(extraction())
    assert ok
    ok, reason = check(extraction(quote="the mayor said something different"))
    assert not ok and reason == "quote_not_verbatim"


def test_fact_numbers_must_be_on_page():
    assert numbers_in_text("the fleet cost 12 million", PAGE)
    ok, reason = check(extraction(fact="the fleet cost 99 million"))
    assert not ok and reason == "numbers_not_on_page"


def test_subject_attribution():
    verbatim = "Ortiz told reporters the purchase was approved last year"
    assert subject_attribution("Ortiz", verbatim, PAGE) == "paragraph"
    # "reporters" is only on the page's second paragraph, not the quote's one
    assert (
        subject_attribution("reporters", "the fleet will cost 12 million dollars", PAGE) == "page"
    )
    ok, reason = check(extraction(), subject="Nobody")
    assert not ok and reason == "subject_absent"


def test_undated_rejected_and_min_age():
    ok, reason = check(extraction(), date=None)
    # page_date None -> falls back to date_from_page (empty) -> undated
    ok, reason = verify(
        extraction(),
        task_type="history",
        subject="Ortiz",
        page_text=PAGE,
        page_date=None,
        first_at=FIRST_AT,
        min_age_days=3,
        now=NOW,
    )
    assert not ok and reason == "undated"
    ok, reason = verify(
        extraction(),
        task_type="history",
        subject="Ortiz",
        page_text=PAGE,
        page_date=FIRST_AT - timedelta(days=1),
        first_at=FIRST_AT,
        min_age_days=3,
        now=NOW,
    )
    assert not ok and reason == "too_recent"
    ok, reason = verify(
        extraction(kind="background"),
        task_type="background",
        subject="Ortiz",
        page_text=PAGE,
        page_date=None,
        first_at=FIRST_AT,
        min_age_days=3,
        now=NOW,
    )
    assert ok, reason  # background tolerates undated pages


def test_kind_mismatch_and_caps():
    ok, reason = check(extraction(kind="quotes"))
    assert not ok and reason == "kind_mismatch"
    ok, reason = check(extraction(quote=" ".join(["w"] * 30)))
    assert not ok and reason == "bad_quote"


# --- compose validation -----------------------------------------------------------------


def item(iid, **kw):
    base = {
        "task_type": "history",
        "subject": "Ortiz",
        "fact": "f",
        "quote": "q",
        "speaker": "",
        "date": None,
        "url": f"https://x.example/{iid}",
        "outlet": "x.example",
        "origin": "web",
    }
    return DetailItem(id=iid, **{**base, **kw})


def test_compose_notes_and_then_and_now():
    old = item(1, date=NOW - timedelta(days=100), quote="we will never buy buses")
    new = item(2, date=NOW - timedelta(days=1), quote="we approved the bus purchase")
    out = validate_composed(
        {
            "notes": [
                {"id": 1, "note": "a note"},
                {"id": 9, "note": "ghost"},
                {"id": 2, "note": " ".join(["x"] * 40)},
            ],
            "then_and_now": {
                "relation": "changed",
                "summary": "Ortiz moved from opposing to approving the purchase",
                "references": [1, 2],
            },
        },
        [old, new],
    )
    assert out.notes == {1: "a note"}  # unknown id + >30w note dropped
    assert out.then_and_now["relation"] == "changed" and out.then_and_now["show"]


def test_compose_then_and_now_rules():
    items = [item(1, date=NOW - timedelta(days=100)), item(2, date=NOW - timedelta(days=1))]
    # consistent -> timeline only, no box
    out = validate_composed(
        {
            "notes": [],
            "then_and_now": {"relation": "consistent", "summary": "same", "references": [1, 2]},
        },
        items,
    )
    assert out.then_and_now is not None and not out.then_and_now["show"]
    # loaded word not present in the cited quotes -> dropped entirely
    out = validate_composed(
        {
            "notes": [],
            "then_and_now": {
                "relation": "changed",
                "summary": "a complete reversal of position",
                "references": [1, 2],
            },
        },
        items,
    )
    assert out.then_and_now is None
    # fewer than two verified dated items -> no comparison
    out = validate_composed(
        {
            "notes": [],
            "then_and_now": {"relation": "changed", "summary": "x moved", "references": [1, 9]},
        },
        [item(1, date=NOW - timedelta(days=5)), item(2)],
    )
    assert out.then_and_now is None


# --- end-to-end planner run ---------------------------------------------------------------

QUOTE = "Springfield studied electric buses for years before the vote"
PAGE_TEXT = f"Mayor Ortiz has a long history here. {QUOTE}. " + " ".join(
    ["Background reading about transit in the city."] * 20
)


def _llm_transport():
    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        name = (body.get("response_format") or {}).get("json_schema", {}).get("name")
        content = {
            "context_planner": {
                "tasks": [
                    {
                        "type": "history",
                        "subject": "Ortiz",
                        "question": "What led to the vote?",
                        "priority": 1,
                    },
                ]
            },
            "context_extract": {
                "relevant": True,
                "kind": "history",
                "fact": "Springfield studied electric buses for years",
                "evidence_quote": QUOTE,
                "quote_speaker": "",
                "date_from_page": "2025-01-01",
            },
            "context_compose": {
                "notes": [{"id": 1, "note": "The city weighed buses for years."}],
            },
        }.get(name, {})
        return httpx.Response(
            200,
            json={
                "model": "fake",
                "choices": [{"message": {"role": "assistant", "content": json.dumps(content)}}],
            },
        )

    return httpx.MockTransport(answer)


def test_planner_run_stores_detail_and_trace(session_factory, tmp_path):
    hits = [
        (
            f"https://archives.example/{n}",
            "Springfield transit history",
            (NOW - timedelta(days=40)).isoformat(),
        )
        for n in range(1, 4)
    ]
    fake = FakeSearch(hits=hits)
    role = LLMRole(base_url="http://llm.example/v1", model="fake")
    from app.enrich.fetch import CandidateFetcher
    from app.images.safety import Guard
    from tests.test_context import resolver

    def transport():
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                200,
                content=page("Archive piece", PAGE_TEXT),
                headers={"content-type": "text/html"},
            )

        return httpx.MockTransport(handle)

    fetcher = CandidateFetcher(
        session_factory, guard=Guard(resolver), domain_delay=0, transport=transport()
    )
    story_id = seed(session_factory, publishers=2)
    finder = make_finder(
        session_factory,
        fake,
        cfg=planner_cfg(),
        llm_role=role,
        llm_transport=_llm_transport(),
        sleep=no_sleep,
    )
    finder.fetcher = fetcher
    result = asyncio.run(finder.find_story(story_id))
    assert result.status == "done", result.reason
    accepted = result.accepted
    assert accepted and all(c.fact and c.quote for c in accepted)
    items = context_articles(session_factory, story_id)
    assert items and all(i.context_task == "history" for i in items)
    assert all(i.context_origin == "archive" for i in items)
    with session_factory() as session:
        detail = session.scalar(
            select(StorySummary).where(
                StorySummary.story_id == story_id, StorySummary.kind == "context"
            )
        )
        assert detail is not None and detail.content_json["items"]
        trace = session.scalar(select(ContextTrace).where(ContextTrace.story_id == story_id))
        assert trace is not None
        assert trace.trace["planner"]["tasks"][0]["type"] == "history"
        assert trace.trace["searches"] and trace.trace["timings"]
        stats = finder.stats(session)
        assert stats["timings"]


def test_planner_fallback_when_llm_fails(session_factory):
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "down"}})

    role = LLMRole(base_url="http://llm.example/v1", model="fake")
    story_id = seed(session_factory, publishers=2)
    finder = make_finder(
        session_factory,
        FakeSearch(hits=HITS),
        cfg=planner_cfg(),
        llm_role=role,
        llm_transport=httpx.MockTransport(bad),
        sleep=no_sleep,
    )
    result = asyncio.run(finder.find_story(story_id, dry_run=True))
    assert result.planner["fallback"] is True
    assert [t["type"] for t in result.planner["tasks"]] == [
        "history",
        "related_developments",
    ]
