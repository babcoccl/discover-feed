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
from app.config import (
    ContextConfig,
    EnrichmentConfig,
    EnrichmentQualityConfig,
    LLMRole,
    SearchProviderConfig,
)
from app.context.angles import Angle, heuristic_angles, make_angles, parse_angles
from app.context.filters import StoryContext, context_pre_filter, context_text_check
from app.context.service import ContextFinder
from app.enrich.fetch import CandidateFetcher
from app.enrich.query import story_terms
from app.images.safety import Guard
from app.lifecycle import ensure_stories
from app.models import (
    CONTEXT,
    Article,
    BlockedDomain,
    ContextCandidate,
    ProfileRecord,
    SourceRecord,
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
                    "site_name": urlsplit(e[0]).hostname,
                    "title": e[1],
                    "snippet": e[1],
                    "url": e[0],
                    # an optional 3rd tuple element is the hit's ISO date (None: undated)
                    "date": e[2] if len(e) > 2 else NOW.isoformat(),
                }
                for n, e in enumerate(self.hits, start=1)
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
        enrichment=kw.pop("enrichment", EnrichmentConfig()),
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


# --- context quality: same-event rules, opinion labels, content-farm gate ----------


def test_same_event_signals():
    """The three deterministic signals each reject independently: >=2 shared specific
    numbers (years and single digits never count), the stricter similarity band for
    contemporaneous pages (dated +/-72h, or undated with the story's entities), and
    a title echoing >=3 story entities plus a headline event word."""
    from app.context.filters import same_event_signals
    from app.context.rules import SameEventRules

    rules = SameEventRules()
    # the real StoryContext carries every member headline ("approves" is an event word)
    story = StoryContext(
        title="Springfield mayor Ortiz wins council vote on electric buses",
        headlines=[
            "Springfield mayor Ortiz wins council vote on electric buses",
            "Springfield council approves electric bus purchase",
        ],
        texts=[TEXT],
        domains=set(),
        canonical_urls=set(),
        first_at=NOW - timedelta(hours=1),
        last_at=NOW,
    )
    brief = "The council approved forty electric buses for $61.2 million over 18 months."

    def check(
        title,
        *,
        text="",
        published_at=None,
        entities=0,
        text_similarity=0.0,
        story_=story,
        brief_=brief,
    ):
        return same_event_signals(
            hit("https://x.example/p", title),
            story_,
            brief=brief_,
            text=text,
            published_at=published_at,
            entities=entities,
            text_similarity=text_similarity,
            rules=rules,
        )

    # numbers: >=2 shared numeric tokens (title + first 600 chars vs title + brief)
    assert (
        check(
            "Springfield bus deal",
            text="The package costs $61.2 million and runs 18 months, records show.",
            published_at=NOW - timedelta(days=30),
        )
        == "numbers"
    )
    # single digits and 4-digit years are ignored -> nothing left to share
    assert (
        check(
            "Springfield bus deal",
            text="3 routes since 2019 and a second one in 2026.",
            published_at=NOW - timedelta(days=30),
        )
        is None
    )
    # date_band: a dated page inside +/-72h must clear BOTH 0.30 caps
    kw = {"text": COVERAGE_BLEND, "entities": 8, "text_similarity": 0.55}
    assert check("Council approves Springfield bus plan", published_at=NOW, **kw) == "date_band"
    assert (
        check(
            "Council approves Springfield bus plan",
            published_at=NOW + timedelta(hours=40),
            **kw,
        )
        == "date_band"
    )
    # outside the window the band does not apply (a real backgrounder can be old)
    assert (
        check(
            "Springfield transit history",
            published_at=NOW - timedelta(days=30),
            entities=1,
            text_similarity=0.26,
        )
        is None
    )
    # undated with >=3 shared entities counts as contemporaneous too
    assert (
        check(
            "Mayor wins bus vote",
            text=BACKGROUND3,
            published_at=None,
            entities=3,
            text_similarity=0.18,
        )
        == "date_band"
    )
    # but an undated genuinely-different backgrounder still survives the band
    assert (
        check(
            "How other cities handled bus electrification",
            text=BACKGROUND3,
            published_at=None,
            entities=3,
            text_similarity=0.18,
        )
        is None
    )
    # entities_verb: >=3 story entities in the title plus a headline event word
    assert (
        check(
            "Springfield council approves new bus depot site",
            published_at=NOW - timedelta(days=30),
            entities=1,
        )
        == "entities_verb"
    )
    # >=3 shared entities without a headline event word is fine
    assert (
        check(
            "Springfield council Ortiz depot photo gallery",
            published_at=NOW - timedelta(days=30),
            entities=1,
        )
        is None
    )
    # an event word in the title that is NOT in a story headline does not fire
    assert (
        check(
            "Springfield mayor Ortiz recalls bus fight",
            published_at=NOW - timedelta(days=30),
            entities=1,
        )
        is None
    )


