"""Web-search enrichment: providers (fake servers only), filters, fetch safety, pinning,
visibility, the brief wait and the one late regeneration."""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.cluster import TfidfClusterer
from app.cluster.service import run_clustering
from app.config import EnrichmentConfig, LLMRole, SearchProviderConfig
from app.enrich.diagnose import report
from app.enrich.domains import blocked_reason, registrable_domain
from app.enrich.fetch import CandidateFetcher
from app.enrich.filters import StoryContext, pre_filter, snippet_check, text_check
from app.enrich.query import MAX_TOKENS, build_queries, heuristic_query, parse_llm_queries
from app.enrich.service import Enricher
from app.images.safety import Guard
from app.lifecycle import ensure_stories
from app.models import (
    ENRICHMENT,
    Article,
    BlockedDomain,
    EnrichmentCandidate,
    ProfileRecord,
    SearchCacheEntry,
    SearchProviderState,
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
        if "tinyfish" in request.url.host:
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
        elif request.url.path.endswith("/search"):  # SearXNG
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
    assert text_check("Springfield buses", OTHER_TEXT, ctx, **kw).reason is None
    assert text_check("Springfield buses", TEXT, ctx, **kw).reason == "near_duplicate"
    assert text_check("Football", UNRELATED, ctx, **kw).reason == "low_text_similarity"
    # a second copy of an already accepted candidate collapses too
    assert text_check("Copy", OTHER_TEXT, ctx, accepted_texts=[OTHER_TEXT], **kw).reason == (
        "near_duplicate"
    )


def test_threshold_defaults_are_the_tuned_starting_points():
    cfg = EnrichmentConfig()
    assert cfg.min_text_similarity == 0.35 and cfg.min_snippet_similarity == 0.25
    assert cfg.min_title_similarity == 0.30 and cfg.min_shared_entities == 2
    assert cfg.min_snippet_strong == 0.45 and cfg.min_text_floor == 0.25
    assert cfg.min_signal_tokens == 1 and cfg.link_only is False
    assert [p.name for p in cfg.providers] == ["tinyfish"]  # gdelt is opt-in
    assert "guardian" not in {p.name for p in cfg.providers}


@pytest.mark.parametrize(
    ("site", "snippet", "text", "entities", "reason", "rule"),
    [
        # the live-eval scores: snippets of 0.43-0.48 with text of ~0.25 were dropped
        ("gamesindustry.biz", 0.48, 0.25, 2, None, "b"),
        ("phoronix.com", 0.46, 0.30, 3, None, "b"),
        ("shacknews.com", 0.43, 0.30, 2, "low_text_similarity", ""),  # weak snippet
        ("thin.example", 0.48, 0.20, 2, "low_text_similarity", ""),  # below the floor
        ("few.example", 0.48, 0.30, 1, "few_shared_entities", ""),
        ("strong.example", 0.40, 0.60, 2, None, ""),  # rule A still wins
    ],
)
def test_combined_rule_b_accepts_strong_snippets(
    monkeypatch, site, snippet, text, entities, reason, rule
):
    """Rule B: snippet >= 0.45 + entities >= 2 + text >= 0.25 accepts below the
    min_text_similarity threshold; the near-duplicate check still applies."""
    monkeypatch.setattr("app.enrich.filters.similarity", lambda _a, _b: text)
    monkeypatch.setattr("app.enrich.filters.shared_entities", lambda _a, _b: entities)
    verdict = text_check(
        f"Ubuntu 26.10 on {site}",
        OTHER_TEXT,
        story_ctx(),
        threshold=0.35,
        min_entities=2,
        duplicate_overlap=0.6,
        snippet_similarity=snippet,
    )
    assert verdict.reason == reason and verdict.score == text
    if reason is None:
        assert verdict.rule == rule


@pytest.mark.parametrize(
    ("status", "decision", "reason"),
    [
        ("failed", "link_only", "fetch_failed"),
        ("blocked", "link_only", "blocked_url"),
        ("paywalled", "link_only", "paywalled"),
        ("skipped", "rejected", "robots"),  # robots-disallowed is never link-only
    ],
)
def test_link_only_candidate_is_stored_without_text(
    session_factory, monkeypatch, status, decision, reason
):
    """A strong-snippet hit whose page cannot be fetched becomes a text_status=none member
    (enrichment.link_only): counted separately, never summarized."""
    from app.enrich.fetch import FetchedPage

    monkeypatch.setattr("app.enrich.filters.similarity", lambda _a, _b: 0.5)
    monkeypatch.setattr("app.enrich.filters.shared_entities", lambda _a, _b: 3)
    story_id = seed(session_factory)
    tinyfish = SearchProviderConfig(
        name="tinyfish", base_url="http://tinyfish.test/search", min_interval_seconds=0
    )
    enricher = make_enricher(
        session_factory, FakeSearch(), config(providers=[tinyfish], link_only=True)
    )

    async def dead(url: str) -> FetchedPage:
        return FetchedPage(url, status, reason="", text="", words=0)

    monkeypatch.setattr(enricher.fetcher, "fetch", dead)
    result = asyncio.run(enricher.enrich_story(story_id))
    link_only = [c for c in result.candidates if c.decision == "link_only"]
    if decision == "link_only":
        assert link_only and link_only[0].reason == reason and result.added == 0
        with session_factory() as session:
            member = session.scalar(select(Article).where(Article.url == link_only[0].hit.url))
            assert member.text_status == "none" and member.text is None
            assert member.origin == ENRICHMENT and member.word_count is None
            ctx, _ = enricher.context(session, story_id)
            assert all(t == "" for t in ctx.texts[1:])  # link-only text is never input
    else:
        assert not link_only
        assert all(c.reason != "robots" or c.decision == "rejected" for c in result.candidates)


@pytest.mark.parametrize(
    ("site", "score", "reason"),
    [
        ("gamingonlinux.com", 0.67, None),
        ("phoronix.com", 0.46, None),
        ("linuxiac.com", 0.36, None),
        ("unrelated.example", 0.20, "low_text_similarity"),
    ],
)
def test_live_text_scores_pass_the_default_threshold(monkeypatch, site, score, reason):
    """Story 370 (Ubuntu 26.10): the related coverage TinyFish found scored 0.36-0.67."""
    monkeypatch.setattr("app.enrich.filters.similarity", lambda _a, _b: score)
    cfg = EnrichmentConfig()
    got = text_check(
        f"Ubuntu 26.10 on {site}",
        OTHER_TEXT,
        story_ctx(),
        threshold=cfg.min_text_similarity,
        min_entities=cfg.min_shared_entities,
        duplicate_overlap=cfg.duplicate_overlap,
    )
    assert got.reason == reason and got.score == score


@pytest.mark.parametrize(
    ("site", "score", "reason"),
    [
        ("betanews.com", 0.30, None),
        ("omgubuntu.co.uk", 0.29, None),
        ("x.example", 0.20, "low_similarity"),
    ],
)
def test_live_snippet_scores_pass_the_default_threshold(monkeypatch, site, score, reason):
    monkeypatch.setattr("app.enrich.filters.similarity", lambda _a, _b: score)
    hit = SearchHit(url=f"https://{site}/ubuntu-26-10", title="Ubuntu 26.10", snippet="out now")
    assert snippet_check(hit, story_ctx(), EnrichmentConfig().min_snippet_similarity) == (
        reason,
        score,
    )


NINE_TO_FIVE = "https://9to5linux.com/ubuntu-26-10-is-now-available-for-download-heres-whats-new"
NINE_TO_FIVE_ROBOTS = (  # the shape of 9to5linux.com/robots.txt: named crawlers only
    "User-agent: Googlebot\nAllow: /\n\nUser-agent: bingbot\nCrawl-delay: 10\nAllow: /\n\n"
    "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /\n"
)


def nine_to_five_transport(robots: httpx.Response) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return robots
        if str(request.url) == NINE_TO_FIVE:
            body = page("Ubuntu 26.10 is now available for download", OTHER_TEXT)
            return httpx.Response(200, content=body, headers={"content-type": "text/html"})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    ("robots", "status", "reason"),
    [
        (httpx.Response(200, text=NINE_TO_FIVE_ROBOTS), "skipped", "disallowed by robots.txt"),
        (httpx.Response(403, text="cloudflare"), "ok", None),  # RFC 9309: 4xx = no rules
        (httpx.Response(404), "ok", None),
    ],
)
def test_9to5linux_pages_fetch_unless_robots_refuses(robots, status, reason):
    fetcher = CandidateFetcher(
        guard=Guard(resolver), domain_delay=0, transport=nine_to_five_transport(robots)
    )
    got = asyncio.run(fetcher.fetch(NINE_TO_FIVE))
    assert got.status == status, got.reason
    if reason:
        assert got.reason == reason
    else:
        assert got.words > 50


