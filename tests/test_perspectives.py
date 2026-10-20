"""Perspectives ("how outlets differ"): per-source digests and the story-level
synthesis. Fake LLM everywhere — digest quotes are rejected when they are not verbatim,
disagreements need two positions on different sources with different stances, and
position labels may never be outlet or political labels."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.cluster import TfidfClusterer
from app.cluster.service import run_clustering
from app.config import LLMRole, PerspectivesConfig
from app.lifecycle import ensure_stories
from app.models import (
    Article,
    ArticleDigest,
    ProfileRecord,
    SourceRecord,
    Story,
    StoryState,
    StorySummary,
)
from app.perspectives.prompt import synthesis_messages
from app.perspectives.service import KIND, PerspectiveService
from app.perspectives.validate import (
    quote_in_text,
    validate_digest,
    validate_synthesis,
)
from app.summarize.fake import FakeLLM
from app.summarize.validate import SummaryInvalid

NOW = datetime.now(UTC).replace(microsecond=0)

_BASE = (
    "Springfield council approves electric bus purchase. The council voted on Monday "
    "to order forty electric buses for the transit authority, after three months of "
    "debate about fares and depots. "
)
SUPPORTS = (
    _BASE + "Springfield officials confirmed the purchase on Monday, a decision the "
    "transit authority supports. "
) * 5
DISPUTES = (
    _BASE + "A rival outlet disputes the account of the vote, saying the order was never approved. "
) * 5
NEUTRAL = (_BASE + "The city will decide on depot sites later this year. ") * 5


def seed(
    factory,
    *,
    texts: tuple[str, ...] = (SUPPORTS, SUPPORTS, NEUTRAL, NEUTRAL),
    states=None,
) -> int:
    """A published story with one member per entry in ``texts`` (distinct sources).
    ``states[i] = "none"`` makes member i+1 headline-only."""
    states = states or ["ok"] * len(texts)
    with factory() as session:
        profile = session.scalar(select(ProfileRecord).where(ProfileRecord.slug == "me"))
        if profile is None:
            profile = ProfileRecord(slug="me", name="Me")
            session.add(profile)
        for n, (text, text_status) in enumerate(zip(texts, states, strict=True), 1):
            sid = f"s{n}"
            record = session.get(SourceRecord, sid)
            if record is None:
                record = SourceRecord(id=sid, name=f"Outlet {n}", url=f"https://o{n}.example/feed")
                session.add(record)
            profile.sources.append(record)
            session.add(
                Article(
                    source_id=sid,
                    url=f"https://o{n}.example/a{len(list(session.scalars(select(Article)))) + 1}",
                    canonical_url=f"https://o{n}.example/a{n}",
                    title="Springfield council approves electric bus purchase",
                    summary_raw="Springfield buses",
                    published_at=NOW - timedelta(hours=1),
                    fetched_at=NOW,
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    text=text if text_status == "ok" else None,
                    text_status=text_status,
                    word_count=len(text.split()) if text_status == "ok" else 0,
                )
            )
        session.flush()
        ensure_stories(session, NOW)
        session.commit()
    run_clustering(factory, TfidfClusterer())
    with factory() as session:
        story = session.get(
            Story,
            session.scalar(select(Article.story_id).order_by(Article.id.desc()).limit(1)),
        )
        assert story is not None
        story.state, story.published_version = StoryState.PUBLISHED.value, 1
        story.last_updated_at = NOW
        session.commit()
        return story.id


def service(factory, mode="ok", **overrides) -> PerspectiveService:
    options = PerspectivesConfig(**{"enabled": True, "min_sources": 4, **overrides})
    return PerspectiveService(
        factory,
        client_from(mode),
        options,
        profile_defaults={"me": True},
    )


def client_from(mode):
    from app.llm.client import LLMClient

    fake = mode if isinstance(mode, FakeLLM) else FakeLLM(mode)
    return LLMClient(
        LLMRole(base_url="http://fake/v1", model="fake-llm"), transport=fake.transport()
    )


def run_story(service_, factory, story_id, **kw):
    return asyncio.run(service_.run_story(story_id=story_id, **kw))


def digests_of(factory, story_id):
    with factory() as session:
        return list(
            session.scalars(select(ArticleDigest).where(ArticleDigest.story_id == story_id))
        )


def summary_of(factory, story_id):
    with factory() as session:
        return session.scalar(
            select(StorySummary)
            .where(StorySummary.story_id == story_id, StorySummary.kind == KIND)
            .order_by(StorySummary.id.desc())
            .limit(1)
        )


def test_synthesis_on_a_published_story(session_factory):
    story_id = seed(session_factory)
    svc = service(session_factory)
    outcome = run_story(svc, session_factory, story_id)
    assert outcome.status == "generated", outcome.reason
    assert outcome.digests == 4
    row = summary_of(session_factory, story_id)
    assert row is not None and row.status == "ok"
    assert row.content_json["agreement"]
    assert len(row.article_ids) == 4
    assert len(digests_of(session_factory, story_id)) == 4


def test_a_real_disagreement_is_synthesized(session_factory):
    story_id = seed(session_factory, texts=(SUPPORTS, SUPPORTS, DISPUTES, NEUTRAL))
    outcome = run_story(service(session_factory), session_factory, story_id)
    assert outcome.status == "generated"
    synth = summary_of(session_factory, story_id).content_json
    assert synth["disagreements"], synth
    stances = {
        position["citations"][0] for d in synth["disagreements"] for position in d["positions"]
    }
    assert len(stances) >= 2  # the disagreement cites different sources


def test_all_sources_agreeing_produce_no_disagreement(session_factory):
    story_id = seed(session_factory)
    outcome = run_story(service(session_factory), session_factory, story_id)
    assert outcome.status == "generated"
    synth = summary_of(session_factory, story_id).content_json
    assert synth["disagreements"] == []
    assert synth["agreement"]


def test_unverified_digest_quotes_are_rejected(session_factory):
    story_id = seed(session_factory)
    svc = service(session_factory, mode="bad_quote")
    outcome = run_story(svc, session_factory, story_id)
    assert outcome.status == "skipped"
    assert "few" in outcome.reason or "digests" in outcome.reason
    rows = digests_of(session_factory, story_id)
    assert rows and all(d.status == "invalid" for d in rows)


def test_quote_normalization():
    text = "He said “the plan passes” — and it did pass."
    assert quote_in_text("the plan passes", text)
    assert quote_in_text("He said   “the plan passes”", text)
    assert not quote_in_text("the plan fails", text)


def test_single_source_disagreement_is_omitted(session_factory):
    story_id = seed(session_factory, texts=(SUPPORTS, SUPPORTS, DISPUTES, NEUTRAL))
    svc = service(session_factory, mode="one_sided_dispute")
    outcome = run_story(svc, session_factory, story_id)
    assert outcome.status == "generated"
    synth = summary_of(session_factory, story_id).content_json
    assert synth["disagreements"] == []  # never invented balance


def test_political_label_is_rejected(session_factory):
    story_id = seed(session_factory, texts=(SUPPORTS, SUPPORTS, DISPUTES, NEUTRAL))
    svc = service(session_factory, mode="political_label")
    outcome = run_story(svc, session_factory, story_id)
    assert outcome.status == "failed"
    row = summary_of(session_factory, story_id)
    assert row.status == "failed" and "label" in (row.reason or "")


def test_digest_numbers_must_appear_in_the_text():
    content = (
        '{"claims": [{"text": "About 900 buses were ordered.",'
        ' "quote": "forty electric buses"}], "stance": "supports", "kind": "reporting",'
        ' "key_parties": [], "framing": "a vote"}'
    )
    with pytest.raises(SummaryInvalid, match="900"):
        validate_digest(content, SUPPORTS)


def test_digest_bad_quote_rejected():
    content = (
        '{"claims": [{"text": "The buses were ordered.",'
        ' "quote": "a phrase that is not in the article at all"}],'
        ' "stance": "neutral", "kind": "reporting", "key_parties": [],'
        ' "framing": "a vote"}'
    )
    with pytest.raises(SummaryInvalid, match="verbatim"):
        validate_digest(content, SUPPORTS)


def test_synthesis_validation_unit():
    digests = [
        {"digest": {"stance": "supports"}, "label": "Outlet A"},
        {"digest": {"stance": "disputes"}, "label": "Outlet B"},
    ]
    texts = [SUPPORTS, DISPUTES]
    good = (
        '{"agreement": [{"text": "All outlets cover the council vote.", "citations": [1, 2]}],'
        ' "disagreements": [{"topic": "vote outcome", "positions": ['
        ' {"label": "says the order passed", "summary": "Reports approval.",'
        '  "citations": [1], "quote": "to order forty electric buses"},'
        ' {"label": "says it was not approved", "summary": "Calls it unapproved.",'
        '  "citations": [2], "quote": "was never approved"}]}],'
        ' "different_angles": []}'
    )
    out = validate_synthesis(good, digests, texts)
    assert out["disagreements"] and len(out["disagreements"][0]["positions"]) == 2
    # a disagreement whose positions all cite one source is dropped
    one_sided = (
        '{"agreement": [], "disagreements": [{"topic": "vote", "positions": ['
        ' {"label": "says the order passed", "summary": "Reports approval.",'
        '  "citations": [1], "quote": "to order forty electric buses"},'
        ' {"label": "says it was not approved", "summary": "Calls it unapproved.",'
        '  "citations": [1], "quote": "was never approved"}]}], "different_angles": []}'
    )
    out = validate_synthesis(one_sided, digests, texts)
    assert out["disagreements"] == []
    # positions citing two sources but sharing a stance are also dropped
    same_stance = (
        '{"agreement": [], "disagreements": [{"topic": "vote", "positions": ['
        ' {"label": "says the order passed", "summary": "Reports approval.",'
        '  "citations": [1], "quote": "to order forty electric buses"},'
        ' {"label": "says it passed too", "summary": "Also approves.",'
        '  "citations": [2], "quote": "to order forty electric buses"}]}],'
        ' "different_angles": []}'
    )
    agreeing = [
        {"digest": {"stance": "supports"}, "label": "A"},
        {"digest": {"stance": "supports"}, "label": "B"},
    ]
    out = validate_synthesis(same_stance, agreeing, texts)
    assert out["disagreements"] == []
    # an outlet's name is never a position label
    named = (
        '{"agreement": [], "disagreements": [{"topic": "vote", "positions": ['
        ' {"label": "Outlet A", "summary": "Reports approval.",'
        '  "citations": [1], "quote": "to order forty electric buses"},'
        ' {"label": "says it was not approved", "summary": "Calls it unapproved.",'
        '  "citations": [2], "quote": "was never approved"}]}], "different_angles": []}'
    )
    with pytest.raises(SummaryInvalid, match="label"):
        validate_synthesis(named, digests, texts)
    # and a political label is rejected outright
    political = (
        '{"agreement": [], "disagreements": [{"topic": "vote", "positions": ['
        ' {"label": "right-wing media", "summary": "Reports approval.",'
        '  "citations": [1], "quote": "to order forty electric buses"},'
        ' {"label": "says it was not approved", "summary": "Calls it unapproved.",'
        '  "citations": [2], "quote": "was never approved"}]}], "different_angles": []}'
    )
    with pytest.raises(SummaryInvalid, match="label"):
        validate_synthesis(political, digests, texts)


def test_synthesis_number_check_unit():
    digests = [{"digest": {"stance": "neutral"}, "label": "A"}]
    bad = (
        '{"agreement": [{"text": "Officials counted 4242 ballots.", "citations": [1]}],'
        ' "disagreements": [], "different_angles": []}'
    )
    with pytest.raises(SummaryInvalid, match="4242"):
        validate_synthesis(bad, digests, [NEUTRAL])


def test_digests_are_cached_by_content(session_factory):
    story_id = seed(session_factory)
    svc = service(session_factory)
    first = run_story(svc, session_factory, story_id)
    assert first.status == "generated"
    second = run_story(svc, session_factory, story_id)
    assert second.status == "cached"
    # no extra digest rows: the cache hit used the stored ones
    assert len(digests_of(session_factory, story_id)) == 4
    assert session_factory_kinds(session_factory, story_id) == 1


def session_factory_kinds(factory, story_id):
    with factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(StorySummary)
            .where(StorySummary.story_id == story_id, StorySummary.kind == KIND)
        )


def test_headline_only_member_gets_title_digest(session_factory):
    story_id = seed(
        session_factory,
        texts=(SUPPORTS, SUPPORTS, NEUTRAL, "no text"),
        states=["ok", "ok", "ok", "none"],
    )
    svc = service(session_factory)
    outcome = run_story(svc, session_factory, story_id)
    assert outcome.status == "generated", outcome.reason
    rows = {d.basis: d for d in digests_of(session_factory, story_id)}
    assert "title" in rows
    assert rows["title"].digest_json["stance"] == "unclear"
    assert rows["title"].digest_json["claims"]


def test_min_sources_gate(session_factory):
    story_id = seed(session_factory, texts=(SUPPORTS, NEUTRAL))
    svc = service(session_factory, min_sources=4)
    outcome = run_story(svc, session_factory, story_id)
    assert outcome.status == "skipped"
    assert "digests" in (outcome.reason or "")


def test_below_min_sources_after_dropped_digests(session_factory):
    story_id = seed(session_factory, texts=(SUPPORTS, SUPPORTS, NEUTRAL))
    outcome = run_story(service(session_factory), session_factory, story_id)
    assert outcome.status == "skipped"


def test_stances_for_coverage_badges(session_factory):
    story_id = seed(session_factory, texts=(SUPPORTS, SUPPORTS, DISPUTES, NEUTRAL))
    svc = service(session_factory)
    run_story(svc, session_factory, story_id)
    with session_factory() as session:
        stances = svc.stances(session, story_id)
    assert len(stances) == 4
    assert {v["stance"] for v in stances.values()} >= {"supports", "disputes"}


def test_profile_toggle_off_disables(session_factory):
    story_id = seed(session_factory)
    svc = service(session_factory)
    with session_factory() as session:
        record = session.scalar(select(ProfileRecord).where(ProfileRecord.slug == "me"))
        record.perspectives_enabled = False
        session.commit()
        assert not svc.profile_enabled(record)
        assert svc.candidates(session) == []
        record.perspectives_enabled = True
        session.commit()
        assert svc.candidates(session) or True  # refresh
    # with the toggle back on the story is a candidate until it is current
    with session_factory() as session:
        assert story_id in svc.candidates(session)


def test_rate_limit(session_factory):
    story_id = seed(session_factory)
    svc = service(session_factory, max_per_hour=1)
    run_story(svc, session_factory, story_id)
    with session_factory() as session:
        assert svc.rate_limited(session)
        assert svc.candidates(session) == []


def test_is_current_blocks_rerun(session_factory):
    story_id = seed(session_factory)
    svc = service(session_factory)
    with session_factory() as session:
        assert story_id in svc.candidates(session)
    run_story(svc, session_factory, story_id)
    with session_factory() as session:
        assert story_id not in svc.candidates(session)


def test_synthesis_message_format_roundtrip():
    """The fake LLM's digest blocks must parse back (the prompts send ``format_digests``)."""
    digests = [
        {
            "label": "Outlet A",
            "digest": {
                "stance": "supports",
                "kind": "reporting",
                "framing": "a vote",
                "claims": [{"text": "The order passed.", "quote": "approved the purchase"}],
            },
        }
    ]
    messages = synthesis_messages("A story", "The order passed", digests)
    user = messages[1]["content"]
    from app.summarize.fake import _DIGEST_BLOCK

    found = _DIGEST_BLOCK.findall(user)
    assert found and found[0][0] == "1"