def test_same_event_signals_eval_pairs():
    """The spec's accepted-as-same-event pattern: near-identical coverage that the
    judge let through (breach/launch/unveil titles echoing the story) is rejected
    by the deterministic rules; genuine backgrounder-style pages still pass."""
    from app.context.filters import same_event_signals
    from app.context.rules import SameEventRules

    rules = SameEventRules()
    breach = StoryContext(
        title="OpenCode breach exposes 1068 user API tokens",
        headlines=[
            "OpenCode breach exposes 1068 user API tokens",
            "Hackers leak 1068 API tokens stolen from OpenCode",
        ],
        texts=["Hackers breached OpenCode and leaked 1068 tokens. " * 60],
        domains=set(),
        canonical_urls=set(),
        first_at=NOW - timedelta(hours=5),
        last_at=NOW,
    )
    # same-event coverage: contemporaneous, echoing title, shared "1068"/"6200" numbers
    assert (
        same_event_signals(
            hit("https://thehackernews.example/opencode", "OpenCode hackers leak 1,068 tokens"),
            breach,
            brief="The breach leaked 1068 API tokens; a $6200 bounty was paid.",
            text="OpenCode hackers leaked 1,068 tokens and collected a $6,200 bounty. " * 30,
            published_at=NOW,
            entities=9,
            text_similarity=0.5,
            rules=rules,
        )
        == "numbers"
    )
    # entities+event word: a launch story retold (the Mercury-3 acceptance)
    launch = StoryContext(
        title="Astra launches Mercury 3 rocket from pad 39A",
        headlines=["Astra launches Mercury 3 rocket from pad 39A"],
        texts=["Astra's Mercury 3 lifted off from pad 39A carrying satellites. " * 60],
        domains=set(),
        canonical_urls=set(),
        first_at=NOW - timedelta(hours=2),
        last_at=NOW,
    )
    assert (
        same_event_signals(
            hit("https://startupfortune.example/m3", "Astra's Mercury 3 launches successfully"),
            launch,
            brief="Astra launched the Mercury 3 rocket.",
            text="Mercury 3 launches: Astra's rocket lifted off from pad 39A. " * 30,
            published_at=NOW,
            entities=7,
            text_similarity=0.4,
            rules=rules,
        )
        == "date_band"  # contemporaneous + similar; entities_verb would fire too
    )
    # a genuine backgrounder still passes every signal
    for title, published_at, text in (
        ("How the DOE bet on small reactor designs", NOW - timedelta(days=60), BACKGROUND),
        ("A history of API token leaks", None, BACKGROUND2),
        ("Rocketry programs comparable to Astra's", NOW - timedelta(days=90), BACKGROUND3),
    ):
        got = same_event_signals(
            hit("https://bg.example/x", title),
            launch,
            brief="Astra launched the Mercury 3 rocket.",
            text=text,
            published_at=published_at,
            entities=2,
            text_similarity=0.18,
            rules=rules,
        )
        assert got is None, (title, got)


def test_same_event_rule_and_prior_date_preference(session_factory):
    """A dated page inside the event window that retells the story is rejected as
    ``same_event`` before the judge; an undated title-lookalike sharing the story's
    entities is contemporaneous and rejected too; a real backgrounder — dated before
    the story, or undated but genuinely different — is accepted, the dated one
    first."""
    story_id = seed(session_factory)
    PAGES["https://farm.example/retell"] = page("Springfield buses approved", COVERAGE_BLEND)
    PAGES["https://bg.example/history"] = page("Springfield transit history", BACKGROUND)
    PAGES["https://undated.example/take"] = page(
        "How other cities handled bus electrification", BACKGROUND3
    )
    PAGES["https://undated.example/echo"] = page("Mayor wins bus vote", BACKGROUND3)
    hits = [
        # dated NOW -> inside +/-72h; COVERAGE_BLEND reads ~0.55 like the story
        # -> date_band (the shattered.io DMV pattern from the eval)
        (
            "https://farm.example/retell",
            "Council approves Springfield bus plan",
            NOW.isoformat(),
        ),
        # a real backgrounder predates the story -> wins the prior-date preference
        (
            "https://bg.example/history",
            "Springfield transit history",
            (NOW - timedelta(days=30)).isoformat(),
        ),
        # undated + shares >=3 entities -> contemporaneous: its title reads ~0.37
        # like the headlines -> date_band rejects it too
        ("https://undated.example/echo", "Mayor wins bus vote", None),
        # undated but genuinely different -> survives the band
        ("https://undated.example/take", "How other cities handled bus electrification", None),
    ]
    run(make_finder(session_factory, FakeSearch(hits)), story_id)
    with session_factory() as session:
        rows = {c.url: c for c in session.scalars(select(ContextCandidate))}
    assert rows["https://farm.example/retell"].reason == "same_event"
    assert rows["https://farm.example/retell"].same_event_signal == "date_band"
    assert rows["https://undated.example/echo"].reason == "same_event"
    assert rows["https://undated.example/take"].decision == "accepted"
    links = context_articles(session_factory, story_id)
    assert [a.url for a in links] == [
        "https://bg.example/history",
        "https://undated.example/take",
    ]
    # the accepted link keeps the page's own date, not the discovery time
    assert links[0].published_at == NOW - timedelta(days=30)


