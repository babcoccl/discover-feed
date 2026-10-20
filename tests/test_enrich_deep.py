"""Deep-coverage tier (``enrichment.depth``): admission, the query/page/location
fan-out, ensure-groups include queries, the daily request budget, headline-only
members and the outlets-per-story stats. Fake providers only — no network."""

import asyncio
import hashlib
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import select

from app.config import EnrichmentConfig, EnrichmentDepthConfig, SearchProviderConfig
from app.enrich.evaluate import summary
from app.enrich.service import DEEP_USAGE, Candidate, StoryEnrichment
from app.models import AlertFiring, Article, ProfileRecord, SourceRecord, SourceUsage, Story
from app.search.base import SearchHit
from tests.test_enrich import (
    NOW,
    PAGES,
    TEXT,
    FakeSearch,
    make_enricher,
    page,
    seed,
)

TF = SearchProviderConfig(
    name="tinyfish", base_url="http://tinyfish.test/search", min_interval_seconds=0
)

DEEP = EnrichmentDepthConfig(
    deep_min_publishers=2,
    target_sources=6,
    max_added_sources=6,
    max_queries_per_story=2,
    pages=2,
    locations=["US", "GB", "AU"],
)

# Every page/location a paged fake answer can point at (one host per extra answer —
# distinct canonical domains like the docker fixture's wire* aliases). Each body is
# its own wording of the same event so the candidates are relevant without tripping
# the near-duplicate shingle check.
_DEEP_BODIES = {
    "wire-p1.example": (
        "The Springfield city council signed off on a forty-bus electric fleet deal "
        "on Monday, a clear victory for Mayor Lena Ortiz. The transit authority says "
        "Central Station gets rebuilt while depots are upgraded for charging. "
    )
    * 5,
    "wire-us.example": (
        "Mayor Lena Ortiz confirmed Springfield's forty electric bus purchase Monday. "
        "Under the council-backed plan the transit authority will also rebuild Central "
        "Station, ending three months of debate over fares and depot work. "
    )
    * 5,
    "wire-gb.example": (
        "Springfield's transit authority is buying forty battery-electric buses after "
        "the council backed Mayor Lena Ortiz on Monday. The package includes a full "
        "Central Station rebuild that union critics call rushed. "
    )
    * 5,
    "wire-au.example": (
        "Ortiz's Springfield administration won approval for forty electric buses this "
        "week as councillors ended a long debate on fares. Central Station will be "
        "rebuilt and depots converted for charging the new fleet. "
    )
    * 5,
    "wire-local.example": (
        "Local officials in Springfield confirmed the forty-bus electric order on "
        "Monday as Mayor Lena Ortiz and the council closed out the fares debate. The "
        "Central Station rebuild moves ahead with the depot conversions, though the "
        "transit union objects to the rushed timeline. "
    )
    * 5,
}
for host, body in _DEEP_BODIES.items():
    PAGES[f"https://{host}/buses"] = page("Springfield approves electric buses", body)