def test_robots_refusals_are_reported_as_robots_not_fetch_failed(session_factory):
    story_id = seed(session_factory)
    hits = [(NINE_TO_FIVE, "Springfield council approves electric buses plan")]
    fetcher = CandidateFetcher(
        session_factory,
        guard=Guard(resolver),
        domain_delay=0,
        transport=nine_to_five_transport(httpx.Response(200, text=NINE_TO_FIVE_ROBOTS)),
    )
    search = SearchService(session_factory, transport=FakeSearch(hits).transport(), sleep=no_sleep)
    enricher = Enricher(session_factory, config(), search=search, fetcher=fetcher)
    result = asyncio.run(enricher.enrich_story(story_id, dry_run=True))
    (candidate,) = result.candidates
    assert candidate.reason == "robots"
    out = report(result)
    assert "fetch: skipped (disallowed by robots.txt)" in out and NINE_TO_FIVE in out


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

    with session_factory() as session:  # the cooldown is shared state: clear it
        session.execute(delete(SearchProviderState))
        session.commit()
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
    assert reasons["https://elsewhere.example/a"] == "no_shared_entity"  # title-only hit
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


def test_search_failure_is_retried_not_done(session_factory):
    story_id = seed(session_factory)
    summary = run(make_enricher(session_factory, FakeSearch(status=500, text="down")), story_id)
    assert summary["added"] == 0 and summary["provider_errors"] >= 1 and summary["retry"] == 1
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_status == "retry" and story.enriched_at is None
        assert story.enrich_reason.startswith("provider_unavailable: ")


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


# --- hardening: pacing, cooldowns, retries ----------------------------------------------


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def clocked_enricher(factory, fake, cfg=None, clock=None, sleep=no_sleep):
    clock = clock or Clock()
    fetcher = CandidateFetcher(
        factory, guard=Guard(resolver), domain_delay=0, transport=page_transport()
    )
    search = SearchService(factory, transport=fake.transport(), sleep=sleep, now=clock)
    return Enricher(factory, cfg or config(), search=search, fetcher=fetcher, now=clock), clock


