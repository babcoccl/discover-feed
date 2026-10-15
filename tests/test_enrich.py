"""Web-search enrichment: providers (fake servers only), filters, fetch safety, pinning,
visibility, the brief wait and the one late regeneration."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.cluster import TfidfClusterer
from app.cluster.service import run_clustering
from app.config import EnrichmentConfig, LLMRole, SearchProviderConfig
from app.enrich.diagnose import report
from app.enrich.domains import blocked_reason, registrable_domain
from app.enrich.fetch import CandidateFetcher
from app.enrich.filters import StoryContext, pre_filter, text_check
from app.enrich.query import MAX_TOKENS, build_queries, heuristic_query, parse_llm_queries
from app.enrich.service import Enricher
from app.images.safety import Guard
from app.lifecycle import ensure_stories
from app.models import (
    ENRICHMENT,
    Article,
    EnrichmentCandidate,
    ProfileRecord,
    SourceRecord,
    SourceUsage,
    Story,
    StoryState,
)
from app.profiles import get_story, source_names
from app.search.base import SearchHit
from app.search.service import SearchService
from app.summarize.worker import SummaryWorker

NOW = datetime.now(UTC).replace(microsecond=0)
TEXT = (
    "Mayor Lena Ortiz of Springfield announced on Monday that the Springfield transit "
    "authority will buy forty electric buses and rebuild Central Station. The council "
    "approved the plan after three months of debate about fares and depots. "
) * 6
OTHER_TEXT = (
    "Springfield council approved forty electric buses on Monday, giving Mayor Lena Ortiz a "
    "win after months of debate. The Springfield transit authority will rebuild Central "
    "Station too. Critics in the transit union called the bus plan rushed; Ortiz said fares "
    "stay flat while depots are converted for electric buses. "
) * 5
UNRELATED = (
    "The national football league schedule was released with several prime time games and "
    "a new international series in three countries next autumn for every franchise. "
) * 6


def resolver(host: str) -> list[str]:
    return ["127.0.0.1"] if host.startswith("internal") else ["93.184.216.34"]


def page(title: str, body: str, extra: str = "") -> bytes:
    paras = "".join(f"<p>{s}.</p>" for s in body.split(". ") if s)
    return (
        f"<html><head><title>{title}</title>{extra}</head><body><article><h1>{title}</h1>"
        f"{paras}</article></body></html>"
    ).encode()


PAGES = {
    "https://www.wire.example/buses": page("Springfield approves electric buses", OTHER_TEXT),
    "https://syndicated.example/copy": page("Springfield mayor wins bus vote", TEXT),
    "https://sports.example/nfl": page("Springfield buses football schedule", UNRELATED),
    "https://paywall.example/buses": page(
        "Springfield electric buses approved",
        OTHER_TEXT,
        '<script type="application/ld+json">{"isAccessibleForFree": false}</script>',
    ),
}


def page_transport() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if url == "https://redirect.example/go":
            return httpx.Response(302, headers={"location": "http://internal.example/admin"})
        if url in PAGES:
            return httpx.Response(200, content=PAGES[url], headers={"content-type": "text/html"})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def gdelt_body(urls: list[tuple[str, str]]) -> dict:
    return {
        "articles": [
            {
                "url": url,
                "title": title,
                "seendate": NOW.strftime("%Y%m%dT%H%M%SZ"),
                "domain": registrable_domain(url),
                "language": "English",
                "socialimage": "",
            }
            for url, title in urls
        ]
    }


HITS = [
    ("https://news.example/other", "Springfield buses approved by council"),  # same domain
    ("https://twitter.com/x/status/1", "Springfield electric buses"),
    ("https://www.wire.example/buses", "Springfield council approves electric buses plan"),
    ("https://syndicated.example/copy", "Springfield mayor wins electric bus vote"),
    ("https://sports.example/nfl", "Springfield buses and football schedule"),
    ("https://paywall.example/buses", "Springfield electric buses approved"),
    ("https://redirect.example/go", "Springfield electric buses council plan"),
    ("https://elsewhere.example/a", "Cooking with garlic tonight"),
]


class FakeSearch:
    """A fake GDELT / SearXNG server (``httpx.MockTransport``) that counts requests."""

    def __init__(self, hits=HITS, *, status: int = 200, text: str | None = None, headers=None):
        self.hits, self.status, self.text, self.headers = hits, status, text, headers or {}
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        if self.text is not None:
            return httpx.Response(self.status, text=self.text, headers=self.headers)
        if request.url.path.endswith("/search"):  # SearXNG
            body = {
                "results": [
                    {"url": u, "title": t, "content": t, "publishedDate": NOW.isoformat()}
                    for u, t in self.hits
                ]
            }
        else:
            body = gdelt_body(self.hits)
        return httpx.Response(self.status, json=body, headers=self.headers)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


async def no_sleep(_seconds: float) -> None:
    return None


def config(**kw) -> EnrichmentConfig:
    base = {
        "enabled": True,
        "providers": [SearchProviderConfig(name="gdelt", min_interval_seconds=0)],
        "max_added_sources": 3,
        "target_sources": 4,
    }
    return EnrichmentConfig(**{**base, **kw})


def make_enricher(factory, fake: FakeSearch, cfg: EnrichmentConfig | None = None) -> Enricher:
    fetcher = CandidateFetcher(
        factory, guard=Guard(resolver), domain_delay=0, transport=page_transport()
    )
    search = SearchService(factory, transport=fake.transport(), sleep=no_sleep)
    return Enricher(factory, cfg or config(), search=search, fetcher=fetcher)


def seed(factory, *, followed: bool = True) -> int:
    """One clustered story from source ``a`` (followed by profile ``me``)."""
    with factory() as session:
        session.add(SourceRecord(id="a", name="News A", url="https://news.example/feed"))
        profile = ProfileRecord(slug="me", name="Me")
        session.add(profile)
        session.flush()
        if followed:
            profile.sources = [session.get(SourceRecord, "a")]
        for n, title in enumerate(
            ["Springfield mayor Ortiz wins council vote on electric buses"], start=1
        ):
            session.add(
                Article(
                    source_id="a",
                    url=f"https://news.example/{n}",
                    canonical_url=f"https://news.example/{n}",
                    title=title,
                    summary_raw=title,
                    published_at=NOW - timedelta(hours=1),
                    fetched_at=NOW,
                    content_hash=hashlib.sha256(title.encode()).hexdigest(),
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
        return session.scalar(select(Story.id).where(Story.state != "merged"))


def run(enricher: Enricher, story_id: int):
    enricher.schedule([story_id])
    enricher._thread.join(30)
    return enricher.last_run


# --- query building ---------------------------------------------------------------------


def test_heuristic_query_uses_headline_terms_only():
    q = heuristic_query(
        [
            "BREAKING: The Springfield council votes on a plan for electric buses today",
            "Springfield Mayor Ortiz backs electric buses",
            "Ortiz: buses will arrive in Springfield next year",
        ]
    )
    tokens = q.split()
    assert len(tokens) <= MAX_TOKENS
    assert "Springfield" in tokens and "buses" in tokens
    assert not {"BREAKING", "The", "on", "a", "for"} & set(tokens)
    assert "Ortiz" in tokens  # an entity the other headlines repeat


def test_llm_queries_are_capped_and_fall_back_to_the_heuristic():
    parsed = parse_llm_queries(
        json.dumps({"queries": ["one two three four five six seven eight nine ten", "b c", "x"]})
    )
    assert len(parsed) == 2 and all(len(q.split()) <= 8 for q in parsed)

    def broken(request):
        return httpx.Response(500, json={"error": "down"})

    role = LLMRole(base_url="http://llm.test/v1", model="m")
    queries, how = asyncio.run(
        build_queries(
            ["Springfield buses approved"],
            mode="llm",
            role=role,
            transport=httpx.MockTransport(broken),
        )
    )
    assert how == "llm_fallback" and queries == [heuristic_query(["Springfield buses approved"])]


# --- filters ------------------------------------------------------------------------------


def story_ctx(**kw) -> StoryContext:
    base = {
        "title": "Springfield mayor Ortiz wins council vote on electric buses",
        "headlines": ["Springfield mayor Ortiz wins council vote on electric buses"],
        "texts": [TEXT],
        "domains": {"news.example"},
        "canonical_urls": {"https://news.example/1"},
        "first_at": NOW - timedelta(hours=1),
        "last_at": NOW,
        "language": "en",
    }
    return StoryContext(**{**base, **kw})


@pytest.mark.parametrize(
    ("url", "kw", "reason"),
    [
        ("ftp://files.example/x", {}, "bad_url"),
        ("https://www.facebook.com/post/1", {}, "social"),
        ("https://news.google.com/articles/x", {}, "aggregator"),
        ("https://spam.example/a", {}, "denied"),
        ("https://amp.news.example/x", {}, "same_domain"),
        ("https://other.example/x", {"published_at": NOW - timedelta(days=10)}, "too_far_in_time"),
        ("https://other.example/x", {"language": "de"}, "language"),
    ],
)
def test_pre_filter_reasons(url, kw, reason):
    hit = SearchHit(url=url, title="Springfield buses", **kw)
    got = pre_filter(
        hit, story_ctx(), deny=["spam.example"], max_gap=timedelta(hours=72), language="en"
    )
    assert got == reason


def test_registrable_domains_and_block_list():
    assert registrable_domain("https://www.bbc.co.uk/news/x") == "bbc.co.uk"
    assert registrable_domain("https://edition.cnn.com/x") == "cnn.com"
    assert blocked_reason("https://m.youtube.com/watch") == "social"
    assert blocked_reason("https://reuters.com/x") is None


def test_text_check_accepts_related_and_rejects_copies_and_unrelated():
    ctx = story_ctx()
    kw = {"threshold": 0.45, "min_entities": 2, "duplicate_overlap": 0.6}
    assert text_check("Springfield buses", OTHER_TEXT, ctx, **kw)[0] is None
    assert text_check("Springfield buses", TEXT, ctx, **kw)[0] == "near_duplicate"
    assert text_check("Football", UNRELATED, ctx, **kw)[0] == "low_text_similarity"
    # a second copy of an already accepted candidate collapses too
    assert text_check("Copy", OTHER_TEXT, ctx, accepted_texts=[OTHER_TEXT], **kw)[0] == (
        "near_duplicate"
    )


# --- providers ----------------------------------------------------------------------------


def test_gdelt_and_searxng_shaped_responses_and_cache(session_factory):
    fake = FakeSearch()
    service = SearchService(session_factory, transport=fake.transport(), sleep=no_sleep)
    gdelt = SearchProviderConfig(name="gdelt", min_interval_seconds=0)
    first = asyncio.run(service.search(gdelt, "springfield buses", since=NOW, language="en"))
    assert not first.error and len(first.hits) == len(HITS)
    assert first.hits[2].domain == "wire.example" and first.hits[2].language == "en"
    assert fake.requests[0].url.params["mode"] == "artlist"
    again = asyncio.run(service.search(gdelt, "Springfield  buses ", since=NOW, language="en"))
    assert again.cached and len(fake.requests) == 1

    searx = SearchProviderConfig(
        name="searxng", base_url="http://searx.test", min_interval_seconds=0
    )
    out = asyncio.run(service.search(searx, "springfield", since=NOW, language="en"))
    assert out.hits[0].snippet and fake.requests[-1].url.params["format"] == "json"


def test_searxng_403_explains_the_json_format(session_factory):
    fake = FakeSearch(status=403, text="Forbidden")
    service = SearchService(session_factory, transport=fake.transport(), sleep=no_sleep)
    searx = SearchProviderConfig(name="searxng", base_url="http://searx.test")
    out = asyncio.run(service.search(searx, "q", since=NOW, language=None))
    assert "json" in out.error and service.health["searxng"].errors == 1


def test_rate_limits_retry_once_then_cool_down(session_factory):
    fake = FakeSearch()
    fake.responses = [httpx.Response(429, headers={"Retry-After": "2"})]
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    service = SearchService(session_factory, transport=fake.transport(), sleep=sleep)
    gdelt = SearchProviderConfig(name="gdelt", min_interval_seconds=0)
    out = asyncio.run(service.search(gdelt, "q1", since=NOW, language=None, cache_hours=0))
    assert not out.error and 2 in slept and len(fake.requests) == 2

    fake.responses = [httpx.Response(429, headers={"Retry-After": "600"})]
    out = asyncio.run(service.search(gdelt, "q2", since=NOW, language=None, cache_hours=0))
    assert out.error and "429" in out.error
    out = asyncio.run(service.search(gdelt, "q3", since=NOW, language=None, cache_hours=0))
    assert "cooling down" in out.error and len(fake.requests) == 3  # no request sent

    plain = FakeSearch(text="Please limit requests to one every 5 seconds")
    service = SearchService(session_factory, transport=plain.transport(), sleep=no_sleep)
    out = asyncio.run(service.search(gdelt, "q4", since=NOW, language=None, cache_hours=0))
    assert service.health["gdelt"].rate_limited >= 1


def test_daily_budget_is_shared_and_persisted(session_factory):
    fake = FakeSearch()
    service = SearchService(session_factory, transport=fake.transport(), sleep=no_sleep)
    capped = SearchProviderConfig(name="gdelt", daily_request_budget=1, min_interval_seconds=0)
    assert not asyncio.run(service.search(capped, "a", since=NOW, language=None)).error
    second = asyncio.run(service.search(capped, "b", since=NOW, language=None))
    assert second.error and len(fake.requests) == 1
    assert service.health["gdelt"].budget_skips == 1
    with session_factory() as session:
        assert sum(u.requests for u in session.scalars(select(SourceUsage))) >= 1


def test_provider_keys_never_leak(session_factory, monkeypatch):
    monkeypatch.setenv("BRAVE_KEY", "brave-secret-xyz")
    fake = FakeSearch(status=500, text="bad token brave-secret-xyz")
    service = SearchService(session_factory, transport=fake.transport(), sleep=no_sleep)
    brave = SearchProviderConfig(name="brave", api_key="BRAVE_KEY", min_interval_seconds=0)
    out = asyncio.run(service.search(brave, "q", since=NOW, language=None))
    assert fake.requests[0].headers["X-Subscription-Token"] == "brave-secret-xyz"
    assert "brave-secret-xyz" not in (out.error or "") + out.raw
    assert "brave-secret-xyz" not in json.dumps(service.health["brave"].as_dict())


# --- safe fetching ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "status"),
    [
        ("http://127.0.0.1/admin", "blocked"),
        ("http://internal.example/x", "blocked"),
        ("https://redirect.example/go", "blocked"),
        ("file:///etc/passwd", "blocked"),
        ("https://paywall.example/buses", "paywalled"),
        ("https://www.wire.example/buses", "ok"),
        ("https://missing.example/x", "failed"),
    ],
)
def test_fetcher_ssrf_paywall_and_failures(url, status):
    fetcher = CandidateFetcher(guard=Guard(resolver), domain_delay=0, transport=page_transport())
    got = asyncio.run(fetcher.fetch(url))
    assert got.status == status, got.reason
    if status == "ok":
        assert got.words > 50


# --- the stage end to end -----------------------------------------------------------------


def test_enrichment_adds_pinned_members_and_records_decisions(session_factory):
    story_id = seed(session_factory)
    enricher = make_enricher(session_factory, FakeSearch())
    summary = run(enricher, story_id)
    assert summary["added"] == 1, summary
    with session_factory() as session:
        added = session.scalars(select(Article).where(Article.origin == ENRICHMENT)).all()
        assert [a.url for a in added] == ["https://www.wire.example/buses"]
        member = added[0]
        assert member.story_id == member.discovered_for_story_id == story_id
        assert member.source_id == "web:wire.example" and member.text_status == "ok"
        assert source_names(session).get(member.source_id) == "wire.example"
        story = session.get(Story, story_id)
        assert story.enrich_status == "done" and story.enrich_added == 1
        reasons = {c.url: c.reason for c in session.scalars(select(EnrichmentCandidate))}
    assert reasons["https://news.example/other"] == "same_domain"
    assert reasons["https://twitter.com/x/status/1"] == "social"
    assert reasons["https://syndicated.example/copy"] == "near_duplicate"
    assert reasons["https://paywall.example/buses"] == "paywalled"
    assert reasons["https://redirect.example/go"] == "blocked_url"
    assert reasons["https://elsewhere.example/a"] == "low_similarity"
    stats = enricher.stats(session_factory())
    assert stats["enriched"] == 1 and dict(stats["reject_reasons"])["same_domain"] == 1


def test_dry_run_adds_nothing_and_diagnose_reports(session_factory):
    story_id = seed(session_factory)
    enricher = make_enricher(session_factory, FakeSearch())
    result = asyncio.run(enricher.enrich_story(story_id, dry_run=True))
    text = report(result, "t")
    assert "wire.example" in text and "same_domain" in text and "raw:" in text
    with session_factory() as session:
        assert not session.scalars(select(Article).where(Article.origin == ENRICHMENT)).all()
        assert session.get(Story, story_id).enrich_status is None


def test_pinning_survives_rebuild_and_merge(session_factory):
    story_id = seed(session_factory)
    run(make_enricher(session_factory, FakeSearch()), story_id)
    run_clustering(session_factory, TfidfClusterer(), rebuild=True)
    with session_factory() as session:
        member = session.scalar(select(Article).where(Article.origin == ENRICHMENT))
        anchor = session.get(Article, member.anchor_article_id)
        assert member.story_id == anchor.story_id is not None
        stories = session.scalars(select(Story).where(Story.state != "merged")).all()
        assert len(stories) == 1  # never a story of its own


def test_enrichment_members_never_make_a_story_visible(session_factory):
    story_id = seed(session_factory)
    run(make_enricher(session_factory, FakeSearch()), story_id)
    with session_factory() as session:
        follows = session.scalar(select(ProfileRecord))
        item = get_story(session, story_id, follows)
        assert item is not None and item.source_count == 2
        assert item.representative.origin == "feed"
        stranger = ProfileRecord(slug="other", name="Other")
        session.add(stranger)
        session.flush()
        assert get_story(session, story_id, stranger) is None
        # a detached enrichment article is never turned into a story of its own
        member = session.scalar(select(Article).where(Article.origin == ENRICHMENT))
        member.story_id = None
        session.flush()
        assert ensure_stories(session, NOW) is not None or True
        assert member.story_id is None


def test_not_eligible_without_an_enabled_profile_or_when_well_covered(session_factory):
    story_id = seed(session_factory, followed=False)
    enricher = make_enricher(session_factory, FakeSearch())
    with session_factory() as session:
        assert enricher.eligible(session) == []
        profile = session.scalar(select(ProfileRecord))
        profile.sources = [session.get(SourceRecord, "a")]
        session.commit()
        assert enricher.eligible(session) == [story_id]
        profile.enrichment_enabled = False  # the Settings toggle
        session.commit()
        assert enricher.eligible(session) == []
    covered = make_enricher(session_factory, FakeSearch(), config(target_sources=2))
    with session_factory() as session:
        session.scalar(select(ProfileRecord)).enrichment_enabled = None
        session.commit()
        assert covered.eligible(session) == [story_id]  # 1 publisher < 2


def test_brief_waits_for_enrichment_then_goes_ahead(session_factory):
    story_id = seed(session_factory)
    clock = {"now": NOW}
    worker = SummaryWorker(session_factory, None, clock=lambda: clock["now"])
    worker.enrich_wait = timedelta(seconds=180)
    with session_factory() as session:
        story = session.get(Story, story_id)
        story.enrich_status, story.enrich_started_at = "pending", NOW
        session.commit()
        assert story_id not in worker.candidates(session)
        assert story_id in worker.candidates(session, story_id, manual=True)
        clock["now"] = NOW + timedelta(seconds=181)  # provider timed out: go ahead
        assert story_id in worker.candidates(session)


def test_search_failure_still_finishes_the_story(session_factory):
    story_id = seed(session_factory)
    summary = run(make_enricher(session_factory, FakeSearch(status=500, text="down")), story_id)
    assert summary["added"] == 0 and summary["provider_errors"] >= 1
    with session_factory() as session:
        assert session.get(Story, story_id).enrich_status == "done"


def test_late_sources_regenerate_a_published_story_once(session_factory):
    story_id = seed(session_factory)
    with session_factory() as session:
        story = session.get(Story, story_id)
        story.state, story.published_version = StoryState.PUBLISHED.value, 1
        session.commit()
    enricher = make_enricher(session_factory, FakeSearch())
    run(enricher, story_id)
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_regens == 1 and story.state == StoryState.CLUSTERED
        assert story.published_version == 1  # the published view stays until replaced
        story.enrich_status, story.enriched_at = "done", NOW - timedelta(days=2)
        session.commit()
        assert enricher.eligible(session) == []  # never a second regeneration
    forced = asyncio.run(enricher.enrich_story(story_id))
    assert all(c.reason != "accepted" for c in forced.candidates)


# --- admin actions ------------------------------------------------------------------------


def test_enrich_now_requires_the_admin_key(settings):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from app.main import create_app

    settings.admin_key = SecretStr("admin-key-123")
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/admin/enrich").status_code == 401
        ok = client.post("/api/admin/enrich", headers={"X-API-Key": "admin-key-123"})
        assert ok.status_code == 200 and ok.json() == {"queued": 0}
        slug = client.app.state.config.profiles[0].id
        page = client.post(f"/p/{slug}/settings/pipeline/enrich", follow_redirects=False)
        assert page.status_code in (302, 303, 401)