class PagingSearch(FakeSearch):
    """Fake TinyFish honoring ``page``/``location``/``include_domains``: each extra
    answer's single hit lives on a distinct ``*.example`` host (a new publisher)."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "tinyfish" not in request.url.host:
            return super().__call__(request)
        params = request.url.params
        include = [d for d in (params.get("include_domains") or "").split(",") if d]
        location = params.get("location") or ""
        page_no = int(params.get("page") or 0)
        if include:
            host = include[0]
        elif location:
            host = f"wire-{location.lower()}.example"
        elif page_no:
            host = f"wire-p{page_no}.example"
        else:
            host = "www.wire.example"
        url = f"https://{host}/buses"
        body = {
            "query": params.get("query"),
            "results": [
                {
                    "position": 1,
                    "site_name": host,
                    "title": "Springfield council approves electric buses plan",
                    "snippet": "Springfield council approves electric buses plan",
                    "url": url,
                    "date": NOW.isoformat(),
                }
            ],
            "total_results": 1,
            "page": page_no,
        }
        return httpx.Response(200, json=body)


def seed2(factory) -> int:
    """A clustered story with members from two feed publishers (deep-admitted)."""
    with factory() as session:
        session.add(SourceRecord(id="a", name="News A", url="https://news.example/feed"))
        session.add(SourceRecord(id="b", name="Gazette B", url="https://gazette.example/feed"))
        profile = ProfileRecord(slug="me", name="Me")
        session.add(profile)
        session.flush()
        profile.sources = [
            session.get(SourceRecord, "a"),
            session.get(SourceRecord, "b"),
        ]
        title = "Springfield mayor Ortiz wins council vote on electric buses"
        for n, host in enumerate(["news.example", "gazette.example"], start=1):
            session.add(
                Article(
                    source_id="a" if n == 1 else "b",
                    url=f"https://{host}/{n}",
                    canonical_url=f"https://{host}/{n}",
                    title=title,
                    summary_raw=title,
                    published_at=NOW - timedelta(hours=1),
                    fetched_at=NOW,
                    content_hash=hashlib.sha256(f"{title}{host}".encode()).hexdigest(),
                    text=TEXT,
                    text_status="ok",
                    word_count=len(TEXT.split()),
                )
            )
        session.flush()
        from app.lifecycle import ensure_stories

        ensure_stories(session, NOW)
        session.commit()
    from app.cluster import TfidfClusterer
    from app.cluster.service import run_clustering

    run_clustering(factory, TfidfClusterer())
    with factory() as session:
        return session.scalar(select(Story.id).where(Story.state != "merged"))


def deep_enricher(factory, fake, cfg=None):
    base = {
        "enabled": True,
        "providers": [TF],
        "depth": DEEP,
        "max_added_sources": 3,
        "target_sources": 4,
    }
    return make_enricher(factory, fake, cfg or EnrichmentConfig(**base))


def test_depth_config_defaults_and_validation():
    cfg = EnrichmentDepthConfig()
    assert cfg.enabled and cfg.deep_min_publishers == 2 and cfg.target_sources == 12
    assert cfg.locations == ["US", "GB", "AU", "CA", "IN"]
    assert cfg.daily_request_budget == 1500 and cfg.max_group_queries == 3
    assert EnrichmentDepthConfig(locations=["gb", "de"]).locations == ["GB", "DE"]
    with pytest.raises(ValueError, match="country codes"):
        EnrichmentDepthConfig(locations=["united"])
    cfg = EnrichmentConfig()
    assert cfg.depth.enabled and cfg.headline_only_title_similarity == 0.50
    assert cfg.headline_only_min_entities == 2


def test_deep_tier_fans_out_across_pages_and_locations(session_factory):
    """A deep-admitted story gains one accepted source per distinct publisher: page 0/1
    hits then per-location hits — deduped by domain across all calls."""
    story_id = seed2(session_factory)
    fake = PagingSearch()
    enricher = deep_enricher(session_factory, fake)

    result = asyncio.run(enricher.enrich_story(story_id, dry_run=True, force_deep=True))

    assert result.tier == "deep"
    domains = {urlsplit(c.hit.url).hostname for c in result.accepted}
    # need = target 6 - 2 feed publishers = 4: the pass stops at the fourth accepted
    # outlet, so the AU location call never goes out.
    assert domains == {
        "www.wire.example",
        "wire-p1.example",
        "wire-us.example",
        "wire-gb.example",
    }
    pages = {s.get("page") for s in result.searches if s.get("page")}
    locations = {s.get("location") for s in result.searches if s.get("location")}
    assert 1 in pages and locations == {"US", "GB"}
    assert result.deep_requests == len(
        [s for s in result.searches if not s.get("skipped") and not s.get("stopped")]
    )


def test_deep_run_writes_tier_and_members(session_factory):
    story_id = seed2(session_factory)
    enricher = deep_enricher(session_factory, PagingSearch())
    result = asyncio.run(enricher.enrich_story(story_id))
    assert result.tier == "deep" and len(result.accepted) >= 4
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_depth == "deep"
        hosts = {
            urlsplit(u).hostname
            for u in session.scalars(select(Article.url).where(Article.story_id == story_id))
        }
    assert {"news.example", "gazette.example", "wire-us.example"} <= hosts


def test_standard_tier_when_not_admitted(session_factory):
    """Below ``deep_min_publishers`` feed publishers, no priority, no alert: standard."""
    story_id = seed(session_factory)  # one feed publisher only
    enricher = deep_enricher(session_factory, PagingSearch())
    result = asyncio.run(enricher.enrich_story(story_id, dry_run=True))
    assert result.tier == "standard"


def test_deep_admission_by_priority_and_alert(session_factory):
    story_id = seed(session_factory)
    enricher = deep_enricher(session_factory, PagingSearch())
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert not enricher._deep_admitted(session, story)
        story.priority = 0.9
        enricher.config.depth.deep_min_priority = 0.5
        assert enricher._deep_admitted(session, story)
        story.priority = 0.0
        firing = AlertFiring(
            rule_id=1,
            lineage_id=story_id,
            kind="alert",
            slot=0,
            article_id=1,
            source_id="s",
            matched="[]",
        )
        session.add(firing)
        session.commit()
        assert enricher._deep_admitted(session, story)


def test_eligible_admits_deep_stories_over_the_standard_cap(session_factory):
    """A story already at ``target_sources`` is still eligible when the deep tier
    admits it and it is below ``depth.target_sources``."""
    story_id = seed2(session_factory)
    cfg = EnrichmentConfig(
        enabled=True,
        providers=[TF],
        target_sources=2,  # already met by the two feed publishers
        depth=EnrichmentDepthConfig(deep_min_publishers=2, target_sources=6),
    )
    enricher = deep_enricher(session_factory, FakeSearch(), cfg)
    with session_factory() as session:
        assert story_id in enricher.eligible(session)


def test_daily_request_budget_stops_the_deep_pass(session_factory):
    story_id = seed2(session_factory)
    cfg = EnrichmentConfig(
        enabled=True,
        providers=[TF],
        depth=EnrichmentDepthConfig(daily_request_budget=2),
    )
    enricher = deep_enricher(session_factory, PagingSearch(), cfg)
    enricher._record_deep_requests(2)  # today's budget is spent
    assert enricher._deep_budget_left() <= 0
    result = asyncio.run(enricher.enrich_story(story_id, force_deep=True))
    assert result.tier == "deep" and not result.accepted
    assert any(s.get("stopped") == "deep_budget" for s in result.searches)
    with session_factory() as session:
        row = session.scalar(select(SourceUsage).where(SourceUsage.source_id == DEEP_USAGE))
        assert row.requests == 2  # nothing was sent


def test_group_coverage_sends_include_domains(session_factory, tmp_path, monkeypatch):
    """A configured ``ensure_groups`` tag the coverage does not reach gets one extra
    ``include_domains`` query for that group's outlets."""
    (tmp_path / "outlets.yaml").write_text(
        "outlets:\n- name: Local Wire\n  domains: [wire-local.example]\n"
        "  country: GB\n  kind: local\n  groups: [locals]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCOVER_OUTLETS_PATH", str(tmp_path / "outlets.yaml"))
    import app.outlets

    app.outlets._reset()
    try:
        story_id = seed2(session_factory)
        cfg = EnrichmentConfig(
            enabled=True,
            providers=[TF],
            depth=EnrichmentDepthConfig(
                target_sources=6,
                max_added_sources=6,
                max_queries_per_story=1,
                pages=1,
                locations=[],
                ensure_groups=["locals"],
            ),
        )
        fake = PagingSearch()
        result = asyncio.run(
            deep_enricher(session_factory, fake, cfg).enrich_story(story_id, force_deep=True)
        )
        group_searches = [s for s in result.searches if s.get("group") == "locals"]
        assert group_searches and any(
            c.hit.url == "https://wire-local.example/buses" for c in result.accepted
        )
        assert any(
            r.url.params.get("include_domains") == "wire-local.example" for r in fake.requests
        )
    finally:
        app.outlets._reset()


