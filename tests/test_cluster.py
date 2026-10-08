from pathlib import Path

import pytest
from sqlalchemy import select

from app.cluster import ArticleDoc, StoryDoc, TfidfClusterer, representative
from app.cluster.evaluate import evaluate, load_labels
from app.cluster.service import run_clustering
from app.cluster.tokens import significant_tokens
from app.db import make_engine, make_session_factory
from app.demo import build_demo
from app.models import Article, Story
from tests.pipeline_helpers import T0, add_article

APPLE = "Apple unveils M5 MacBook Pro laptops with faster graphics and longer battery life"
APPLE_2 = "Apple's new MacBook Pro gets the M5 chip, faster graphics and longer battery life"
APPLE_TEXT = (
    "Apple announced new MacBook Pro laptops with the M5 chip, which brings faster graphics, "
    "a brighter display and longer battery life. Prices start at $1,599 and the laptops ship "
    "next week. "
) * 3


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    build = build_demo(tmp_path_factory.mktemp("demo") / "demo.db")
    engine = make_engine(build.settings.database_url)
    yield build, make_session_factory(engine)
    engine.dispose()


def groups(session_factory) -> dict[int, set[str]]:
    with session_factory() as session:
        out: dict[int, set[str]] = {}
        for a in session.scalars(select(Article).where(Article.story_id.is_not(None))):
            out.setdefault(a.story_id, set()).add(a.url)
        return out


def test_fixture_stories_cluster_exactly(demo) -> None:
    build, session_factory = demo
    labels = load_labels()
    by_url = {}
    for story_id, urls in groups(session_factory).items():
        for url in urls:
            by_url[url] = story_id
    found = {frozenset(urls) for urls in groups(session_factory).values() if len(urls) > 1}
    stories = labels["stories"]
    fomc = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261007a.htm"
    expected = {
        frozenset(stories["apple-m5-macbook-pro"]),
        frozenset(stories["aws-us-east-1-outage"]),
        frozenset(stories["sec-nimbus-crypto-fraud"]),
        # Known miss at the default threshold: the Fed's formal FOMC statement shares too
        # little wording with the press coverage (cosine ~0.33), so it stays on its own.
        frozenset(set(stories["fed-rate-cut"]) - {fomc}),
    }
    assert found == expected
    for pair in labels["near_misses"]:
        assert by_url[pair["a"]] != by_url[pair["b"]], pair["note"]
    assert build.clustered.multi_source_stories == 4


def test_fixture_precision_recall(demo) -> None:
    build, session_factory = demo
    clusterer = TfidfClusterer()
    report = evaluate(session_factory, clusterer, build.clustered)
    assert report.precision == 1.0
    assert report.correct_pairs == report.true_pairs - 3  # FOMC statement x 3 members
    assert all(not n.merged for n in report.near_misses)
    assert all(r.score is None or r.score >= 0.45 for r in report.rows)


def test_clustering_is_idempotent_and_rebuild_reproduces(demo) -> None:
    _, session_factory = demo
    before = groups(session_factory)
    again = run_clustering(session_factory, TfidfClusterer())
    assert again.processed == 0 and again.new_stories == 0
    assert groups(session_factory) == before
    rebuilt = run_clustering(session_factory, TfidfClusterer(), rebuild=True)
    assert rebuilt.processed == sum(len(g) for g in before.values())
    assert sorted(map(sorted, groups(session_factory).values())) == sorted(
        map(sorted, before.values())
    )


def test_story_fields_follow_members(demo) -> None:
    _, session_factory = demo
    with session_factory() as session:
        for story in session.scalars(select(Story)):
            members = list(session.scalars(select(Article).where(Article.story_id == story.id)))
            assert story.article_count == len(members)
            assert story.source_count == len({m.source_id for m in members})
            assert story.first_seen_at == min(m.published_at for m in members)
            assert story.last_updated_at == max(m.published_at for m in members)
            rep = next(m for m in members if m.id == story.representative_article_id)
            assert story.title == rep.title