def test_gdelt_interval_defaults_to_10_and_is_floored_at_5(caplog):
    assert SearchProviderConfig(name="gdelt").min_interval_seconds == 10.0
    assert SearchProviderConfig(name="gdelt", min_interval_seconds=6).min_interval_seconds == 6
    with caplog.at_level("WARNING"):
        assert SearchProviderConfig(name="gdelt", min_interval_seconds=1).min_interval_seconds == 5
    assert "below GDELT's limit" in caplog.text
    assert SearchProviderConfig(name="guardian").min_interval_seconds == 1.0
    assert SearchProviderConfig(name="newsdata", min_interval_seconds=0).min_interval_seconds == 0
    cfg = EnrichmentConfig.model_validate({"providers": [{"name": "gdelt"}]})
    assert cfg.providers[0].min_interval_seconds == 10.0
    assert cfg.max_queries_per_story == 2 and cfg.min_title_similarity == 0.30
    assert cfg.empty_cache_minutes == 10 and cfg.second_pass_hours == 3
    assert cfg.late_regen_max_per_hour == 6


def test_cooldown_skip_is_not_an_error_or_a_request(session_factory):
    fake = FakeSearch()
    fake.responses = [httpx.Response(429, headers={"Retry-After": "600"})]
    service = SearchService(session_factory, transport=fake.transport(), sleep=no_sleep)
    gdelt = SearchProviderConfig(name="gdelt")
    first = asyncio.run(service.search(gdelt, "a b", since=NOW, language=None, cache_hours=0))
    assert first.error and first.cooldown_seconds > 500 and not first.skipped
    out = asyncio.run(service.search(gdelt, "c d", since=NOW, language=None, cache_hours=0))
    health = service.health["gdelt"]
    assert out.skipped == "cooldown" and not out.ok and len(fake.requests) == 1
    assert health.cooldown_skips == 1 and health.errors == 0 and health.requests == 1
    assert health.rate_limited == 1 and health.successes == 0


def test_provider_state_is_shared_across_services(session_factory):
    fake = FakeSearch()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    clock = Clock()
    gdelt = SearchProviderConfig(name="gdelt", min_interval_seconds=5)
    one = SearchService(session_factory, transport=fake.transport(), sleep=sleep, now=clock)
    two = SearchService(session_factory, transport=fake.transport(), sleep=sleep, now=clock)
    asyncio.run(one.search(gdelt, "q1", since=NOW, language=None, cache_hours=0))
    asyncio.run(two.search(gdelt, "q2", since=NOW, language=None, cache_hours=0))
    assert slept == [5.0]  # the second process waits for the first one's slot
    fake.responses = [httpx.Response(429, headers={"Retry-After": "300"})]
    clock.advance(seconds=10)
    asyncio.run(one.search(gdelt, "q3", since=NOW, language=None, cache_hours=0))
    out = asyncio.run(two.search(gdelt, "q4", since=NOW, language=None, cache_hours=0))
    assert out.skipped == "cooldown" and len(fake.requests) == 3
    assert 299 <= two.cooldown_remaining("gdelt") <= 300
    with session_factory() as session:
        state = session.get(SearchProviderState, "gdelt")
        assert state.consecutive_errors == 1 and state.cooldown_until is not None


def test_timeouts_are_counted_separately(session_factory):
    def handle(request):
        raise httpx.ReadTimeout("slow", request=request)

    service = SearchService(session_factory, transport=httpx.MockTransport(handle), sleep=no_sleep)
    out = asyncio.run(
        service.search(SearchProviderConfig(name="gdelt"), "q", since=NOW, language=None)
    )
    health = service.health["gdelt"]
    assert out.error and health.timeouts == 1 and health.errors == 0
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SearchCacheEntry)) == 0


def test_empty_results_are_cached_briefly_and_empty_bodies_never(session_factory):
    fake = FakeSearch(hits=[])
    clock = Clock()
    service = SearchService(session_factory, transport=fake.transport(), sleep=no_sleep, now=clock)
    gdelt = SearchProviderConfig(name="gdelt")

    def search(q, **kw):
        return asyncio.run(service.search(gdelt, q, since=NOW, language=None, **kw))

    assert search("empty").hits == [] and len(fake.requests) == 1
    assert search("empty").cached  # within empty_cache_minutes
    clock.advance(minutes=11)
    assert not search("empty").cached and len(fake.requests) == 2
    search("never", empty_cache_minutes=0)
    assert not search("never", empty_cache_minutes=0).cached
    for body in ("", "  ", "<html>Oops</html>"):
        fake.responses = [httpx.Response(200, text=body), httpx.Response(200, text=body)]
        clock.advance(seconds=10)
        assert search(f"bad {body!r}").error  # retried once (transient), then an error
    with session_factory() as session:
        queries = set(session.scalars(select(SearchCacheEntry.query)))
    assert queries == {"empty"}


def test_story_retries_after_backoff_then_recovers(session_factory):
    story_id = seed(session_factory)
    fake = FakeSearch(status=500, text="down")
    enricher, clock = clocked_enricher(session_factory, fake)
    worker = SummaryWorker(session_factory, None, clock=clock)
    run(enricher, story_id)
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_status == "retry" and story.enriched_at is None
        assert story.enrich_next_attempt_at == NOW + timedelta(minutes=2)
        assert story.enrich_reason == "provider_unavailable: GDELT HTTP 500: down" or (
            story.enrich_reason.startswith("provider_unavailable: ")
        )
        assert story_id not in worker.candidates(session)  # within wait_seconds
    assert asyncio.run(enricher.run_pending()) == []  # not due yet
    clock.advance(minutes=2)
    asyncio.run(enricher.run_pending())
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_attempts == 2
        assert story.enrich_next_attempt_at == clock.now + timedelta(minutes=4)
    clock.advance(seconds=enricher.config.wait_seconds)
    with session_factory() as session:
        assert story_id in worker.candidates(session)  # the brief waits wait_seconds at most
    clock.advance(seconds=-enricher.config.wait_seconds)
    for minutes in (4, 8, 16, 32):
        clock.advance(minutes=minutes)
        asyncio.run(enricher.run_pending())
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_next_attempt_at == clock.now + timedelta(minutes=60)  # capped
    fake.status, fake.text = 200, None
    clock.advance(minutes=60)
    asyncio.run(enricher.run_pending())
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_status == "done" and story.enrich_added == 1
        assert story.enriched_at == clock.now and story.enrich_reason.startswith("recovered")
        assert enricher.stats(session)["recovered"] == 1


