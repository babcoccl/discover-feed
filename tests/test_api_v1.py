"""The public /api/v1 read API: response models, pagination, published-only, Atom, the
OpenAPI snapshot, deprecated aliases and no extracted-text leakage."""

from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api_v1 import FeedV1, HealthV1, ProfileV1, StoryV1, TopicV1, VersionV1
from app.cluster.service import _merge
from app.demo import build_demo
from app.events import events_after
from app.main import create_app
from app.models import Article, Story
from app.openapi import SNAPSHOT, render

SLUG = "personal-reader"
ATOM = "{http://www.w3.org/2005/Atom}"


@pytest.fixture(scope="module")
def v1(tmp_path_factory):
    build = build_demo(tmp_path_factory.mktemp("v1") / "demo.db")
    with TestClient(create_app(build.settings, llm_transport=build.llm_transport)) as c:
        yield c


@pytest.fixture
def fresh(tmp_path):
    build = build_demo(tmp_path / "demo.db")
    with TestClient(create_app(build.settings, llm_transport=build.llm_transport)) as c:
        yield c


def _all(client, slug=SLUG) -> list[dict]:
    return client.get(f"/api/v1/profiles/{slug}/feed?limit=100").json()["items"]


def test_every_endpoint_matches_its_response_model(v1) -> None:
    profiles = [ProfileV1.model_validate(p) for p in v1.get("/api/v1/profiles").json()]
    assert {p.slug for p in profiles} >= {SLUG}
    ProfileV1.model_validate(v1.get(f"/api/v1/profiles/{SLUG}").json())
    [TopicV1.model_validate(t) for t in v1.get(f"/api/v1/profiles/{SLUG}/topics").json()]
    feed = FeedV1.model_validate(v1.get(f"/api/v1/profiles/{SLUG}/feed").json())
    assert feed.items and feed.include == "card"
    story = StoryV1.model_validate(v1.get(f"/api/v1/stories/{feed.items[0].id}").json())
    assert story.versions.story == story.version and story.sources
    assert all(s.url.startswith("http") for s in story.sources)
    HealthV1.model_validate(v1.get("/api/v1/health").json())
    assert VersionV1.model_validate(v1.get("/api/v1/version").json()).api_version == "1"
    assert v1.get("/api/v1/profiles/nope").status_code == 404
    assert v1.get(f"/api/v1/profiles/{SLUG}/feed?topic=9999").status_code == 404
    assert v1.get(f"/api/v1/profiles/{SLUG}/feed?cursor=%%%").status_code == 422


def test_card_and_full(v1) -> None:
    card = v1.get(f"/api/v1/profiles/{SLUG}/feed?limit=5").json()["items"]
    full = v1.get(f"/api/v1/profiles/{SLUG}/feed?limit=5&include=full").json()["items"]
    assert [c["id"] for c in card] == [f["id"] for f in full]
    assert all(c["report"] is None and c["sources"] is None for c in card)
    assert all(f["sources"] for f in full) and any(f["report"] for f in full)