def test_incremental_join_to_existing_story(session_factory) -> None:
    add_article(
        session_factory, url="https://a.example/1", title=APPLE, source_id="a", text=APPLE_TEXT
    )
    run_clustering(session_factory, TfidfClusterer())
    add_article(
        session_factory,
        url="https://b.example/1",
        title=APPLE_2,
        source_id="b",
        hours=2,
        text=APPLE_TEXT,
    )
    result = run_clustering(session_factory, TfidfClusterer())
    assert result.processed == 1 and result.joined_existing == 1
    with session_factory() as session:
        story = session.scalar(select(Story))
        assert (story.article_count, story.source_count) == (2, 2)


@pytest.mark.parametrize(("hours", "joined"), [(10, True), (71, True), (80, False)])
def test_time_window(session_factory, hours, joined) -> None:
    add_article(
        session_factory, url="https://a.example/1", title=APPLE, source_id="a", text=APPLE_TEXT
    )
    add_article(
        session_factory,
        url="https://b.example/1",
        title=APPLE_2,
        source_id="b",
        hours=hours,
        text=APPLE_TEXT,
    )
    result = run_clustering(session_factory, TfidfClusterer(window_hours=72))
    assert (result.stories == 1) is joined


def _docs(source_b: str) -> list[ArticleDoc]:
    a = ArticleDoc(1, "a", "https://a.example/1", APPLE, "Apple's laptops get the M5 chip.", T0)
    b = ArticleDoc(
        2,
        source_b,
        "https://x.example/2",
        APPLE_2,
        "The MacBook Pro refresh brings M5 graphics and battery gains, with prices unchanged.",
        T0,
    )
    return [a, b]


def test_same_source_guard_needs_higher_similarity() -> None:
    clusterer = TfidfClusterer(threshold=0.3, same_source_threshold=0.7)
    vectors = clusterer.vectorize(_docs("a"))
    from app.cluster.tfidf import cosine

    similarity = cosine(vectors[1], vectors[2])
    assert 0.3 < similarity < 0.7
    same = clusterer.assign(_docs("a"), [])
    assert [x.new_story for x in same] == [0, 1]  # same source, different URL: kept apart
    other = clusterer.assign(_docs("b"), [])
    assert other[1].new_story == 0 and other[1].score == pytest.approx(similarity)


def test_shared_token_guard() -> None:
    a = ArticleDoc(1, "a", "u1", "Storm floods coastal towns", "heavy rain wind damage", T0)
    b = ArticleDoc(2, "b", "u2", "Heavy rain and wind", "storm damage heavy rain wind", T0)
    assert len(significant_tokens(f"{b.title} {b.summary}") & significant_tokens(a.title)) == 1
    assert TfidfClusterer(threshold=0.1, min_shared_tokens=2).assign([a, b], [])[1].new_story == 1
    assert TfidfClusterer(threshold=0.1, min_shared_tokens=1).assign([a, b], [])[1].new_story == 0


def test_assign_against_existing_story_docs() -> None:
    existing = [StoryDoc(7, [ArticleDoc(1, "a", "u1", APPLE, APPLE_TEXT, T0)])]
    new = [ArticleDoc(2, "b", "u2", APPLE_2, APPLE_TEXT, T0)]
    (assignment,) = TfidfClusterer().assign(new, existing)
    assert assignment.story_id == 7 and assignment.score > 0.45


def test_representative_is_earliest_from_longest_source() -> None:
    from datetime import timedelta

    short = ArticleDoc(1, "a", "u1", "t", "short summary", T0)
    long_late = ArticleDoc(2, "b", "u2", "t", "", T0 + timedelta(hours=2), text="w " * 500)
    long_early = ArticleDoc(3, "b", "u3", "t", "x", T0 + timedelta(hours=1), text="w " * 10)
    assert representative([short, long_late, long_early]).id == 3


def test_cluster_cli_runs_on_configured_db(tmp_path: Path, monkeypatch, capsys) -> None:
    from app.cluster.__main__ import main
    from app.settings import get_settings

    monkeypatch.setenv("DISCOVER_DATABASE_URL", f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    monkeypatch.setattr("sys.argv", ["app.cluster", "--rebuild"])
    get_settings.cache_clear()
    try:
        main()
    finally:
        get_settings.cache_clear()
    assert '"rebuild": true' in capsys.readouterr().out