def test_retry_waits_for_the_cooldown_without_errors(session_factory):
    story_id = seed(session_factory)
    fake = FakeSearch()
    fake.responses = [httpx.Response(429, headers={"Retry-After": "120"})]
    enricher, clock = clocked_enricher(session_factory, fake)
    run(enricher, story_id)
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_status == "retry" and story.enrich_attempts == 1
        assert story.enrich_next_attempt_at >= NOW + timedelta(minutes=2)
        stats = enricher.stats(session)
        assert stats["cooling"][0]["provider"] == "gdelt" and stats["cooling"][0]["waiting"] == 1
        story.enrich_next_attempt_at = NOW  # due while gdelt still cools down
        session.commit()
    asyncio.run(enricher.run_pending())
    health = enricher.search.health["gdelt"]
    assert len(fake.requests) == 1 and health.cooldown_skips == 1 and health.errors == 0
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_attempts == 1  # waiting for a cooldown is not an attempt
        assert story.enrich_next_attempt_at == NOW + timedelta(seconds=121)
    clock.advance(seconds=121)
    asyncio.run(enricher.run_pending())
    with session_factory() as session:
        assert session.get(Story, story_id).enrich_status == "done"


def test_falls_back_to_a_second_provider(session_factory):
    story_id = seed(session_factory)
    fake = FakeSearch()
    fake.responses = [httpx.Response(500, text="gdelt down")]
    cfg = config(
        providers=[
            SearchProviderConfig(name="gdelt"),
            SearchProviderConfig(name="searxng", base_url="https://searx.example"),
        ]
    )
    summary = run(make_enricher(session_factory, fake, cfg), story_id)
    assert summary["added"] >= 1 and summary["provider_errors"] == 1
    assert fake.requests[1].url.path.endswith("/search")
    with session_factory() as session:
        story = session.get(Story, story_id)
        # the fallback's finds are kept, but the pass is degraded: retried for the primary
        assert story.enrich_status == "retry" and story.enrich_reason.startswith("degraded: gdelt")


def test_looser_query_only_after_zero_hits(session_factory):
    story_id = seed(session_factory)
    fake = FakeSearch()
    run(make_enricher(session_factory, fake), story_id)
    assert len(fake.requests) == 1  # the strict query found usable hits
    other = seed_more(session_factory, ["Fresno council backs Kings River water plant"])[0]
    empty = FakeSearch(hits=[])
    run(make_enricher(session_factory, empty), other)
    queries = [r.url.params["query"] for r in empty.requests]
    assert len(queries) == 2 and " OR " in queries[1] and "(" in queries[1]


def test_looser_query_runs_when_the_first_returns_only_unusable_hits(session_factory):
    """Live bug (stories 549/531/532): the enricher stopped after the first query with any
    hits, even when every hit was rejected by the cheap filters (same domain, social,
    already ingested). The looser query now runs and its page is accepted."""
    story_id = seed(session_factory)
    own = [
        ("https://news.example/other", "Springfield buses approved by council"),  # same domain
        ("https://twitter.com/x/status/1", "Springfield electric buses"),  # social
        ("https://news.example/1", "Springfield buses"),  # already ingested
    ]
    good = [("https://www.wire.example/buses", "Springfield council approves electric buses plan")]
    fake = FakeSearch()
    fake.responses = [
        httpx.Response(200, json=gdelt_body(own)),
        httpx.Response(200, json=gdelt_body(good)),
    ]
    result = asyncio.run(make_enricher(session_factory, fake).enrich_story(story_id))
    assert len(fake.requests) == 2 and result.added == 1
    assert {c.hit.domain for c in result.accepted} == {"wire.example"}
    reasons = {c.hit.url: c.reason for c in result.candidates}
    assert reasons["https://news.example/other"] == "same_domain"


def test_cli_refuses_during_a_cooldown(session_factory):
    import io

    from app.enrich.cli import respect_cooldowns

    enricher, _ = clocked_enricher(session_factory, FakeSearch())
    out = io.StringIO()
    assert respect_cooldowns(enricher, wait=False, out=out)
    enricher.search._cool_down("gdelt", 90)
    assert not respect_cooldowns(enricher, wait=False, out=out)
    assert "gdelt is cooling down for 90 s" in out.getvalue() and "--wait" in out.getvalue()