def test_headline_only_member_is_stored_without_text(session_factory, monkeypatch):
    """Title clearly the same event but the page cannot be fetched: stored with
    ``text_status = none`` and ``headline_only`` — coverage only, never summarized."""
    from app.enrich.fetch import FetchedPage

    monkeypatch.setattr("app.enrich.filters.similarity", lambda _a, _b: 0.5)
    monkeypatch.setattr("app.enrich.filters.shared_entities", lambda _a, _b: 3)
    monkeypatch.setattr("app.enrich.service.similarity", lambda _a, _b: 0.55)
    monkeypatch.setattr("app.enrich.service.shared_entities", lambda _a, _b: 3)
    story_id = seed(session_factory)
    enricher = deep_enricher(session_factory, PagingSearch())

    async def dead(url: str) -> FetchedPage:
        return FetchedPage(url, "failed", reason="", text="", words=0)

    monkeypatch.setattr(enricher.fetcher, "fetch", dead)
    result = asyncio.run(enricher.enrich_story(story_id, force_deep=True))
    kept = [c for c in result.candidates if c.decision == "headline_only"]
    assert kept and all(c.reason in ("fetch_failed", "blocked_url", "paywalled") for c in kept)
    with session_factory() as session:
        member = session.scalar(select(Article).where(Article.url == kept[0].hit.url))
        assert member.text_status == "none" and member.text is None
        assert member.raw_json["headline_only"] is True
        ctx, _ = enricher.context(session, story_id)
        assert all(t == "" for t in ctx.texts[1:])  # headline-only text is never input


