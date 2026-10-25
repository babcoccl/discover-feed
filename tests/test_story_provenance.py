"""Enrichment provenance on the story page: which brief/report points cite web-search
members — rendered from stored view documents, never re-generated."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Story, StoryView
from app.story_views import pre_enrichment_brief, published_story

NOW = datetime.now(UTC).replace(microsecond=0)


def _member(i, source_id, source, origin, publisher):
    return {
        "id": i,
        "source_id": source_id,
        "source": source,
        "title": f"{source} buses story",
        "url": f"https://{publisher.lower().replace(' ', '')}.example/{i}",
        "published_at": (NOW - timedelta(hours=4 - i)).isoformat(),
        "image_url": None,
        "summary": f"{source} coverage of the bus plan",
        "text_status": "ok",
        "words": 120,
        "origin": origin,
        "publisher": publisher,
    }


# 2 feed + 2 enrichment members; brief/report mix web, mixed and feed points.
MEMBERS = [
    _member(1, "a", "News A", "feed", "NewsA"),
    _member(2, "b", "News B", "feed", "NewsB"),
    _member(3, "web:wire.example", "Wire Daily", "enrichment", "WireDaily"),
    _member(4, "web:search.example", "Search News", "enrichment", "SearchNews"),
]
CITATIONS = [
    {
        "index": i,
        "article_id": m["id"],
        "source": m["source"],
        "headline": m["title"],
        "url": m["url"],
        "publisher": m["publisher"],
    }
    for i, m in enumerate(MEMBERS, 1)
]
BRIEF = {
    "style": "brief",
    "lead": [{"text": "The council approved forty buses.", "citations": [1, 3]}],  # mixed
    "bullets": [
        {"text": "Only the web sources reported the depot detail.", "citations": [3]},  # web
        {"text": "Your sources covered the vote count.", "citations": [2]},  # feed
        {"text": "Both kinds confirm the timeline.", "citations": [4, 1]},  # mixed
    ],
    "citations": CITATIONS,
    "basis": "full_text",
    "model": "fake-llm",
    "generated_at": NOW.isoformat(),
    "status": "ok",
}
REPORT = {
    "state": "ready",
    "reason": None,
    "paragraphs": [
        {"text": "Web sources frame it as a win for Ortiz.", "citations": [3, 4]},  # web
        {"text": "News B noted the 6-1 vote.", "citations": [2]},  # feed
        {"text": "No citations in this paragraph.", "citations": []},  # feed (uncited)
    ],
    "citations": CITATIONS,
    "model": "fake-llm",
    "generated_at": NOW.isoformat(),
}


def _doc(story_id, *, members=MEMBERS, brief=BRIEF, report=REPORT):
    return {
        "schema_version": 1,
        "story_id": story_id,
        "title": "Springfield approves forty electric buses",
        "representative_id": 1,
        "image": {"url": None, "source": "none"},
        "first_seen_at": (NOW - timedelta(hours=3)).isoformat(),
        "last_updated_at": NOW.isoformat(),
        "article_count": len(members),
        "source_count": len({m["source_id"] for m in members}),
        "members": members,
        "brief": brief,
        "report": report,
        "model": "fake-llm",
        "generated_at": NOW.isoformat(),
    }


def _seed(session, *, members=MEMBERS, brief=BRIEF, report=REPORT, version=1, earlier=()):
    story = Story(
        title="Springfield approves forty electric buses",
        first_seen_at=NOW - timedelta(hours=3),
        last_updated_at=NOW,
        article_count=len(members),
        source_count=len({m["source_id"] for m in members}),
        state="published",
        published_version=version,
        published_at=NOW - timedelta(hours=1),
        published_sort_at=NOW,
    )
    session.add(story)
    session.flush()
    for v, doc in earlier:
        session.add(
            StoryView(
                story_id=story.id,
                version=v,
                state="complete",
                doc=doc,
                published_at=NOW - timedelta(hours=2),
            )
        )
    session.add(
        StoryView(
            story_id=story.id,
            version=version,
            state="complete",
            doc=_doc(story.id, members=members, brief=brief, report=report),
            published_at=NOW - timedelta(hours=1),
        )
    )
    session.commit()
    return story.id


@pytest.fixture
def story_id(session_factory):
    with session_factory() as session:
        return _seed(session)


@pytest.fixture
def client(settings, session_factory, story_id):
    with TestClient(create_app(settings)) as c:
        c.app.state.session_factory = session_factory
        yield c


def test_citation_origin_filled_from_members(session_factory, story_id):
    with session_factory() as session:
        story = published_story(session, story_id)
    by_index = {c.index: c for c in story.brief.citations}
    assert [by_index[i].origin for i in (1, 2, 3, 4)] == [
        "feed",
        "feed",
        "enrichment",
        "enrichment",
    ]


def test_missing_member_citation_defaults_to_feed(session_factory):
    doc_members = MEMBERS[:3]  # web member 4 dropped; a point still cites it
    with session_factory() as session:
        sid = _seed(session, members=doc_members)
        story = published_story(session, sid)
    by_index = {c.index: c for c in story.brief.citations}
    assert by_index[4].origin == "feed"  # no member to resolve from


def test_point_provenance_classes(session_factory, story_id):
    with session_factory() as session:
        story = published_story(session, story_id)
    points = story.brief.lead + story.brief.bullets + story.report.paragraphs
    assert [p.provenance for p in points] == [
        "mixed",
        "web",
        "feed",
        "mixed",
        "web",
        "feed",
        "feed",
    ]
    lead = story.brief.lead[0]
    assert [c.index for c in lead.web_citations] == [3]
    assert [c.index for c in lead.feed_citations] == [1]
    assert story.brief.bullets[1].provenance == "feed"  # feed citations only
    assert story.report.paragraphs[2].provenance == "feed"  # uncited


def test_old_view_documents_without_origin_load(session_factory):
    """Views written before this change have no ``origin`` key anywhere: members
    default to feed and every point is feed-provenance."""
    members = [{k: v for k, v in m.items() if k != "origin"} for m in MEMBERS]
    citations = [{k: v for k, v in c.items() if k != "publisher"} for c in CITATIONS]
    brief = {**BRIEF, "citations": citations}
    with session_factory() as session:
        sid = _seed(session, members=members, brief=brief)
        story = published_story(session, sid)
    assert story is not None
    assert all(c.origin == "feed" for c in story.brief.citations)
    assert all(p.provenance == "feed" for p in story.brief.sentences)


def test_enrichment_summary_counts_and_cited_in(session_factory, story_id):
    with session_factory() as session:
        story = published_story(session, story_id)
    e = story.enrichment
    assert e is not None
    assert e.web_member_count == 2
    assert e.web_publishers == ["WireDaily", "SearchNews"]
    assert e.brief_points == {"web": 1, "mixed": 2, "feed": 1, "total": 4}
    assert e.report_points == {"web": 1, "mixed": 0, "feed": 2, "total": 3}
    assert e.cited_in == {
        3: ["lead", "bullet 1", "report ¶1"],
        4: ["bullet 3", "report ¶1"],
    }


def test_no_enrichment_members_no_summary(session_factory):
    members = MEMBERS[:2]
    with session_factory() as session:
        sid = _seed(session, members=members)
        story = published_story(session, sid)
    assert story.enrichment is None


def test_story_page_provenance_markup(client, story_id):
    html = client.get(f"/story/{story_id}").text
    assert 'data-provenance="web"' in html
    assert 'data-provenance="mixed"' in html
    assert 'data-provenance="feed"' in html
    # the "web" tag and aria labels are non-color indicators
    assert "data-web-tag" in html
    assert 'aria-label="Added from web search"' in html
    assert 'aria-label="Confirmed by web search"' in html
    assert "not a word-level diff" in html
    # enrichment chips carry data-origin and the globe glyph; feed chips don't
    assert 'data-origin="enrichment"' in html
    assert "data-web-dot" in html
    web_chips = html.count('data-origin="enrichment"')
    feed_chips = html.count('data-origin="feed"')
    assert web_chips > 0 and feed_chips > 0
    # report paragraphs get the same treatment
    assert "data-report-paragraph" in html


def test_story_page_enrichment_panel(client, story_id):
    html = client.get(f"/story/{story_id}").text
    assert "data-enrichment-panel" in html
    assert "Enriched with 2 sources found via web search" in html
    assert "WireDaily" in html and "SearchNews" in html
    assert "data-enrichment-breakdown" in html
    assert "Brief: 1 only from web sources, 2 confirmed by both, 1 from your sources." in html
    assert "Report: 1 only from web sources, 0 confirmed by both, 2 from your sources." in html
    assert "data-enrichment-legend" in html
    # the toggle: default on, localStorage-persisted, CSS-only
    assert 'id="web-highlight-toggle"' in html
    assert 'data-web-highlight="on"' in html
    assert "localStorage" in html
    assert "data-enrichment-publishers" in html


def test_story_page_sources_list_cited_in(client, story_id):
    html = client.get(f"/story/{story_id}").text
    assert "data-cited-in" in html
    assert "cited in: lead, bullet 1, report ¶1" in html
    assert "cited in: bullet 3, report ¶1" in html


def test_story_page_without_enrichment_renders_as_before(client, session_factory):
    with session_factory() as session:
        sid = _seed(session, members=MEMBERS[:2])
    html = client.get(f"/story/{sid}").text
    assert "data-enrichment-panel" not in html
    assert "data-provenance" not in html
    assert "data-web-highlight" not in html
    assert "data-origin" not in html
    assert "data-web-tag" not in html and "data-web-dot" not in html
    assert "web-highlight-toggle" not in html
    assert "data-cited-in" not in html


def test_pre_enrichment_brief_picks_earlier_feed_only_view(session_factory):
    """story_views is append-only: the latest earlier version whose brief cites no
    enrichment member is the collapsed 'brief before enrichment' comparison."""
    earlier = [
        (1, _doc(1, members=MEMBERS[:2], brief=BRIEF, report=None)),
        (2, _doc(1, members=MEMBERS, brief=BRIEF, report=None)),  # cites enrichment: skip
    ]
    with session_factory() as session:
        sid = _seed(session, version=3, earlier=earlier)
        story = published_story(session, sid)
        prior = pre_enrichment_brief(session, story)
    assert prior is not None and prior.version == 1
    assert prior.brief is not None
    # v2's brief cites a web member, so the comparison must fall back to v1
    with session_factory() as session:
        story2 = published_story(session, sid)
        assert pre_enrichment_brief(session, story2).version == 1


def test_pre_enrichment_brief_none_without_earlier_view(session_factory, story_id):
    with session_factory() as session:
        story = published_story(session, story_id)
        assert pre_enrichment_brief(session, story) is None


def test_pre_enrichment_brief_none_without_enrichment(session_factory):
    with session_factory() as session:
        sid = _seed(session, members=MEMBERS[:2])
        story = published_story(session, sid)
        assert pre_enrichment_brief(session, story) is None


def test_story_page_prior_brief_details(session_factory, client, story_id):
    with session_factory() as session:
        session.add(
            StoryView(
                story_id=story_id,
                version=0,
                state="complete",
                doc=_doc(story_id, members=MEMBERS[:2], brief=BRIEF, report=None),
                published_at=NOW - timedelta(hours=5),
            )
        )
        # story.published_version is 1, so v0 counts as an earlier version
        session.commit()
    html = client.get(f"/story/{story_id}").text
    assert "data-prior-brief" in html
    assert "Brief before enrichment (v0" in html
    # rendered read-only, without provenance styling
    assert html.count("data-provenance") == html.count('data-provenance="')  # sanity
    prior_section = html.split("data-prior-brief", 1)[1]
    assert 'data-provenance="web"' not in prior_section.split("</details>", 1)[0]


def test_api_v1_story_additive_fields(client, story_id):
    data = client.get(f"/api/v1/stories/{story_id}").json()
    origins = {c["index"]: c["origin"] for c in data["brief"]["sources"]}
    assert origins == {1: "feed", 2: "feed", 3: "enrichment", 4: "enrichment"}
    bullets = data["brief"]["bullets"]
    assert [b["provenance"] for b in bullets] == ["web", "feed", "mixed"]
    paragraphs = data["report"]["paragraphs"]
    assert [p["provenance"] for p in paragraphs] == ["web", "feed", "feed"]
    assert [c["origin"] for c in data["brief"]["lead_citations"]] == [
        "feed",
        "enrichment",
    ]


def test_openapi_has_provenance_fields(client):
    spec = client.get("/openapi.json").json()
    cited = spec["components"]["schemas"]["CitedTextOut"]["properties"]
    assert "provenance" in cited
    citation = spec["components"]["schemas"]["CitationOut"]["properties"]
    assert "origin" in citation