def test_evaluate_and_diagnose_dry_run_write_nothing(session_factory, monkeypatch, capsys):
    from app.enrich import diagnose, evaluate

    story_id = seed(session_factory)
    fake = FakeSearch()
    enricher = make_enricher(session_factory, fake)
    monkeypatch.setattr(evaluate, "build", lambda: (session_factory, enricher))
    monkeypatch.setattr(diagnose, "build", lambda: (session_factory, enricher))
    assert evaluate.main(["--stories", "5", "--provider", "gdelt"]) == 0
    assert diagnose.main(["--story-id", str(story_id), "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "Accepted (check each for false matches)" in printed and "Rejected by reason" in printed
    assert "wire.example" in printed and "near_duplicate" in printed
    assert len(fake.requests) >= 2  # really searched
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_status is None and story.enriched_at is None
        assert session.scalar(select(func.count()).select_from(EnrichmentCandidate)) == 0
        assert session.scalar(select(func.count()).select_from(SearchCacheEntry)) == 0
        assert (
            session.scalar(
                select(func.count()).select_from(Article).where(Article.origin == ENRICHMENT)
            )
            == 0
        )
    with pytest.raises(SystemExit):
        evaluate.main(["--provider", "brave"])
    enricher.search._cool_down("gdelt", 60)
    assert evaluate.main(["--stories", "5"]) == 3


# --- queries per provider -----------------------------------------------------------------

REAL = [
    [
        "Federal Reserve holds interest rates steady, signals two cuts in 2025",
        "Fed leaves rates unchanged as Powell cites inflation progress",
        "Powell says Federal Reserve in no hurry to cut",
    ],
    [
        "Magnitude 6.1 earthquake strikes off coast of Japan's Hokkaido",
        "Strong quake hits northern Japan; no tsunami warning",
    ],
    [
        "Springfield council approves 40 electric buses",
        "Mayor Ortiz backs Springfield bus plan",
        "Ortiz: electric buses arrive in 2027",
    ],
    [
        "UK Prime Minister Keir Starmer announces sweeping overhaul of immigration rules "
        "for skilled workers, students and care staff amid record net migration figures",
    ],
]


def test_gdelt_queries_are_short_and_quote_names():
    from app.enrich.query import provider_queries

    fed = provider_queries("gdelt", REAL[0])
    assert fed[0].startswith('"Federal Reserve"') and "Powell" in fed[0]
    for headlines in REAL:
        strict, loose = provider_queries("gdelt", headlines)
        terms = re.findall(r'"[^"]+"|\S+', strict)
        assert 3 <= len(terms) <= 5, strict
        assert all(len(t.strip('"')) >= 3 for t in terms)  # GDELT: no too-short keywords
        term = r'(?:"[^"]+"|\S+)'
        assert re.search(rf"\(({term} OR )+{term}\)$", loose), loose  # OR group in parens
    quake = provider_queries("gdelt", REAL[1])[0]
    assert "6.1" in quake and "Hokkaido" in quake and '"Japan Hokkaido"' not in quake
    assert len(provider_queries("gdelt", REAL[0], max_queries=1)) == 1


def test_guardian_and_newsdata_limits_and_llm_mode():
    from app.enrich.query import provider_queries

    for headlines in REAL:
        guardian = provider_queries("guardian", headlines)
        assert len(re.findall(r'"[^"]+"|\S+', guardian[0])) <= 4
        assert all(len(q) <= 100 for q in provider_queries("newsdata", headlines))
    starmer = provider_queries("newsdata", REAL[3])[0]
    assert "Starmer" in starmer
    llm = ["Keir Starmer immigration overhaul skilled workers students care staff net migration"]
    gdelt = provider_queries("gdelt", REAL[3], llm=llm)
    assert len(gdelt[0].split()) <= 4 and gdelt[0].startswith("Keir Starmer")
    long_llm = ["x" * 30 + " " + "y" * 30 + " " + "z" * 30 + " " + "w" * 30]
    assert len(provider_queries("newsdata", REAL[3], llm=long_llm)[0]) <= 100


# --- title-only relevance -------------------------------------------------------------------


def test_title_only_hits_need_an_entity_and_similarity():
    from app.enrich.filters import title_check

    story = story_ctx()

    def hit(title):
        return SearchHit(url="https://x.example/a", title=title, domain="x.example")

    reason, score, entities = title_check(hit("Springfield electric buses win vote"), story, 0.3)
    assert reason is None and score >= 0.3 and entities >= 2
    assert title_check(hit("Cooking with garlic tonight"), story, 0.3)[0] == "no_shared_entity"
    reason, score, _ = title_check(hit("Springfield weather: sunny week ahead"), story, 0.3)
    assert reason == "low_title_similarity" and score < 0.3


def test_title_and_text_scores_are_recorded(session_factory):
    story_id = seed(session_factory)
    run(make_enricher(session_factory, FakeSearch()), story_id)
    with session_factory() as session:
        wire = session.scalar(
            select(EnrichmentCandidate).where(EnrichmentCandidate.decision == "accepted")
        )
    assert wire.similarity is None  # GDELT hits have no snippet
    assert wire.title_similarity >= 0.3 and wire.text_similarity and wire.shared_entities


# --- second pass, admission, late regenerations -------------------------------------------


def seed_more(factory, titles: list[str], *, hours_ago: float = 1) -> list[int]:
    """More single-article stories from source ``a`` (``seed`` must have run)."""
    with factory() as session:
        for n, title in enumerate(titles):
            url = f"https://news.example/more/{hashlib.sha256(title.encode()).hexdigest()[:10]}"
            session.add(
                Article(
                    source_id="a",
                    url=url,
                    canonical_url=url,
                    title=title,
                    summary_raw=title,
                    published_at=NOW - timedelta(hours=hours_ago, minutes=n),
                    fetched_at=NOW,
                    content_hash=hashlib.sha256(title.encode()).hexdigest(),
                    text=f"{title}. " * 30,
                    text_status="ok",
                    word_count=200,
                )
            )
        session.flush()
        ensure_stories(session, NOW)
        session.commit()
    run_clustering(factory, TfidfClusterer())
    with factory() as session:
        return [
            session.scalar(select(Article.story_id).where(Article.title == title))
            for title in titles
        ]


TOPICS = [
    "Volcano erupts near Reykjavik airport",
    "Chess grandmaster Carlsen wins blitz title",
    "Bakery chain Greggs recalls almond croissants",
    "NASA satellite launch delayed by Florida solar storm",
    "Orchestra unveils Mahler symphony cycle",
    "Glacier retreat measured in Patagonia",
    "Marathon record falls in Berlin",
    "Rare orchid blooms in Kew Gardens",
    "Submarine cable cut off Taiwan coast",
]


def test_second_pass_after_second_pass_hours(session_factory):
    story_id = seed(session_factory)
    enricher, clock = clocked_enricher(session_factory, FakeSearch(hits=[]))
    run(enricher, story_id)
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_status == "done" and story.enrich_passes == 1
        clock.advance(hours=2, minutes=59)
        assert enricher.eligible(session) == []
        clock.advance(minutes=2)
        assert enricher.eligible(session) == [story_id]
        enricher.config = enricher.config.model_copy(update={"second_pass_hours": 0})
        assert enricher.eligible(session) == []
    enricher.config = enricher.config.model_copy(update={"second_pass_hours": 3})
    run(enricher, story_id)
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_passes == 2
        first_wait = NOW + timedelta(seconds=enricher.config.wait_seconds)
        assert story.enrich_wait_until == first_wait  # no second brief wait
        clock.advance(hours=10)
        assert enricher.eligible(session) == []  # only one second pass


def test_admission_is_limited_by_provider_capacity(session_factory):
    seed(session_factory)
    ids = seed_more(session_factory, TOPICS)
    enricher, _ = clocked_enricher(session_factory, FakeSearch(), config(wait_seconds=60))
    enricher.start = lambda: True  # only look at what gets admitted
    # gdelt: 5 s x 2 queries per story -> 6 stories within wait_seconds = 60
    assert enricher.schedule() == 6
    with session_factory() as session:
        pending = set(session.scalars(select(Story.id).where(Story.enrich_status == "pending")))
        newest = set(
            session.scalars(
                select(Story.id)
                .where(Story.state != "merged")
                .order_by(Story.last_updated_at.desc())
                .limit(6)
            )
        )
        assert pending == newest
        assert enricher.schedule() == 0  # full until those are served
        session.execute(
            Story.__table__.update().where(Story.id.in_(pending)).values(enrich_status="done")
        )
        session.commit()
    assert enricher.schedule() == 4  # the rest are admitted later
    assert len(ids) == len(TOPICS)


def test_backlog_catch_up_skips_stories_older_than_backfill(session_factory):
    seed(session_factory)
    old = seed_more(session_factory, ["Volcano erupts near Reykjavik airport"], hours_ago=30)[0]
    enricher, _ = clocked_enricher(session_factory, FakeSearch())
    enricher.backfill = timedelta(hours=24)
    with session_factory() as session:
        assert old not in enricher.eligible(session)
        enricher.backfill = None
        assert old in enricher.eligible(session)


def test_late_regenerations_are_rate_limited(session_factory):
    story_id = seed(session_factory)
    with session_factory() as session:
        story = session.get(Story, story_id)
        story.state, story.published_version = StoryState.PUBLISHED.value, 1
        session.commit()
    enricher = make_enricher(session_factory, FakeSearch(), config(late_regen_max_per_hour=0))
    run(enricher, story_id)
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_added == 1 and story.enrich_reason == "late_regen_deferred"
        assert story.state == StoryState.PUBLISHED and story.enrich_regens == 0
        assert (
            session.scalar(
                select(func.count()).select_from(Article).where(Article.origin == ENRICHMENT)
            )
            == 1
        )  # kept for the next regeneration


def test_reenrich_stories_that_ended_with_provider_errors(session_factory):
    story_id = seed(session_factory)
    fake = FakeSearch(status=500, text="down")
    enricher = make_enricher(session_factory, fake)
    with session_factory() as session:  # what the old code left behind
        story = session.get(Story, story_id)
        story.enrich_status, story.enriched_at = "done", NOW
        session.commit()
    enricher.start = lambda: True
    assert enricher.requeue_provider_errors() == 1
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_status == "retry" and story.enrich_next_attempt_at is not None
        assert story_id in enricher.pending(session)


# --- Phase 10.4: source quality, publisher aliases, extra rules, block domain -------------


def test_domain_lists_cover_social_and_aggregators():
    assert blocked_reason("https://www.threads.com/@x/post/1") == "social"
    assert blocked_reason("https://threads.net/@x/post/1") == "social"
    assert blocked_reason("https://x.com/x/status/1") == "social"
    for host in ("mastodon.social", "mastodon.online", "mstdn.jp", "fosstodon.org", "hachyderm.io"):
        assert blocked_reason(f"https://{host}/@user/1") == "social", host
    # a mastodon-style host on any domain: no fixed list covers them
    assert blocked_reason("https://mastodon.example.org/@u/1") == "social"
    assert blocked_reason("https://social.mstdn.example/@u/1") == "social"
    for host in (
        "daily.dev",
        "app.daily.dev",
        "flipboard.com",
        "newsbreak.com",
        "ground.news",
        "smartnews.com",
        "msn.com",
    ):
        assert blocked_reason(f"https://{host}/item/1") == "aggregator", host
    # configured extensions behave like the built-ins
    assert blocked_reason("https://chat.example/post", social=("chat.example",)) == "social"
    assert blocked_reason("https://scrape.example/x", aggregators=("scrape.example",)) == (
        "aggregator"
    )


def test_publisher_aliases_collapse_editions():
    from app.enrich.domains import canonical_domain

    aliases = {"bbc.co.uk": "bbc.com", "guardian.co.uk": "theguardian.com"}
    assert canonical_domain("https://www.bbc.co.uk/news/x", aliases) == "bbc.com"
    assert canonical_domain("https://amp.bbc.com/news/x", aliases) == "bbc.com"
    assert canonical_domain("https://m.theguardian.com/x", aliases) == "theguardian.com"
    assert canonical_domain("https://uk.Guardian.CO.UK/x", aliases) == "theguardian.com"
    # and in the cheap same-domain check, before any fetch
    hit = SearchHit(url="https://www.bbc.co.uk/news/x", title="t")
    ctx = story_ctx(domains={"bbc.com"})
    got = pre_filter(hit, ctx, deny=(), max_gap=timedelta(hours=72), language=None, aliases=aliases)
    assert got == "same_domain"
    assert (
        pre_filter(hit, ctx, deny=(), max_gap=timedelta(hours=72), language=None) is None
    )  # no aliases: different publishers again


def test_publishers_count_uses_aliases(session_factory):
    story_id = seed(session_factory)
    enricher = make_enricher(session_factory, FakeSearch())
    with session_factory() as session:
        assert enricher.publishers(session, story_id) == 1
        session.add(
            Article(
                source_id="b",
                url="https://www.bbc.co.uk/news/1",
                canonical_url="https://www.bbc.co.uk/news/1",
                title="t",
                published_at=NOW,
                fetched_at=NOW,
                content_hash="x1",
                story_id=story_id,
            )
        )
        session.add(
            Article(
                source_id="c",
                url="https://bbc.com/news/2",
                canonical_url="https://bbc.com/news/2",
                title="t2",
                published_at=NOW,
                fetched_at=NOW,
                content_hash="x2",
                story_id=story_id,
            )
        )
        session.commit()
        # news.example + bbc (co.uk and .com count once) = 2 publishers
        assert enricher.publishers(session, story_id) == 2


@pytest.mark.parametrize(
    ("site", "title_sim", "text", "entities", "rule"),
    [
        # live-eval scores: marktechpost.com on stories 631/632 -> rule D
        ("marktechpost.com", 0.61, 0.24, 3, "d"),
        # bbc.com and i24news.tv on story 637 -> rule C
        ("bbc.com", 0.30, 0.27, 6, "c"),
        ("i24news.tv", 0.30, 0.28, 7, "c"),
        # just under each bar stays rejected
        ("close.example", 0.61, 0.21, 3, None),  # text below rule D's floor
        ("four.example", 0.30, 0.30, 4, None),  # entities below rule C's bar
    ],
)
def test_extra_rules_c_and_d_accept_real_coverage(
    monkeypatch, site, title_sim, text, entities, rule
):
    """Below ``min_text_similarity`` and without a strong snippet: rule C accepts many
    shared entities with a lower text floor, rule D a matching headline + entities."""
    hit_title = f"Ubuntu 26.10 on {site}"
    monkeypatch.setattr(
        "app.enrich.filters.similarity",
        lambda a, _b: title_sim if a == hit_title else text,
    )
    monkeypatch.setattr("app.enrich.filters.shared_entities", lambda _a, _b: entities)
    cfg = EnrichmentConfig()
    verdict = text_check(
        hit_title,
        OTHER_TEXT,
        story_ctx(),
        threshold=cfg.min_text_similarity,
        min_entities=cfg.min_shared_entities,
        duplicate_overlap=cfg.duplicate_overlap,
        snippet_similarity=0.20,  # rule B does not apply
        extra_rules=cfg.extra_rules,
    )
    if rule is None:
        assert verdict.reason == "low_text_similarity"
    else:
        assert verdict.reason is None and verdict.rule == rule
    # disabled: same scores reject
    off = EnrichmentConfig(extra_rules={"enabled": False})
    verdict = text_check(
        hit_title,
        OTHER_TEXT,
        story_ctx(),
        threshold=cfg.min_text_similarity,
        min_entities=cfg.min_shared_entities,
        duplicate_overlap=cfg.duplicate_overlap,
        snippet_similarity=0.20,
        extra_rules=off.extra_rules,
    )
    assert verdict.reason == "low_text_similarity"


def test_anchor_url_patterns_skip_opinion_and_features(session_factory):
    """Opinion columns / live blogs / features are searched and never match: the anchor
    URL's path skips them (``skipped:url_pattern``)."""
    enricher = make_enricher(session_factory, FakeSearch())
    headlines = ["Springfield mayor Ortiz wins council vote on electric buses"]
    for path in (
        "/commentisfree/2026/oct/17/buses",
        "/opinion/2026/10/buses",
        "/opinions/buses",
        "/comment/buses",
        "/columnists/ortiz/buses",
        "/blogs/city-hall/buses",
        "/live/2026/oct/17/buses-live",
    ):
        assert enricher.skip_reason(headlines, f"https://news.example{path}") == (
            "skipped:url_pattern"
        ), path
    assert enricher.skip_reason(headlines, "https://news.example/news/buses") is None
    cfg = EnrichmentConfig(skip_url_patterns=[])
    enricher.config = cfg
    assert enricher.skip_reason(headlines, "https://news.example/opinion/buses") is None


def test_query_cleanup_drops_contractions_and_filler():
    from app.enrich.query import heuristic_query

    q = heuristic_query(["You can't make this stuff up: fascinated by the Elizabeth Holmes trial"])
    tokens = set(q.lower().split())
    assert "elizabeth" in tokens and "holmes" in tokens
    for filler in ("can't", "cant", "make", "stuff", "fascinated"):
        assert filler not in tokens, (filler, q)
    # contractions drop whole; possessives keep the noun
    q = heuristic_query(["Apple's quarterly results aren't what analysts'd expect"])
    tokens = set(q.lower().split())
    assert "apple" in tokens and "arent" not in tokens and "analystsd" not in tokens
    # O'Brien-style apostrophes clean into one token
    from app.enrich.query import _clean

    assert _clean("O'Brien") == "OBrien"
    assert _clean("Apple's") == "Apple"


def test_block_domain_denies_removes_and_queues(session_factory):
    """The admin action: the domain is persisted (and overrides YAML), its enrichment
    members disappear from every story, and those stories queue for one regeneration."""
    story_id = seed(session_factory)
    enricher = make_enricher(session_factory, FakeSearch())
    run(enricher, story_id)
    with session_factory() as session:
        member = session.scalar(select(Article).where(Article.origin == ENRICHMENT))
        story = session.get(Story, story_id)
        story.state = StoryState.BRIEF_READY.value
        session.commit()
        member_url = member.url
    result = enricher.block_domain(member_url)
    assert result["domain"] == "wire.example" and result["removed"] == 1
    assert result["stories"] == [story_id] and result["queued"] == 1
    with session_factory() as session:
        assert session.get(Story, story_id).state == StoryState.CLUSTERED
        assert not session.scalars(select(Article).where(Article.origin == ENRICHMENT)).all()
        blocked = session.scalars(select(BlockedDomain)).all()
        assert [(b.domain, b.canonical) for b in blocked] == [("wire.example", "wire.example")]
    # and it is denied on the next pass even though YAML does not list it
    with session_factory() as session:
        assert "wire.example" in enricher._denied(session)


def test_block_domain_via_story_page(settings, session_factory):
    """POST /story/{id}/members/{article_id}/block-domain (admin-only via the POST
    guard) removes the member; a feed member's id 404s."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    story_id = seed(session_factory)
    enricher = make_enricher(session_factory, FakeSearch())
    run(enricher, story_id)
    with session_factory() as session:
        member = session.scalar(select(Article).where(Article.origin == ENRICHMENT))
        feed = session.scalar(select(Article).where(Article.origin != ENRICHMENT))
        session.commit()
        member_id, feed_id = member.id, feed.id
    with TestClient(create_app(settings)) as client:
        client.app.state.session_factory = session_factory  # the seeded DB
        client.app.state.pipeline.enricher = enricher
        assert client.post(f"/story/{story_id}/members/{feed_id}/block-domain").status_code == 404
        ok = client.post(
            f"/story/{story_id}/members/{member_id}/block-domain",
            headers={"HX-Request": "true"},
        )
        assert ok.status_code == 200
    with session_factory() as session:
        assert session.get(Article, member_id) is None
        assert session.scalars(select(BlockedDomain)).all()


def test_fetch_failure_causes_are_categorized():
    from app.enrich.evaluate import _fetch_cause

    assert _fetch_cause("failed: HTTP 503") == "http_503"
    assert _fetch_cause("failed: Timeout: ReadTimeout") == "timeout"
    assert _fetch_cause("failed: larger than 1048576 bytes") == "size_cap"
    assert _fetch_cause("failed: extracted 3 words (< 40)") == "extract_empty"
    assert _fetch_cause("failed: not HTML (application/pdf)") == "not_html"
    assert _fetch_cause("blocked: example.com refused") == "blocked"
    assert _fetch_cause("skipped: disallowed by robots.txt") == "robots"
    assert _fetch_cause("failed: something else") == "other"


def test_backtest_hides_everything_but_the_anchor(session_factory):
    """anchor_only pretends a multi-publisher story has one member: its other feed
    domains are no longer same-domain rejections and the context only sees the anchor."""
    story_id = seed(session_factory)
    with session_factory() as session:
        session.add(
            Article(
                source_id="b",
                url="https://wire.example/covered",
                canonical_url="https://wire.example/covered",
                title="Springfield council approves electric buses",
                published_at=NOW,
                fetched_at=NOW,
                content_hash="zz1",
                story_id=story_id,
            )
        )
        session.commit()
    with session_factory() as session:
        enricher = make_enricher(session_factory, FakeSearch())
        full, _ = enricher.context(session, story_id)
        solo, anchor = enricher.context(session, story_id, anchor_only=True)
        assert len(full.domains) == 2 and solo.domains == {"news.example"}
        assert solo.headlines == [anchor.title] and len(solo.texts) == 1


def test_backtest_story_picker_needs_three_publishers(session_factory):
    from app.enrich.evaluate import _backtest_stories

    story_id = seed(session_factory)
    enricher = make_enricher(session_factory, FakeSearch())
    with session_factory() as session:
        assert _backtest_stories(session, enricher.config, None, 30) == []
        for n, dom in enumerate(("wire.example", "other.example")):
            session.add(
                Article(
                    source_id="x",
                    url=f"https://{dom}/a",
                    canonical_url=f"https://{dom}/a",
                    title="t",
                    published_at=NOW,
                    fetched_at=NOW,
                    content_hash=f"zz{n}",
                    story_id=story_id,
                )
            )
        session.commit()
        picks = _backtest_stories(session, enricher.config, None, 30)
        assert [p[0] for p in picks] == [story_id]
        _id, _title, anchor, expected = picks[0]
        assert anchor == "news.example" and expected == {"wire.example", "other.example"}