def test_robots_disallowed_never_headline_only(session_factory, monkeypatch):
    from app.enrich.fetch import FetchedPage

    monkeypatch.setattr("app.enrich.filters.similarity", lambda _a, _b: 0.5)
    monkeypatch.setattr("app.enrich.filters.shared_entities", lambda _a, _b: 3)
    monkeypatch.setattr("app.enrich.service.similarity", lambda _a, _b: 0.55)
    monkeypatch.setattr("app.enrich.service.shared_entities", lambda _a, _b: 3)
    story_id = seed(session_factory)
    enricher = deep_enricher(session_factory, PagingSearch())

    async def dead(url: str) -> FetchedPage:
        return FetchedPage(url, "skipped", reason="robots", text="", words=0)

    monkeypatch.setattr(enricher.fetcher, "fetch", dead)
    result = asyncio.run(enricher.enrich_story(story_id, force_deep=True))
    assert not [c for c in result.candidates if c.decision == "headline_only"]
    assert all(c.reason == "robots" for c in result.candidates)


def test_stats_deep_distribution(session_factory):
    story_id = seed2(session_factory)
    enricher = deep_enricher(session_factory, PagingSearch())
    asyncio.run(enricher.enrich_story(story_id))
    with session_factory() as session:
        stats = enricher.stats(session)
    assert stats["deep"]["stories"] == 1
    assert sum(dict(stats["deep"]["distribution"]).values()) == 1
    assert stats["deep"]["outlets"] >= 5 and stats["deep"]["requests_today"] >= 1


def test_evaluate_deep_summary(capsys):
    r = StoryEnrichment(story_id=7, tier="deep", publishers_before=2, deep_requests=4)
    r.searches = [
        {"provider": "tinyfish", "query": "a b c", "page": 0, "hits": 1, "cached": False},
        {"provider": "tinyfish", "query": "a b c", "page": 1, "hits": 1, "cached": False},
        {"provider": "tinyfish", "query": "a b c", "location": "GB", "hits": 1, "cached": False},
    ]
    c = Candidate(
        hit=SearchHit(url="https://wire.example/x", title="t"), provider="tinyfish", query="a b c"
    )
    c.decision = "accepted"
    r.candidates = [c]
    text = summary([("Title", r)], markdown=False, deep=True)
    assert "Deep coverage" in text and "outlets per story" in text
    assert "GB" in text and "8+: 0" in text
