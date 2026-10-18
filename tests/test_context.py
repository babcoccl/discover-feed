"""The background-and-context finder: angles, filters, judge, scheduling and the
``origin="context"`` exclusions. All fakes — no real search, fetches or LLM calls."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.cluster import TfidfClusterer
from app.cluster.service import refresh_stories, run_clustering
from app.config import ContextConfig, EnrichmentConfig, LLMRole, SearchProviderConfig
from app.context.angles import Angle, heuristic_angles, make_angles, parse_angles
from app.context.filters import StoryContext, context_pre_filter, context_text_check
from app.context.service import ContextFinder
from app.enrich.fetch import CandidateFetcher
from app.enrich.query import story_terms
from app.images.safety import Guard
from app.ingest import usage_periods
from app.lifecycle import ensure_stories
from app.models import (
    CONTEXT,
    Article,
    ContextCandidate,
    ProfileRecord,
    SourceRecord,
    SourceUsage,
    Story,
    StoryState,
    StoryView,
)
from app.profiles import get_story
from app.search.base import SearchHit
from app.search.service import SearchService

NOW = datetime.now(UTC).replace(microsecond=0)
TEXT = (
    "Mayor Lena Ortiz of Springfield announced on Monday that the Springfield transit "
    "authority will buy forty electric buses and rebuild Central Station. The council "
    "approved the plan after three months of debate about fares and depots. "
) * 6
BACKGROUND = (
    "Springfield's transit authority has debated electric buses for a decade. A 2014 "
    "pilot ran two battery buses on Route 7 for eighteen months. The depots downtown "
    "date to a 1960s expansion and the council studied fares three times since 2019. "
    "Rail historians note the station opened in 1912 under a different operator. "
) * 8
UNRELATED = (
    "The national football league schedule was released with several prime time games "
    "and a new international series in three countries next autumn for every franchise. "
) * 6
SHORT = "Springfield transit history. " * 10
# Same event retold with background vocabulary: similarity ~0.51 — accepted under the
# old 0.60 ceiling, rejected at 0.45.
COVERAGE_BLEND = (
    "Springfield's council approved the electric bus purchase and the Central Station "
    "rebuild on Monday. The transit authority has weighed electric buses since a 2014 "
    "pilot on Route 7, and the depots date to a 1960s expansion. Mayor Lena Ortiz said "
    "fares will be studied again next year after three council reviews since 2019. "
) * 6
BACKGROUND2 = (
    "Long before this week's vote, Springfield officials argued over whether the "
    "transit authority should electrify its fleet at all. A 2011 feasibility study "
    "estimated depot upgrades at several million dollars, and council members twice "
    "delayed a bus contract over fare revenue. The Central Station terminal, built "
    "in 1967, has been rebuilt in stages for decades. "
) * 6
BACKGROUND3 = (
    "Electric bus programs elsewhere offer a preview of what Springfield may face: "
    "battery range limits, charging windows between rush periods, and depot retrofits. "
    "Transit authorities in comparable cities reported fare shortfalls in early years, "
    "though councils there say ridership recovered once routes stabilized. "
) * 8


def resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


def page(title: str, body: str) -> bytes:
    paras = "".join(f"<p>{s}.</p>" for s in body.split(". ") if s)
    return (
        f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>"
        f"{paras}</article></body></html>"
    ).encode()


PAGES = {
    "https://wikipedia.example/transit-history": page(
        "A history of Springfield transit", BACKGROUND
    ),
    "https://copy.example/same-event": page("Springfield buses approved", TEXT),
    "https://sports.example/nfl": page("Football schedule released", UNRELATED),
    "https://tiny.example/short": page("Springfield transit", SHORT),
}


def page_transport() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        url = str(request.url)
        if url in PAGES:
            return httpx.Response(200, content=PAGES[url], headers={"content-type": "text/html"})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


HITS = [
    ("https://news.example/1", "Springfield buses approved by council"),  # same domain
    ("https://wikipedia.example/transit-history", "A history of Springfield transit"),
    ("https://copy.example/same-event", "Springfield buses approved"),
    ("https://sports.example/nfl", "Football schedule released"),
    ("https://tiny.example/short", "Springfield transit history brief"),
]


class FakeSearch:
    def __init__(self, hits=HITS, *, status: int = 200, text: str | None = None):
        self.hits, self.status, self.text = hits, status, text
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        body = {
            "query": request.url.params.get("query"),
            "results": [
                {
                    "position": n,
                    "site_name": urlsplit(u).hostname,
                    "title": t,
                    "snippet": t,
                    "url": u,
                    "date": NOW.isoformat(),
                }
                for n, (u, t) in enumerate(self.hits, start=1)
            ],
            "total_results": len(self.hits),
            "page": 0,
        }
        return httpx.Response(self.status, json=body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


async def no_sleep(_seconds: float) -> None:
    return None


def config(**kw) -> ContextConfig:
    base = {
        "enabled": True,
        "providers": [SearchProviderConfig(name="tinyfish", min_interval_seconds=0)],
        "similarity_floor": 0.05,
        "similarity_ceiling": 0.7,
        "min_publishers": 1,
    }
    return ContextConfig(**{**base, **kw})


def make_finder(
    factory, fake: FakeSearch | None = None, cfg: ContextConfig | None = None, **kw
) -> ContextFinder:
    fetcher = CandidateFetcher(
        factory, guard=Guard(resolver), domain_delay=0, transport=page_transport()
    )
    search = SearchService(factory, transport=(fake or FakeSearch()).transport(), sleep=no_sleep)
    return ContextFinder(
        factory,
        cfg or config(),
        enrichment=EnrichmentConfig(),
        profile_defaults={"me": True},
        search=search,
        fetcher=fetcher,
        **kw,
    )


def seed(
    factory,
    *,
    publishers: int = 2,
    priority: int = 0,
    published: bool = True,
    headline: str | None = None,
) -> int:
    """A clustered story from sources on two domains, profile ``me`` follows ``a``."""
    with factory() as session:
        for sid_, name, url in (
            ("a", "News A", "https://news.example/feed"),
            ("b", "Wire B", "https://wire.example/feed"),
        ):
            if session.get(SourceRecord, sid_) is None:
                session.add(SourceRecord(id=sid_, name=name, url=url))
        profile = session.scalar(select(ProfileRecord).where(ProfileRecord.slug == "me"))
        if profile is None:
            profile = ProfileRecord(slug="me", name="Me")
            session.add(profile)
            session.flush()
        profile.sources = [session.get(SourceRecord, "a")]
        offset = len(list(session.scalars(select(Article))))
        for n in range(1, publishers + 1):
            sid = "a" if n == 1 else "b"
            session.add(
                Article(
                    source_id=sid,
                    url=f"https://{'news' if n == 1 else 'wire'}.example/{offset + n}",
                    canonical_url=f"https://{'news' if n == 1 else 'wire'}.example/{offset + n}",
                    title=(
                        headline or "Springfield mayor Ortiz wins council vote on electric buses"
                    )
                    if n == 1
                    else "Springfield council approves electric bus purchase",
                    summary_raw="Springfield buses",
                    published_at=NOW - timedelta(hours=1),
                    fetched_at=NOW,
                    content_hash=hashlib.sha256(f"m{offset + n}".encode()).hexdigest(),
                    text=TEXT,
                    text_status="ok",
                    word_count=len(TEXT.split()),
                )
            )
        session.flush()
        ensure_stories(session, NOW)
        session.commit()
    run_clustering(factory, TfidfClusterer())
    with factory() as session:
        newest = session.scalar(select(Article).order_by(Article.id.desc()))
        story = session.get(Story, newest.story_id)
        assert story is not None and story.state != "merged"
        story.priority = priority
        members = list(
            session.scalars(
                select(Article).where(Article.story_id == story.id).order_by(Article.id)
            )
        )
        if published:
            story.state, story.published_version = StoryState.PUBLISHED.value, 1
            if (
                session.scalar(
                    select(StoryView).where(StoryView.story_id == story.id, StoryView.version == 1)
                )
                is None
            ):
                session.add(
                    StoryView(
                        story_id=story.id,
                        version=1,
                        schema_version=1,
                        state="published",
                        published_at=NOW,
                        updated_at=NOW,
                        doc={
                            "story_id": story.id,
                            "title": story.title,
                            "representative_id": members[0].id,
                            "members": [
                                {
                                    "id": m.id,
                                    "source_id": m.source_id,
                                    "source": m.source_id,
                                    "title": m.title,
                                    "url": m.url,
                                    "published_at": m.published_at.isoformat(),
                                    "origin": m.origin,
                                }
                                for m in members
                            ],
                            "brief": {
                                "style": "brief",
                                "lead": [{"text": story.title, "citations": []}],
                                "bullets": [],
                                "citations": [],
                                "basis": "full_text",
                                "model": "m",
                                "generated_at": NOW.isoformat(),
                                "status": "ok",
                            },
                            "report": {"state": "skipped"},
                            "model": "m",
                            "generated_at": NOW.isoformat(),
                            "source_count": len({m.source_id for m in members}),
                        },
                    )
                )
        session.commit()
        return story.id


def run(finder: ContextFinder, story_id: int):
    finder.schedule([story_id], force=True)
    finder._thread.join(30)
    return finder.last_run


def context_articles(factory, story_id: int) -> list[Article]:
    with factory() as session:
        return list(
            session.scalars(
                select(Article).where(Article.story_id == story_id, Article.origin == CONTEXT)
            )
        )


# --- angles ------------------------------------------------------------------------


def test_heuristic_angles_pair_top_entity_with_context_words():
    terms = story_terms(["Springfield mayor Ortiz wins electric bus vote"])
    angles = heuristic_angles(terms, max_angles=3)
    assert angles and all(a.heuristic for a in angles)
    assert all('"' in a.query for a in angles)  # the entity is quoted
    assert {a.label for a in angles} == {"history", "explained", "background"}


def test_parse_angles_rejects_malformed():
    assert parse_angles("not json", max_angles=3) == []
    assert parse_angles('{"angles": "x"}', max_angles=3) == []
    assert parse_angles('{"angles": [{"label": "history"}]}', max_angles=3) == []
    got = parse_angles(
        '{"angles": [{"label": "history", "query": "\\"x\\" history"}]}', max_angles=3
    )
    assert got == [Angle(label="history", query='"x" history')]


def test_llm_angles_used_when_parseable():
    role = LLMRole(base_url="http://llm.test/v1", model="m")

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"angles": [{"label": "opposing view", '
                                '"query": "\\"Springfield transit authority\\" critic fares"}]}'
                            )
                        }
                    }
                ]
            },
        )

    result = asyncio.run(
        make_angles(
            "Springfield buses",
            "The council approved the plan.",
            story_terms(["Springfield mayor Ortiz wins electric bus vote"]),
            max_angles=3,
            providers=["tinyfish"],
            llm_role=role,
            llm_transport=httpx.MockTransport(answer),
        )
    )
    assert [a.label for a in result] == ["opposing view"]
    assert not result[0].heuristic


def test_llm_angles_fall_back_when_unparseable():
    role = LLMRole(base_url="http://llm.test/v1", model="m")

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "nope"}}]})

    result = asyncio.run(
        make_angles(
            "Springfield buses",
            "",
            story_terms(["Springfield mayor Ortiz wins electric bus vote"]),
            max_angles=2,
            providers=["tinyfish"],
            llm_role=role,
            llm_transport=httpx.MockTransport(answer),
        )
    )
    assert all(a.heuristic for a in result) and len(result) == 2


# --- filters -------------------------------------------------------------------------


def story_ctx() -> StoryContext:
    return StoryContext(
        title="Springfield mayor Ortiz wins council vote on electric buses",
        headlines=["Springfield mayor Ortiz wins council vote on electric buses"],
        texts=[TEXT],
        domains={"news.example", "wire.example"},
        canonical_urls={"https://news.example/1", "https://wire.example/2"},
        first_at=NOW - timedelta(hours=1),
        last_at=NOW,
        language="en",
    )


def hit(url: str, title: str = "x") -> SearchHit:
    return SearchHit(url=url, title=title)


def test_pre_filter_rejects_same_domain_social_deny_and_ingested():
    story = story_ctx()
    deny = ["blocked.example"]
    assert (
        context_pre_filter(hit("https://news.example/other"), story, deny=deny, language="en")
        == "same_domain"
    )
    assert (
        context_pre_filter(
            hit("https://twitter.com/x/1"),
            story,
            deny=deny,
            language="en",
            social=["twitter.com"],
        )
        == "social"
    )
    assert (
        context_pre_filter(hit("https://blocked.example/x"), story, deny=deny, language="en")
        == "denied"
    )
    assert (
        context_pre_filter(hit("https://news.example/1"), story, deny=deny, language="en")
        == "same_domain"
    )
    dupe = StoryContext(
        title="T",
        headlines=[],
        texts=[],
        domains=set(),
        canonical_urls={"https://seen.example/dup"},
        first_at=NOW,
        last_at=NOW,
        language="en",
    )
    assert (
        context_pre_filter(
            hit("https://seen.example/dup?utm_source=x"), dupe, deny=deny, language="en"
        )
        == "duplicate_url"
    )
    h = hit("https://ok.example/x")
    h.language = "fr"
    assert context_pre_filter(h, story, deny=deny, language="en") == "language"
    assert context_pre_filter(hit("ftp://x"), story, deny=deny, language="en") == "bad_url"


def test_text_check_band():
    story = story_ctx()
    # identical coverage -> same_event
    v = context_text_check(
        "Springfield buses approved",
        TEXT,
        story,
        floor=0.05,
        ceiling=0.7,
        min_entities=1,
        duplicate_overlap=0.6,
    )
    assert v.reason in ("near_duplicate", "same_event")
    # unrelated -> off_topic
    v = context_text_check(
        "Football schedule released",
        UNRELATED,
        story,
        floor=0.05,
        ceiling=0.7,
        min_entities=1,
        duplicate_overlap=0.6,
    )
    assert v.reason in ("off_topic", "few_shared_entities")
    # background page -> kept
    v = context_text_check(
        "A history of Springfield transit",
        BACKGROUND,
        story,
        floor=0.05,
        ceiling=0.7,
        min_entities=1,
        duplicate_overlap=0.6,
    )
    assert v.reason is None
    assert 0.05 <= v.score < 0.7 and v.entities >= 1
    # heuristic strictness: 1 shared entity fails when min is 2
    solo = "The Springfield library's summer reading program opens next week. " * 8
    v = context_text_check(
        "Library program",
        solo,
        story,
        floor=0.01,
        ceiling=0.9,
        min_entities=2,
        duplicate_overlap=0.6,
    )
    assert v.reason == "few_shared_entities" and v.entities == 1


def test_lowered_ceiling_separates_same_event_from_background():
    """At the 0.45 ceiling, blended same-event coverage (~0.51) is rejected while a
    genuine backgrounder (~0.26, overlapping entities) is still accepted; under the old
    0.60 ceiling the blended page passed."""
    story = story_ctx()
    blend = context_text_check(
        "Council approves buses amid long debate",
        COVERAGE_BLEND,
        story,
        floor=0.05,
        ceiling=0.45,
        min_entities=1,
        duplicate_overlap=0.6,
    )
    assert blend.reason == "same_event" and blend.score >= 0.45 and blend.entities >= 1
    old = context_text_check(
        "Council approves buses amid long debate",
        COVERAGE_BLEND,
        story,
        floor=0.05,
        ceiling=0.60,
        min_entities=1,
        duplicate_overlap=0.6,
    )
    assert old.reason is None  # regression guard: the old ceiling let it through
    background = context_text_check(
        "A history of Springfield transit",
        BACKGROUND,
        story,
        floor=0.05,
        ceiling=0.45,
        min_entities=1,
        duplicate_overlap=0.6,
    )
    assert background.reason is None and background.entities >= 1


# --- the finder ------------------------------------------------------------------------


def test_find_story_adds_context_links_excluded_from_members(session_factory):
    story_id = seed(session_factory)
    finder = make_finder(session_factory)
    run(finder, story_id)
    links = context_articles(session_factory, story_id)
    assert len(links) == 1
    link = links[0]
    assert link.origin == CONTEXT
    assert link.url == "https://wikipedia.example/transit-history"
    assert link.context_angle and link.discovered_for_story_id == story_id
    assert link.anchor_article_id and link.source_id == "web:wikipedia.example"
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.context_status == "done"
        assert story.context_at is not None
        # excluded from the member list -> never reaches briefs/citations/counts
        item = get_story(session, story_id)
        assert all(m.origin != CONTEXT for m in item.members)
        assert len(item.members) == 2
        candidates = list(session.scalars(select(ContextCandidate)))
        assert len(candidates) == 5
        by_reason = {c.reason: c.decision for c in candidates}
        assert by_reason["same_domain"] == "rejected"


def test_counts_and_timestamps_ignore_context(session_factory):
    story_id = seed(session_factory)
    with session_factory() as session:
        before = session.get(Story, story_id)
        counts = (before.article_count, before.source_count, before.first_seen_at)
    run(make_finder(session_factory), story_id)
    # a context link far in the past must not move first_seen_at / last_updated_at
    with session_factory() as session:
        old = session.scalars(select(Article).where(Article.origin == CONTEXT)).first()
        old.published_at = NOW - timedelta(days=365 * 5)
        session.commit()
        refresh_stories(session, [story_id])
        after = session.get(Story, story_id)
        assert (after.article_count, after.source_count, after.first_seen_at) == counts


def test_once_per_story(session_factory):
    story_id = seed(session_factory)
    finder = make_finder(session_factory)
    run(finder, story_id)
    with session_factory() as session:
        assert story_id not in finder.eligible(session)
        assert finder.schedule() == 0  # done stories are not re-searched


def test_unpublished_story_not_eligible(session_factory):
    seed(session_factory, published=False)
    finder = make_finder(session_factory)
    with session_factory() as session:
        assert finder.eligible(session) == []


def test_priority_gate(session_factory):
    sid = seed(session_factory, publishers=1, priority=0)
    finder = make_finder(session_factory, cfg=config(min_publishers=2, min_priority=0))
    with session_factory() as session:
        assert sid not in finder.eligible(session)
        story = session.get(Story, sid)
        story.priority = 1
        session.commit()
    with session_factory() as session:
        assert sid in finder.eligible(session)


def test_publisher_gate(session_factory):
    """A low-priority story still qualifies once enough distinct publishers cover it."""
    sid = seed(session_factory, publishers=2, priority=0)
    finder = make_finder(session_factory, cfg=config(min_publishers=2, min_priority=0))
    with session_factory() as session:
        assert sid in finder.eligible(session)


def test_provider_failure_retries_never_done(session_factory):
    story_id = seed(session_factory)
    finder = make_finder(session_factory, FakeSearch(text="oops", status=500))
    run(finder, story_id)
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.context_status == "retry"
        assert story.context_attempts == 1
        assert story.context_next_attempt_at is not None
        assert not context_articles(session_factory, story_id)


def test_dry_run_writes_nothing(session_factory):
    story_id = seed(session_factory)
    finder = make_finder(session_factory)
    result = asyncio.run(finder.find_story(story_id, dry_run=True))
    assert result.accepted  # the pipeline ran, nothing was stored
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.context_status is None
        assert session.scalar(select(ContextCandidate).limit(1)) is None
    assert not context_articles(session_factory, story_id)


def test_max_items_cap(session_factory):
    story_id = seed(session_factory)
    finder = make_finder(
        session_factory,
        FakeSearch(
            hits=[
                ("https://wikipedia.example/transit-history", "Transit history"),
                ("https://w2.example/transit", "More transit"),
            ]
        ),
    )
    PAGES["https://w2.example/transit"] = page("More Springfield transit", BACKGROUND)
    try:
        finder.config = finder.config.model_copy(update={"max_items": 1})
        run(finder, story_id)
        links = context_articles(session_factory, story_id)
        assert len(links) == 1
    finally:
        del PAGES["https://w2.example/transit"]


def test_domain_cap_keeps_one_per_domain(session_factory):
    """``max_per_domain`` (default 1): the first judge-passing page on a domain wins;
    further accepted pages on it are rejected ``domain_cap``."""
    story_id = seed(session_factory)
    PAGES["https://deep.example/history-a"] = page("How Springfield got here", BACKGROUND2)
    PAGES["https://deep.example/history-b"] = page("A second backgrounder", BACKGROUND)
    PAGES["https://other.example/history-c"] = page("What other cities learned", BACKGROUND3)
    try:
        fake = FakeSearch(
            hits=[
                ("https://deep.example/history-a", "How Springfield got here"),
                ("https://deep.example/history-b", "A second backgrounder"),
                ("https://other.example/history-c", "What other cities learned"),
            ]
        )
        run(make_finder(session_factory, fake), story_id)
        links = context_articles(session_factory, story_id)
        assert sorted(a.url for a in links) == [
            "https://deep.example/history-a",
            "https://other.example/history-c",
        ]
        with session_factory() as session:
            capped = [
                c for c in session.scalars(select(ContextCandidate)) if c.reason == "domain_cap"
            ]
            assert [c.url for c in capped] == ["https://deep.example/history-b"]
    finally:
        for url in (
            "https://deep.example/history-a",
            "https://deep.example/history-b",
            "https://other.example/history-c",
        ):
            del PAGES[url]


def test_zero_hits_records_reason(session_factory):
    """A story whose searches all came back empty is ``done`` with ``context_reason``
    explaining the 0 candidates (``no_hits``), visible in stats/logs."""
    story_id = seed(session_factory)
    finder = make_finder(session_factory, FakeSearch(hits=[]))
    run(finder, story_id)
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.context_status == "done"
        assert story.context_reason == "no_hits"
    assert finder.last_run["zero_candidates"] == {"no_hits": 1}


def test_worker_waits_out_provider_cooldown(session_factory):
    """A cooldown already running pauses the pass until it ends instead of burning the
    story into ``retry`` (cooldowns are shared with enrichment via
    ``search_provider_state``)."""
    story_id = seed(session_factory)
    clock = {"t": NOW}

    def now() -> datetime:
        return clock["t"]

    async def sleep(seconds: float) -> None:
        clock["t"] += timedelta(seconds=seconds)

    finder = make_finder(session_factory, now=now, sleep=sleep)
    finder.search.now = now
    finder.search._cool_down("tinyfish", 30)
    run(finder, story_id)
    assert clock["t"] >= NOW + timedelta(seconds=30)
    assert context_articles(session_factory, story_id)


def test_worker_waits_for_enrichment(session_factory):
    """While the enrichment worker holds the providers the context pass waits, then
    proceeds — they share the same provider slots."""
    story_id = seed(session_factory)
    states = iter([True, True, False])
    finder = make_finder(session_factory, peer_running=lambda: next(states, False), sleep=no_sleep)
    run(finder, story_id)
    assert context_articles(session_factory, story_id)


def test_worker_pauses_when_capacity_never_frees(session_factory):
    """If the peer never finishes (or every provider stays cooling) the pass pauses
    without sending requests; stories stay ``pending`` for the next schedule."""
    story_id = seed(session_factory)
    fake = FakeSearch()
    finder = make_finder(session_factory, fake, peer_running=lambda: True, sleep=no_sleep)
    run(finder, story_id)
    assert not fake.requests  # nothing searched while the peer held the providers
    with session_factory() as session:
        assert session.get(Story, story_id).context_status == "pending"


# --- the judge -------------------------------------------------------------------------


def llm_response(content: str):
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.MockTransport(answer)


def role() -> LLMRole:
    return LLMRole(base_url="http://llm.test/v1", model="m")


def test_judge_scores_and_notes_links(session_factory):
    story_id = seed(session_factory)
    calls = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        content = (
            '{"angles": [{"label": "history", "query": "\\"Springfield transit\\" history"}]}'
            if len(calls) == 1
            else '{"adds_context": 2, "is_substantive_article": 1, '
            '"note": "Decade of transit history before this vote"}'
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    finder = make_finder(
        session_factory,
        llm_role=role(),
        llm_transport=httpx.MockTransport(answer),
    )
    run(finder, story_id)
    links = context_articles(session_factory, story_id)
    assert len(links) == 1
    assert links[0].context_note == "Decade of transit history before this vote"


def test_judge_rejects_non_articles(session_factory):
    """Link lists, landing/section pages and product pages are rejected as
    ``not_article`` even when the judge would otherwise score them 2."""
    story_id = seed(session_factory)

    def answer(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if '"angles"' in body:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"angles": [{"label": "history", '
                                    '"query": "\\"Springfield transit\\" history"}]}'
                                )
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"adds_context": 2, "is_substantive_article": 0, '
                                '"note": "Mostly a list of links"}'
                            )
                        }
                    }
                ]
            },
        )

    finder = make_finder(
        session_factory, llm_role=role(), llm_transport=httpx.MockTransport(answer)
    )
    run(finder, story_id)
    assert not context_articles(session_factory, story_id)
    with session_factory() as session:
        reasons = {
            c.reason for c in session.scalars(select(ContextCandidate)) if c.decision == "rejected"
        }
        assert "not_article" in reasons


def test_judge_rejects_same_event_and_unparseable(session_factory):
    story_id = seed(session_factory)

    def answer(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if "angles" in body or '"angles"' in body:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"angles": [{"label": "history", '
                                    '"query": "\\"Springfield transit\\" history"}]}'
                                )
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "garbage"}}]},
        )

    finder = make_finder(
        session_factory, llm_role=role(), llm_transport=httpx.MockTransport(answer)
    )
    run(finder, story_id)
    assert not context_articles(session_factory, story_id)  # unparsable -> rejected
    with session_factory() as session:
        reasons = {
            c.reason for c in session.scalars(select(ContextCandidate)) if c.decision == "rejected"
        }
        assert "judge_failed" in reasons


def test_context_budget_caps_requests(session_factory):
    story_id = seed(session_factory)
    fake = FakeSearch()
    finder = make_finder(session_factory, fake, cfg=config(daily_request_budget=1, max_angles=3))
    run(finder, story_id)
    assert len(fake.requests) == 1  # one angle search; the rest skipped by the budget


def test_context_budget_resets_each_utc_day(session_factory):
    """``context:<name>`` rows are keyed by UTC day (``usage_periods``): a budget
    spent yesterday does not block today's searches — only today's row counts."""
    story_id = seed(session_factory)
    fake = FakeSearch()
    finder = make_finder(session_factory, fake, cfg=config(daily_request_budget=1))
    yesterday, _month = usage_periods(NOW - timedelta(days=1))
    today, _ = usage_periods(NOW)
    with session_factory() as session:
        session.add(SourceUsage(source_id="context:tinyfish", period=yesterday, requests=99))
        session.commit()
    run(finder, story_id)
    assert len(fake.requests) == 1  # yesterday's spend doesn't count against today
    with session_factory() as session:
        row = session.get(SourceUsage, ("context:tinyfish", today)) or SourceUsage(
            source_id="context:tinyfish", period=today
        )
        row.requests = 99
        session.add(row)
        session.commit()
    fake.requests.clear()
    run(finder, seed(session_factory))
    assert not fake.requests  # today's spend does


def test_dry_run_does_not_consume_context_budget(session_factory):
    """``evaluate`` dry runs read the context budget (an exhausted one still skips
    searches) but never increment it: the real requests count only under the shared
    ``search:<name>`` rows, so repeated evaluations can't burn the daily context cap."""
    story_id = seed(session_factory)
    fake = FakeSearch()
    finder = make_finder(session_factory, fake, cfg=config(daily_request_budget=3))
    asyncio.run(finder.find_story(story_id, dry_run=True))
    assert fake.requests  # the searches really ran
    day, _month = usage_periods(NOW)
    with session_factory() as session:
        ctx = session.get(SourceUsage, ("context:tinyfish", day))
        assert ctx is None or not ctx.requests  # dry run: context budget untouched
        shared = session.get(SourceUsage, ("search:tinyfish", day))
        assert shared and shared.requests == len(fake.requests)
    # an exhausted context budget is still honored (read, never incremented)
    with session_factory() as session:
        session.add(SourceUsage(source_id="context:tinyfish", period=day, requests=99))
        session.commit()
    fake.requests.clear()
    result = asyncio.run(finder.find_story(seed(session_factory), dry_run=True))
    assert not fake.requests
    assert result.searches and all(s.get("skipped") == "context_budget" for s in result.searches)
    with session_factory() as session:
        assert session.get(SourceUsage, ("context:tinyfish", day)).requests == 99


def test_published_view_excludes_context_links(session_factory):
    """``views.published_story`` members never contain context links, so the doc-based
    render path (story page, brief inputs, citations) can't see them either."""
    story_id = seed(session_factory)
    run(make_finder(session_factory), story_id)
    from app import story_views as views

    with session_factory() as session:
        story = views.published_story(session, story_id)
    assert story is not None
    assert all(m.origin != CONTEXT for m in story.members)
    assert len(story.members) == 2