def test_judge_same_event_forces_rejection_and_opinion_cap(session_factory):
    """The spec's eval regressions: same-event coverage the old prompt scored 2
    ("explains the cause", "adds hacker motives") is rejected via the judge's
    ``same_event`` flag; opinion pages are kept but labeled and capped."""
    story_id = seed(session_factory)
    PAGES["https://dmv.example/breach"] = page(
        "Florida DMV Breach: Hackers Claim 200K Records", BACKGROUND
    )
    PAGES["https://view.example/opinion/bus-take"] = page("Why the bus vote was wrong", BACKGROUND)
    PAGES["https://take.example/news/take"] = page("A Springfield transit take", BACKGROUND2)
    PAGES["https://bg.example/history"] = page("Springfield transit history", BACKGROUND3)

    def answer(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if '"angles"' in body:
            out = '{"angles": [{"label": "history", "query": "\\"Springfield transit\\" history"}]}'
        elif "Florida DMV Breach" in body:
            # the same event, scored 2 by the old prompt -> same_event forces 0
            out = (
                '{"adds_context": 2, "same_event": true, "is_opinion": false, '
                '"is_substantive_article": 1, "note": "adds hacker group motives"}'
            )
        elif "A Springfield transit take" in body:
            out = (
                '{"adds_context": 2, "same_event": false, "is_opinion": true, '
                '"is_substantive_article": 1, "note": "a columnist take"}'
            )
        else:
            out = (
                '{"adds_context": 2, "same_event": false, "is_opinion": false, '
                '"is_substantive_article": 1, "note": "decades of transit history"}'
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": out}}]})

    hits = [
        ("https://dmv.example/breach", "Florida DMV Breach: Hackers Claim 200K Records", None),
        ("https://view.example/opinion/bus-take", "Why the bus vote was wrong", None),
        ("https://take.example/news/take", "A Springfield transit take", None),
        ("https://bg.example/history", "Springfield transit history", None),
    ]
    finder = make_finder(
        session_factory,
        FakeSearch(hits),
        llm_role=role(),
        llm_transport=httpx.MockTransport(answer),
    )
    run(finder, story_id)
    links = context_articles(session_factory, story_id)
    kinds = {a.url: a.context_kind for a in links}
    # URL path detection marks the /opinion/ link even with the judge saying article
    assert kinds["https://view.example/opinion/bus-take"] == "opinion"
    assert kinds["https://bg.example/history"] == "article"
    # the judge-flagged second opinion hits context.max_opinion (default 1)
    assert "https://take.example/news/take" not in kinds
    with session_factory() as session:
        rows = {c.url: c for c in session.scalars(select(ContextCandidate))}
    assert rows["https://dmv.example/breach"].reason == "same_event"
    assert rows["https://dmv.example/breach"].same_event_signal == "judge"
    assert rows["https://dmv.example/breach"].judge_same_event is True
    assert rows["https://dmv.example/breach"].judge == 2  # what the old prompt scored it
    assert rows["https://take.example/news/take"].reason == "opinion_cap"
    assert rows["https://take.example/news/take"].is_opinion


def test_judge_flag_attribution(session_factory):
    """Reason attribution: ``same_event`` only for rule hits or a judge flag with
    adds_context >= 1; a judge score of 0 with the flag is ``off_topic`` (the page
    was simply irrelevant), and without it ``judge_rejected``."""
    story_id = seed(session_factory)
    PAGES["https://a.example/x"] = page("A Springfield transit take", BACKGROUND2)
    PAGES["https://b.example/y"] = page("How Springfield got here", BACKGROUND)
    PAGES["https://c.example/z"] = page("What other cities learned", BACKGROUND3)

    def answer(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if '"angles"' in body:
            out = '{"angles": [{"label": "history", "query": "\\"Springfield transit\\" history"}]}'
        elif "A Springfield transit take" in body:
            # score 0 with the flag: irrelevant, not same-event coverage
            out = (
                '{"adds_context": 0, "same_event": true, "is_opinion": false, '
                '"is_substantive_article": 1, "note": "off topic"}'
            )
        elif "How Springfield got here" in body:
            out = (
                '{"adds_context": 0, "same_event": false, "is_opinion": false, '
                '"is_substantive_article": 1, "note": "not context"}'
            )
        else:
            # thin but flagged same-event by the judge -> same_event (judge signal)
            out = (
                '{"adds_context": 1, "same_event": true, "is_opinion": false, '
                '"is_substantive_article": 1, "note": "thin same-event recap"}'
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": out}}]})

    hits = [
        ("https://a.example/x", "A Springfield transit take", None),
        ("https://b.example/y", "How Springfield got here", None),
        ("https://c.example/z", "What other cities learned", None),
    ]
    run(
        make_finder(
            session_factory,
            FakeSearch(hits),
            llm_role=role(),
            llm_transport=httpx.MockTransport(answer),
        ),
        story_id,
    )
    with session_factory() as session:
        rows = {c.url: c for c in session.scalars(select(ContextCandidate))}
    assert rows["https://a.example/x"].reason == "off_topic"
    assert rows["https://a.example/x"].judge_same_event is True
    assert rows["https://b.example/y"].reason == "judge_rejected"
    assert rows["https://b.example/y"].judge_same_event is False
    assert rows["https://c.example/z"].reason == "same_event"
    assert rows["https://c.example/z"].same_event_signal == "judge"


def test_context_reads_the_shared_deny_lists(session_factory):
    """Context denies candidates on the same three lists as enrichment: YAML
    ``enrichment.deny_domains``, ``enrichment.quality.deny_domains`` and the DB block
    list — plus the built-in farm list, which covers the eval's repeat offenders."""
    story_id = seed(session_factory)
    with session_factory() as session:
        session.add(BlockedDomain(domain="dbblocked.example", canonical="dbblocked.example"))
        session.commit()
    hits = [
        ("https://yamldenied.example/a", "Springfield transit background"),
        ("https://qualityfarm.example/b", "Springfield transit background"),
        ("https://dbblocked.example/c", "Springfield transit background"),
        # the eval's content farms, denied via the new DEFAULT_DENY_DOMAINS defaults
        ("https://shattered.io/florida-dmv-breach-2026", "Florida DMV Breach"),
        ("https://finance.biggo.com/wyze-scale", "Wyze Scale X Full Spec Comparison"),
        ("https://vocal.media/shinyhunters", "The ShinyHunters Florida Leak Explained"),
        ("https://newscord.org/musk", "Elon Musk Pledges One SpaceX Share"),
        ("https://wikipedia.example/transit-history", "A history of Springfield transit"),
    ]
    enrichment = EnrichmentConfig(
        deny_domains=["yamldenied.example"],
        quality=EnrichmentQualityConfig(deny_domains=["qualityfarm.example"]),
    )
    # wait: a custom quality.deny_domains REPLACES the defaults, so the farms must be
    # denied by... -> second run below uses the default config for them
    finder = make_finder(session_factory, FakeSearch(hits), enrichment=enrichment)
    run(finder, story_id)
    with session_factory() as session:
        rows = {c.url: c.reason for c in session.scalars(select(ContextCandidate))}
    assert rows["https://yamldenied.example/a"] == "denied"
    assert rows["https://qualityfarm.example/b"] == "denied"
    assert rows["https://dbblocked.example/c"] == "denied"

    # default config: the built-in farm list denies the eval offenders out of the box
    story2 = seed(session_factory)
    run(make_finder(session_factory, FakeSearch(hits)), story2)
    with session_factory() as session:
        rows = {
            c.url: c.reason
            for c in session.scalars(
                select(ContextCandidate).where(ContextCandidate.story_id == story2)
            )
        }
    for u, _t, *_ in hits[3:7]:
        assert rows[u] == "denied", (u, rows.get(u))
    links = context_articles(session_factory, story2)
    assert [a.url for a in links] == ["https://wikipedia.example/transit-history"]


def test_flagged_candidates_rejected_in_context(session_factory):
    """``context.reject_flagged`` (default true) rejects what the shared quality gate
    flags — farm-pattern domains and SEO-template titles — where enrichment only
    marks them; with the flag off the candidate is kept and recorded ``flagged``."""
    story_id = seed(session_factory)
    PAGES["https://news24.example/bus"] = page("Springfield transit background", BACKGROUND)
    PAGES["https://ok.example/compare"] = page("Bus Depot Full Spec Comparison [2026]", BACKGROUND2)
    hits = [
        ("https://news24.example/bus", "Springfield transit background", None),
        ("https://ok.example/compare", "Bus Depot Full Spec Comparison [2026]", None),
        ("https://wikipedia.example/transit-history", "A history of Springfield transit", None),
    ]
    run(make_finder(session_factory, FakeSearch(hits)), story_id)
    with session_factory() as session:
        rows = {c.url: c for c in session.scalars(select(ContextCandidate))}
    assert rows["https://news24.example/bus"].reason == "flagged"
    assert rows["https://news24.example/bus"].flagged == "name:news24"
    assert rows["https://ok.example/compare"].reason == "flagged"
    assert rows["https://ok.example/compare"].flagged == "title:seo"
    assert rows["https://wikipedia.example/transit-history"].decision == "accepted"

    story2 = seed(session_factory)
    run(
        make_finder(session_factory, FakeSearch(hits), cfg=config(reject_flagged=False)),
        story2,
    )
    with session_factory() as session:
        rows = {
            c.url: c
            for c in session.scalars(
                select(ContextCandidate).where(ContextCandidate.story_id == story2)
            )
        }
    assert rows["https://news24.example/bus"].decision == "accepted"
    assert rows["https://news24.example/bus"].flagged == "name:news24"  # kept, marked


def test_block_domain_removes_context_links(session_factory):
    """ "Block this domain" on a context link: the domain lands in the DB block list
    (denying future candidates) and existing context links are deleted — context
    links never feed briefs, so no story is requeued for them."""
    from app.enrich.service import Enricher

    story_id = seed(session_factory)
    run(make_finder(session_factory), story_id)
    links = context_articles(session_factory, story_id)
    assert len(links) == 1
    enricher = Enricher(session_factory, EnrichmentConfig())
    result = enricher.block_domain(links[0].url)
    assert result["domain"] == "wikipedia.example"
    assert result["removed_context"] == 1 and result["removed"] == 1
    assert result["queued"] == 0  # context links never feed briefs: no regeneration
    assert not context_articles(session_factory, story_id)
    with session_factory() as session:
        assert session.scalars(select(BlockedDomain)).all()


def test_opinion_url_and_meta_detection():
    """URL path segments, newsletter hosts and page metadata mark opinion pages."""
    from app.page_meta import is_opinion_page, page_meta

    assert is_opinion_page("https://mk.co.kr/columnists/x")
    assert is_opinion_page("https://arguably-uk.substack.com/p/take")
    assert is_opinion_page("https://fakti.bg/mnenia/123")
    assert is_opinion_page("https://example.com/blog/my-take")
    assert not is_opinion_page("https://news.example/world/story-123")
    meta = page_meta(
        b'<html><head><meta property="og:type" content="article"/>'
        b'<meta property="article:section" content="Opinion"/></head><body/></html>',
        "https://news.example/x",
    )
    assert is_opinion_page("https://news.example/x", meta)


def test_page_meta_parses_dates():
    """``og:article:published_time``, JSON-LD ``datePublished`` and ``<time>`` all
    feed the same-event date rule and the context link's ``published_at``."""
    from app.page_meta import PageMeta, page_meta

    meta = page_meta(
        b'<html><head><meta property="og:article:published_time" '
        b'content="2024-05-01T10:00:00Z"/></head><body/></html>',
        "https://x.example/a",
    )
    assert meta.published_at == datetime(2024, 5, 1, 10, tzinfo=UTC)
    meta = page_meta(
        b'<html><head><script type="application/ld+json">'
        b'{"@type": "NewsArticle", "datePublished": "2023-11-20T08:30:00+00:00"}'
        b"</script></head><body/></html>",
        "https://x.example/b",
    )
    assert meta.published_at == datetime(2023, 11, 20, 8, 30, tzinfo=UTC)
    meta = page_meta(
        b'<html><body><article><time datetime="2022-02-14">Feb 14</time></article></body></html>',
        "https://x.example/c",
    )
    assert meta.published_at == datetime(2022, 2, 14, tzinfo=UTC)
    assert page_meta(b"<html><body>no dates</body></html>", "https://x.example/d") == PageMeta()
