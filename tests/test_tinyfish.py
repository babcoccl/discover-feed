"""TinyFish as the primary enrichment provider (GDELT the fallback), query hardening for
real headlines, skipped stories and GDELT's "keyword too short" replies."""

import asyncio
import io

import httpx
import pytest
from sqlalchemy import func, select

from app.config import EnrichmentConfig, SearchProviderConfig
from app.enrich.query import drop_short_terms, provider_queries, signal_tokens
from app.models import Article, EnrichmentCandidate, SearchCacheEntry, SourceUsage, Story
from app.search import SearchService
from app.search.base import create_provider
from tests.test_enrich import (
    HITS,
    NOW,
    FakeSearch,
    clocked_enricher,
    config,
    no_sleep,
    seed,
    seed_more,
)

TF = SearchProviderConfig(name="tinyfish")


def tf_body(hits=HITS, *, snippet: bool = True) -> dict:
    results = []
    for n, (url, title) in enumerate(hits, start=1):
        item = {"position": n, "site_name": url.split("/")[2], "title": title, "url": url}
        if snippet:
            item["snippet"] = f"{title} - full coverage"
            item["date"] = NOW.isoformat()
        results.append(item)
    return {"query": "q", "results": results, "total_results": len(results), "page": 0}


class Router:
    """TinyFish answers from ``tinyfish`` (a list of responses, then ``default``); every
    other host is the GDELT fake."""

    def __init__(self, tinyfish=None, default=None, gdelt: FakeSearch | None = None):
        self.tinyfish = list(tinyfish or [])
        self.default = default
        self.gdelt = gdelt or FakeSearch()
        self.tf_requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "tinyfish" not in request.url.host:
            return self.gdelt(request)
        self.tf_requests.append(request)
        if self.tinyfish:
            return self.tinyfish.pop(0)
        return self.default or httpx.Response(200, json=tf_body())

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def tf_config(**kw) -> EnrichmentConfig:
    providers = [
        SearchProviderConfig(name="tinyfish"),
        SearchProviderConfig(name="gdelt", min_interval_seconds=0),
    ]
    return config(providers=providers, **kw)


def search_once(factory, router: Router, provider=TF, query="Springfield buses"):
    service = SearchService(factory, transport=router.transport(), sleep=no_sleep)
    return service, asyncio.run(service.search(provider, query, since=NOW, language="en"))


def run_now(enricher, story_id):
    return asyncio.run(enricher.enrich_story(story_id))


# --- provider -------------------------------------------------------------------------