def test_pagination_has_no_gaps_or_duplicates(v1) -> None:
    everything = [s["id"] for s in _all(v1)]
    assert len(everything) > 5
    seen, cursor = [], None
    while True:
        url = f"/api/v1/profiles/{SLUG}/feed?limit=3" + (f"&cursor={cursor}" if cursor else "")
        page = v1.get(url).json()
        seen += [s["id"] for s in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == everything and len(set(seen)) == len(seen)


def test_since_filters(v1) -> None:
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    past = "2000-01-01T00:00:00Z"
    for param in ("since", "updated_since"):
        assert v1.get(f"/api/v1/profiles/{SLUG}/feed", params={param: future}).json()["items"] == []
        items = v1.get(f"/api/v1/profiles/{SLUG}/feed", params={param: past, "limit": 100}).json()
        assert len(items["items"]) == len(_all(v1))


def test_unpublished_and_retracted_stories_are_excluded(fresh) -> None:
    factory = fresh.app.state.session_factory
    with factory() as session:
        hidden = session.scalar(
            select(Story.id).where(Story.published_version.is_(None), Story.merged_into.is_(None))
        )
        published = list(
            session.scalars(select(Story.id).where(Story.published_version.is_not(None)))
        )
    ids = {s["id"] for s in _all(fresh)}
    if hidden is not None:
        assert hidden not in ids
        assert fresh.get(f"/api/v1/stories/{hidden}").status_code == 404
    old, target = published[0], published[1]
    assert old in ids or old in {s["id"] for s in _all(fresh, "market-monitor")}
    with factory() as session:
        _merge(session, old, target)
        session.commit()
        events = events_after(session, 0)
    assert events[-1].type == "story.retracted" and events[-1].story_id == old
    assert old not in {s["id"] for s in _all(fresh)}
    moved = fresh.get(f"/api/v1/stories/{old}", follow_redirects=False)
    assert moved.status_code == 301
    assert moved.headers["location"] == f"/api/v1/stories/{target}"


def test_atom_feed_is_valid(v1) -> None:
    response = v1.get(f"/api/v1/profiles/{SLUG}/feed.atom?limit=10")
    assert response.headers["content-type"].startswith("application/atom+xml")
    root = ET.fromstring(response.content)
    assert root.tag == f"{ATOM}feed"
    for tag in ("id", "title", "updated"):
        assert root.find(f"{ATOM}{tag}") is not None
    entries = root.findall(f"{ATOM}entry")
    titles = [s["title"] for s in _all(v1)[:10]]
    assert [e.findtext(f"{ATOM}title") for e in entries] == titles
    for entry in entries:
        for tag in ("id", "title", "updated", "content"):
            assert entry.find(f"{ATOM}{tag}") is not None, tag
        datetime.fromisoformat(entry.findtext(f"{ATOM}updated").replace("Z", "+00:00"))
        rels = [link.get("rel") for link in entry.findall(f"{ATOM}link")]
        assert rels[0] == "alternate" and "related" in rels


def test_openapi_snapshot_is_current(v1) -> None:
    assert SNAPSHOT.read_text(encoding="utf-8") == render(v1.app), (
        "docs/openapi.v1.json is out of date: run `make openapi` (python -m app.openapi)"
    )
    schema = v1.get("/api/v1/openapi.json").json()
    assert all(p.startswith("/api/v1/") for p in schema["paths"])
    for path, ops in schema["paths"].items():
        if path.endswith(("/stream", ".atom")) or "get" not in ops:
            continue
        response = ops["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        ref = response.get("$ref") or response["items"]["$ref"]
        assert "examples" in schema["components"]["schemas"][ref.rsplit("/", 1)[-1]], path


def test_legacy_routes_are_deprecated_aliases(v1) -> None:
    old = v1.get(f"/api/profiles/{SLUG}/feed")
    assert old.status_code == 200 and old.headers["deprecation"] == "true"
    assert old.headers["link"] == f'</api/v1/profiles/{SLUG}/feed>; rel="successor-version"'
    assert "deprecation" not in v1.get(f"/api/v1/profiles/{SLUG}/feed").headers
    assert "deprecation" not in v1.post("/api/admin/extract").headers


def test_extracted_text_never_leaks(v1) -> None:
    with v1.app.state.session_factory() as session:
        texts = [
            a.text
            for a in session.scalars(select(Article).where(Article.text.is_not(None)))
            if a.text and len(a.text.split()) > 60
        ]
    assert texts
    probes = []
    for text in texts:
        words = text.split()
        probes.append(" ".join(words[len(words) // 2 : len(words) // 2 + 10]))
    bodies = [
        v1.get(f"/api/v1/profiles/{SLUG}/feed?limit=100&include=full").text,
        v1.get(f"/api/v1/profiles/{SLUG}/feed.atom?limit=100").text,
        v1.get("/api/v1/profiles").text,
    ]
    bodies += [v1.get(f"/api/v1/stories/{s['id']}").text for s in _all(v1)]
    for body in bodies:
        assert '"text_status"' not in body
        for probe in probes:
            assert probe not in body
