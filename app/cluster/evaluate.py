"""Score clustering against the labelled demo fixtures (``tests/fixtures/demo/stories.yaml``).

    python -m app.cluster.evaluate                      # current settings
    python -m app.cluster.evaluate --threshold 0.35 0.45 0.55

Builds the offline demo DB once (ingest + extract), then reclusters it from scratch for each
parameter set and prints pairwise precision/recall, every labelled story with the similarity
score each member joined at, and the similarity of each near-miss pair.
"""

import argparse
import itertools
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.cluster.base import representative
from app.cluster.service import ClusterRunResult, doc, run_clustering
from app.cluster.tfidf import TfidfClusterer, cosine
from app.models import Article

ROOT = Path(__file__).resolve().parents[2]
LABELS = ROOT / "tests" / "fixtures" / "demo" / "stories.yaml"


@dataclass
class StoryRow:
    label: str
    source_id: str
    title: str
    story_id: int | None
    score: float | None
    """Similarity at which the article joined; None if it started its story."""


@dataclass
class NearMiss:
    a: str
    b: str
    similarity: float
    merged: bool


@dataclass
class Report:
    precision: float
    recall: float
    true_pairs: int
    predicted_pairs: int
    correct_pairs: int
    rows: list[StoryRow] = field(default_factory=list)
    near_misses: list[NearMiss] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def markdown(self) -> str:
        lines = [
            f"Pairwise precision **{self.precision:.0%}** ({self.correct_pairs}/"
            f"{self.predicted_pairs}), recall **{self.recall:.0%}** ({self.correct_pairs}/"
            f"{self.true_pairs})",
            "",
            "| Fixture story | Source | Headline | Story id | Joined at similarity |",
            "|---|---|---|---|---|",
        ]
        for r in self.rows:
            score = "started story" if r.score is None else f"{r.score:.3f}"
            lines.append(f"| {r.label} | {r.source_id} | {r.title} | {r.story_id} | {score} |")
        lines += ["", "| Near-miss pair | Cosine | Merged? |", "|---|---|---|"]
        for n in self.near_misses:
            lines.append(f"| {n.a} / {n.b} | {n.similarity:.3f} | {'yes' if n.merged else 'no'} |")
        if self.errors:
            lines += ["", "Errors:", *(f"- {e}" for e in self.errors)]
        return "\n".join(lines)


def load_labels(path: Path = LABELS) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def evaluate(
    session_factory: sessionmaker,
    clusterer: TfidfClusterer,
    result: ClusterRunResult,
    labels: dict | None = None,
) -> Report:
    labels = labels or load_labels()
    with session_factory() as session:
        articles = list(session.scalars(select(Article)))
    by_url = {a.url: a for a in articles}
    joined_at = {j.article_id: j.score for j in result.joins}

    gold: set[frozenset[int]] = set()
    rows = []
    for label, urls in labels["stories"].items():
        members = [by_url[u] for u in urls]
        gold |= {frozenset((x.id, y.id)) for x, y in itertools.combinations(members, 2)}
        for a in sorted(members, key=lambda m: (m.published_at, m.id)):
            rows.append(StoryRow(label, a.source_id, a.title, a.story_id, joined_at.get(a.id)))

    groups: dict[int, list[int]] = {}
    for a in articles:
        if a.story_id is not None:
            groups.setdefault(a.story_id, []).append(a.id)
    predicted = {
        frozenset(pair) for ids in groups.values() for pair in itertools.combinations(ids, 2)
    }
    correct = predicted & gold

    vectors = clusterer.vectorize([doc(a) for a in articles])
    names = {a.id: a for a in articles}
    near = []
    for pair in labels.get("near_misses", []):
        a, b = by_url[pair["a"]], by_url[pair["b"]]
        near.append(
            NearMiss(
                a.title,
                b.title,
                cosine(vectors[a.id], vectors[b.id]),
                a.story_id is not None and a.story_id == b.story_id,
            )
        )

    errors = []
    for pair in sorted(predicted - gold, key=sorted):
        x, y = (names[i] for i in sorted(pair))
        errors.append(f"false merge: {x.title!r} + {y.title!r}")
    for pair in sorted(gold - predicted, key=sorted):
        x, y = (names[i] for i in sorted(pair))
        errors.append(f"missed: {x.title!r} + {y.title!r}")

    return Report(
        precision=len(correct) / len(predicted) if predicted else 1.0,
        recall=len(correct) / len(gold) if gold else 1.0,
        true_pairs=len(gold),
        predicted_pairs=len(predicted),
        correct_pairs=len(correct),
        rows=rows,
        near_misses=near,
        errors=errors,
    )


def story_titles(session_factory: sessionmaker) -> dict[int, str]:
    with session_factory() as session:
        members: dict[int, list] = {}
        for a in session.scalars(select(Article).where(Article.story_id.is_not(None))):
            members.setdefault(a.story_id, []).append(doc(a))
    return {sid: representative(m).title for sid, m in members.items()}


def main() -> None:
    from app.db import make_engine, make_session_factory
    from app.demo import build_demo
    from app.pipeline import clusterer_from_settings
    from app.settings import get_settings

    defaults = get_settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--threshold", type=float, nargs="+", default=[defaults.cluster_threshold])
    parser.add_argument("--window-hours", type=float, default=defaults.cluster_window_hours)
    parser.add_argument(
        "--same-source-threshold", type=float, default=defaults.cluster_same_source_threshold
    )
    parser.add_argument("--min-shared-tokens", type=int, default=defaults.cluster_min_shared_tokens)
    parser.add_argument("--markdown", action="store_true", help="print full tables")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        build = build_demo(Path(tmp) / "eval.db")
        engine = make_engine(build.settings.database_url)
        session_factory = make_session_factory(engine)
        try:
            for threshold in args.threshold:
                clusterer = clusterer_from_settings(
                    defaults.model_copy(
                        update={
                            "cluster_threshold": threshold,
                            "cluster_window_hours": args.window_hours,
                            "cluster_same_source_threshold": args.same_source_threshold,
                            "cluster_min_shared_tokens": args.min_shared_tokens,
                        }
                    )
                )
                result = run_clustering(session_factory, clusterer, rebuild=True)
                report = evaluate(session_factory, clusterer, result)
                print(
                    f"threshold={threshold:.2f}  precision={report.precision:.0%}  "
                    f"recall={report.recall:.0%}  stories={result.stories}  "
                    f"multi-source={result.multi_source_stories}"
                )
                if args.markdown:
                    print(report.markdown(), end="\n\n")
                else:
                    for error in report.errors:
                        print(f"    {error}")
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