def test_tinyfish_maps_results_and_sends_the_key(session_factory, monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-secret")
    router = Router()
    _, outcome = search_once(session_factory, router)
    assert outcome.ok and outcome.requests == 1 and outcome.kind is None
    hit = outcome.hits[1]
    assert hit.url == HITS[1][0] and hit.title == HITS[1][1] and hit.domain == "twitter.com"
    assert hit.snippet.endswith("full coverage") and hit.published_at is not None
    request = router.tf_requests[0]
    assert request.url.host == "api.search.tinyfish.ai"
    assert request.headers["X-API-Key"] == "tf-secret"
    assert request.url.params["query"] == "Springfield buses"
    assert request.url.params["domain_type"] == "news"
    assert request.url.params["after_date"] == NOW.date().isoformat()
    assert "tf-secret" not in outcome.raw
    with session_factory() as session:
        usage = session.scalars(
            select(SourceUsage).where(SourceUsage.source_id == "search:tinyfish")
        )
        assert sum(u.requests for u in usage) >= 1


def test_tinyfish_title_only_results(session_factory, monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    router = Router(default=httpx.Response(200, json=tf_body(snippet=False)))
    _, outcome = search_once(session_factory, router)
    assert outcome.hits and all(h.snippet == "" and h.title for h in outcome.hits)
    assert all(h.published_at is None for h in outcome.hits)


def test_tinyfish_keyless_sends_no_key_and_rests_on_401(session_factory, monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    keyless = SearchProviderConfig(name="tinyfish", base_url="http://tinyfish-proxy.local/search")
    router = Router()
    _, outcome = search_once(session_factory, router, keyless)
    assert outcome.ok and "X-API-Key" not in router.tf_requests[0].headers
    denied = httpx.Response(401, json={"error": {"code": "MISSING_API_KEY", "message": "x"}})
    service, outcome = search_once(session_factory, Router(default=denied), query="keyless")
    assert outcome.kind == "auth" and "TINYFISH_API_KEY" in outcome.error
    assert service.cooldown_remaining("tinyfish") > 0  # gdelt serves meanwhile


def test_tinyfish_429_cools_down_shared_state(session_factory, monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    limited = httpx.Response(429, headers={"Retry-After": "120"}, text="slow down")
    _, outcome = search_once(session_factory, Router(default=limited))
    assert outcome.kind == "rate_limited" and outcome.cooldown_seconds > 100
    other = SearchService(session_factory, transport=Router().transport(), sleep=no_sleep)
    assert other.cooldown_remaining("tinyfish") > 100  # the CLI sees the app's cooldown
    again = asyncio.run(other.search(TF, "other", since=NOW, language=None))
    assert again.skipped == "cooldown" and again.requests == 0


@pytest.mark.parametrize("body", ["", "   ", '{"results": [', "<html>oops</html>"])
def test_tinyfish_empty_or_malformed_is_transient(session_factory, monkeypatch, body):
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    bad = [httpx.Response(200, text=body), httpx.Response(200, text=body)]
    service, outcome = search_once(session_factory, Router(tinyfish=bad))
    assert outcome.kind == "transient" and outcome.requests == 2  # retried once
    assert service.cooldown_remaining("tinyfish") == 0
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SearchCacheEntry)) == 0
    recovered = Router(tinyfish=[httpx.Response(200, text=body)])
    _, outcome = search_once(session_factory, recovered, query="retry recovers")
    assert outcome.ok and outcome.hits


def test_tinyfish_interval_floor_and_defaults():
    assert TF.min_interval_seconds == 2.0
    assert SearchProviderConfig(name="tinyfish", min_interval_seconds=0.5).min_interval_seconds == 2
    fast = SearchProviderConfig(
        name="tinyfish", min_interval_seconds=0.5, override_min_interval=True
    )
    assert fast.min_interval_seconds == 0.5
    assert [p.name for p in EnrichmentConfig().providers] == ["tinyfish", "gdelt"]
    assert create_provider(TF, httpx.AsyncClient()).key_env == "TINYFISH_API_KEY"


# --- fallback -------------------------------------------------------------------------


def test_falls_back_to_gdelt_after_a_tinyfish_error(session_factory, monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    story_id = seed(session_factory)
    router = Router(default=httpx.Response(500, text="down"))
    enricher, _ = clocked_enricher(session_factory, router, tf_config())
    result = run_now(enricher, story_id)
    assert [s["provider"] for s in result.searches][-1] == "gdelt"
    assert result.searches[0]["kind"] == "transient" and router.gdelt.requests
    assert result.status == "done" and result.added >= 1
    assert {c.provider for c in result.accepted} == {"gdelt"}


def test_falls_back_to_gdelt_after_zero_tinyfish_hits(session_factory, monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    story_id = seed(session_factory)
    router = Router(default=httpx.Response(200, json=tf_body([])))
    enricher, _ = clocked_enricher(session_factory, router, tf_config())
    result = run_now(enricher, story_id)
    tf = [s for s in result.searches if s["provider"] == "tinyfish"]
    assert len(tf) == 2 and all(s["hits"] == 0 and not s["error"] for s in tf)  # strict, loose
    assert router.gdelt.requests and result.added >= 1


def test_tinyfish_hits_enrich_without_gdelt(session_factory, monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    story_id = seed(session_factory)
    router = Router()
    enricher, _ = clocked_enricher(
        session_factory, router, tf_config(max_added_sources=1, target_sources=2)
    )
    result = run_now(enricher, story_id)
    assert result.added == 1 and result.accepted[0].provider == "tinyfish"
    assert not router.gdelt.requests
    with session_factory() as session:
        web = session.scalars(select(Article).where(Article.origin == "enrichment")).all()
        assert len(web) == 1


def test_never_done_when_every_provider_failed(session_factory, monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    story_id = seed(session_factory)
    router = Router(
        default=httpx.Response(503, text="busy"), gdelt=FakeSearch(status=500, text="down")
    )
    enricher, _ = clocked_enricher(session_factory, router, tf_config())
    result = run_now(enricher, story_id)
    assert result.status == "retry" and result.reason.startswith("provider_unavailable")
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_status == "retry" and story.enriched_at is None


def test_dry_run_writes_no_status_candidates_or_cache(session_factory, monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    story_id = seed(session_factory)
    enricher, _ = clocked_enricher(session_factory, Router(), tf_config())
    result = asyncio.run(enricher.enrich_story(story_id, dry_run=True))
    assert result.accepted
    with session_factory() as session:
        story = session.get(Story, story_id)
        assert story.enrich_status is None and story.enriched_at is None
        assert session.scalar(select(func.count()).select_from(EnrichmentCandidate)) == 0
        assert session.scalar(select(func.count()).select_from(SearchCacheEntry)) == 0


# --- GDELT "keyword too short" --------------------------------------------------------

TOO_SHORT = "Your search contained a keyword that was too short."


def gdelt_only(**kw):
    return config(providers=[SearchProviderConfig(name="gdelt", min_interval_seconds=0)], **kw)


def test_gdelt_too_short_rebuilds_the_query_once(session_factory):
    story_id = seed_more(
        session_factory, ["Gen Z voters back Springfield electric buses"], hours_ago=0
    )
    seed(session_factory)
    story_id = story_id[0] if isinstance(story_id, list) else story_id
    fake = FakeSearch()
    fake.responses = [httpx.Response(200, text=TOO_SHORT)]
    enricher, _ = clocked_enricher(session_factory, fake, gdelt_only())
    result = run_now(enricher, story_id)
    first, second = (r.url.params["query"] for r in fake.requests[:2])
    assert '"Gen Z"' in first and "Gen Z" not in second
    assert result.searches[0]["kind"] == "query_rejected"
    assert enricher.search.cooldown_remaining("gdelt") == 0


def test_gdelt_still_too_short_skips_with_query_too_short(session_factory):
    seed(session_factory)
    story_id = seed_more(session_factory, ["Gen Z voters back Springfield electric buses"])[0]
    enricher, _ = clocked_enricher(session_factory, FakeSearch(text=TOO_SHORT), gdelt_only())
    result = run_now(enricher, story_id)
    assert result.status == "done" and result.reason == "query_too_short"
    assert all(s["kind"] == "query_rejected" for s in result.searches)
    assert result.searches[-1]["skipped"] == "query_too_short"
    assert enricher.search.cooldown_remaining("gdelt") == 0


def test_gdelt_rate_limit_text_still_cools_down(session_factory):
    fake = FakeSearch(text="Please limit requests to one every 5 seconds or contact us.")
    service = SearchService(session_factory, transport=fake.transport(), sleep=no_sleep)
    gdelt = SearchProviderConfig(name="gdelt")
    outcome = asyncio.run(service.search(gdelt, "q", since=NOW, language=None))
    assert outcome.kind == "rate_limited" and service.cooldown_remaining("gdelt") > 0


def test_gdelt_unknown_text_is_logged_in_full(session_factory, caplog):
    text = "Some new GDELT complaint " + "x" * 400
    fake = FakeSearch(text=text)
    service = SearchService(session_factory, transport=fake.transport(), sleep=no_sleep)
    outcome = asyncio.run(
        service.search(SearchProviderConfig(name="gdelt"), "q", since=NOW, language=None)
    )
    assert outcome.kind == "transient" and service.cooldown_remaining("gdelt") == 0
    assert text in caplog.text


def test_drop_short_terms():
    assert drop_short_terms("gdelt", '"Gen Z" wrestle shame') == "wrestle shame"
    assert drop_short_terms("gdelt", '"US Treasury" (IMF OR US OR debt)') == "(IMF OR debt)"
    assert drop_short_terms("gdelt", "wrestle shame") == ""  # nothing to drop
    assert drop_short_terms("gdelt", '"Gen Z" AI') == ""


# --- queries for real headlines -------------------------------------------------------

REAL = {
    "Ubuntu 26.10 (Stonking Stingray) released": ['"Ubuntu 26.10"', "Stingray"],
    "AI was great until it was not": [],
    "Show HN: Unfenced \u2013 Land for sale that borders public land": ["Unfenced"],
    "U.S. Treasury dismisses IMF debt warning and 'messy' forecasting": ["IMF", "Treasury"],
    "Gen Z wrestle with shame from ordering delivery food": ['"Gen Z"', "delivery"],
    "UK economy grew 0.1% in August ahead of Healey's first Budget": ["Healey", "Budget"],
    "Citizen Lab says governments are abusing online ads to hack people": ['"Citizen Lab"'],
    "Robert Reich says the Fed should cut interest rates": ['"Robert Reich"', "Fed"],
}


def _parts(query: str) -> list[str]:
    import re

    return re.findall(r'"[^"]*"|[^\s()]+', query)


@pytest.mark.parametrize("headline", list(REAL))
def test_real_headline_queries(headline):
    for provider in ("tinyfish", "gdelt"):
        queries = provider_queries(provider, [headline])
        assert queries, provider
        strict = queries[0]
        parts = _parts(strict)
        assert 1 <= len(parts) <= 5 and " OR " not in strict
        assert "Show HN" not in strict and "HN" not in parts
        for want in REAL[headline]:
            assert want in strict, (provider, strict)
        if provider == "gdelt":
            for q in queries:
                for part in _parts(q):
                    if part in ("OR",):
                        continue
                    if not part.startswith('"'):  # no standalone keyword under 3 chars
                        assert all(len(p) >= 3 for p in __import__("re").findall(r"\w+", part)), q
            assert "AI" not in parts and "UK" not in parts and "US" not in parts


def test_real_headlines_mostly_have_three_to_five_terms():
    sizes = [
        len(_parts(provider_queries("tinyfish", [h])[0]))
        for h in REAL
        if len(signal_tokens(h)) >= 2
    ]
    assert all(3 <= n <= 5 for n in sizes), sizes


def test_percentages_and_versions_survive_for_tinyfish():
    q = provider_queries("tinyfish", ["Inflation hits 3.2% as Ubuntu 26.10 ships"])[0]
    assert "3.2%" in q and '"Ubuntu 26.10"' in q


# --- skipped stories ------------------------------------------------------------------


def test_skip_patterns_and_no_signal_are_marked_done_and_counted(session_factory):
    seed(session_factory)
    ids = seed_more(
        session_factory,
        [
            "Show HN: Unfenced \u2013 Land for sale that borders public land",
            "The cost of getting a job today",
            "AI was great until it was not",
        ],
    )
    router = Router()
    enricher, _ = clocked_enricher(session_factory, router, tf_config())
    enricher.start = lambda: True
    enricher.schedule()
    with session_factory() as session:
        reasons = {sid: session.get(Story, sid).enrich_reason for sid in ids}
        statuses = {session.get(Story, sid).enrich_status for sid in ids}
        stats = enricher.stats(session)
    assert reasons[ids[0]] == "skipped:pattern"
    assert reasons[ids[1]] == reasons[ids[2]] == "skipped:no_signal"
    assert statuses == {"done"} and not router.tf_requests
    assert stats["skipped"] == {"no_signal": 2, "pattern": 1}


def test_skip_patterns_are_configurable(session_factory):
    seed(session_factory)
    sid = seed_more(session_factory, ["Letters: Springfield readers on Ortiz and buses"])[0]
    enricher, _ = clocked_enricher(session_factory, Router(), tf_config(skip_title_patterns=[]))
    assert enricher.skip_reason(["Letters: Springfield readers on Ortiz and buses"]) is None
    enricher, _ = clocked_enricher(session_factory, Router(), tf_config())
    result = run_now(enricher, sid)
    assert result.reason == "skipped:pattern" and not result.searches
    with pytest.raises(ValueError):
        EnrichmentConfig(skip_title_patterns=["(unclosed"])


def test_signal_tokens():
    assert len(signal_tokens("The cost of getting a job today")) == 0
    assert len(signal_tokens("AI was great until it was not")) == 1
    assert len(signal_tokens("UK economy grew 0.1% in August")) >= 2


# --- CLI tools ------------------------------------------------------------------------


def test_evaluate_groups_errors_and_separates_requests(session_factory, monkeypatch, capsys):
    from app.enrich import evaluate

    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    seed(session_factory)
    router = Router(default=httpx.Response(500, text="boom"))
    enricher, _ = clocked_enricher(session_factory, router, tf_config())
    monkeypatch.setattr(evaluate, "build", lambda: (session_factory, enricher))
    assert evaluate.main(["--stories", "5", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "tinyfish x1: TinyFish HTTP 500: boom" in out
    assert "tinyfish: 2 request(s) sent" in out and "top rejected" in out


def test_cli_skips_one_cooling_provider_and_waits_with_message(session_factory):
    from app.enrich.cli import respect_cooldowns, wait_for_cooldowns

    enricher, _ = clocked_enricher(session_factory, Router(), tf_config())
    enricher.search._cool_down("gdelt", 30)
    out = io.StringIO()
    assert respect_cooldowns(enricher, wait=False, out=out)  # tinyfish still serves
    assert "its searches are skipped" in out.getvalue()
    enricher.search._cool_down("tinyfish", 30)
    assert not respect_cooldowns(enricher, wait=False, out=out)
    slept = []
    waited = wait_for_cooldowns(enricher, out=out, sleep=slept.append)
    assert waited >= 60 and "waiting for gdelt (cooling down" in out.getvalue()


def test_capacity_uses_the_first_provider_not_cooling_down(session_factory):
    enricher, _ = clocked_enricher(session_factory, Router(), tf_config(wait_seconds=60))
    with session_factory() as session:
        normal = enricher.capacity(session)
        enricher.search._cool_down("tinyfish", 3600)
        assert normal == 15 and enricher.capacity(session) > 0  # gdelt (0 s) serves
