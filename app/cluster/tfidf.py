"""TF-IDF + cosine similarity clusterer (no model downloads, no embeddings)."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from scipy.sparse import csr_matrix
from sklearn.feature_extraction.text import TfidfVectorizer

from app.cluster.base import ArticleDoc, Assignment, Clusterer, StoryDoc, representative
from app.cluster.tokens import significant_tokens, words

TEXT_WORDS = 1000


def document(article: ArticleDoc) -> str:
    """Title (weighted x2) + feed summary + the first 1,000 words of the extracted text."""
    body = " ".join(article.text.split()[:TEXT_WORDS]) if article.text else ""
    return f"{article.title}\n{article.title}\n{article.summary}\n{body}"


@dataclass
class _Cluster:
    story_id: int | None
    new_story: int | None
    members: list[ArticleDoc]
    centroid: csr_matrix
    first: datetime
    last: datetime
    title_tokens: set[str] = field(default_factory=set)

    def refresh(self) -> None:
        self.first = min(m.published_at for m in self.members)
        self.last = max(m.published_at for m in self.members)
        self.title_tokens = significant_tokens(representative(self.members).title)

    def gap(self, at: datetime) -> timedelta:
        if at < self.first:
            return self.first - at
        return max(at - self.last, timedelta(0))


class TfidfClusterer(Clusterer):
    """Join an article to the most similar story if every guard passes, else start a story.

    - cosine(article, story centroid) >= ``threshold``
    - the story's time span is within ``window`` of the article's publish time
    - the article (title + summary) shares >= ``min_shared_tokens`` significant tokens with
      the story title
    - every member from the article's own source has similarity > ``same_source_threshold``
    """

    def __init__(
        self,
        *,
        threshold: float = 0.45,
        window_hours: float = 72,
        same_source_threshold: float = 0.7,
        min_shared_tokens: int = 2,
    ) -> None:
        self.threshold = threshold
        self.window = timedelta(hours=window_hours)
        self.same_source_threshold = same_source_threshold
        self.min_shared_tokens = min_shared_tokens

    def vectorize(self, docs: Sequence[ArticleDoc]) -> dict[int, csr_matrix]:
        if not docs:
            return {}
        vectorizer = TfidfVectorizer(analyzer=lambda text: words(text), sublinear_tf=True)
        matrix = vectorizer.fit_transform([document(d) for d in docs])  # rows are L2-normalized
        return {d.id: matrix[i] for i, d in enumerate(docs)}

    def assign(
        self, new_articles: Sequence[ArticleDoc], existing_stories: Sequence[StoryDoc]
    ) -> list[Assignment]:
        new = sorted(new_articles, key=lambda a: (a.published_at, a.id))
        existing = [s for s in existing_stories if s.members]
        vectors = self.vectorize([m for s in existing for m in s.members] + new)

        clusters = []
        for story in existing:
            centroid = sum((vectors[m.id] for m in story.members[1:]), vectors[story.members[0].id])
            cluster = _Cluster(story.id, None, list(story.members), centroid, *_span(story.members))
            cluster.refresh()
            clusters.append(cluster)

        assignments: list[Assignment] = []
        new_count = 0
        for article in new:
            vector = vectors[article.id]
            best, best_score = self._best(article, vector, clusters, vectors)
            if best is None:
                cluster = _Cluster(None, new_count, [article], vector, *_span([article]))
                cluster.refresh()
                clusters.append(cluster)
                assignments.append(Assignment(article.id, new_story=new_count))
                new_count += 1
                continue
            best.members.append(article)
            best.centroid = best.centroid + vector
            best.refresh()
            assignments.append(
                Assignment(
                    article.id, story_id=best.story_id, new_story=best.new_story, score=best_score
                )
            )
        return assignments

    def _best(self, article, vector, clusters, vectors) -> tuple["_Cluster | None", float]:
        tokens = significant_tokens(f"{article.title} {article.summary}")
        best, best_score = None, -1.0
        for cluster in clusters:
            if cluster.gap(article.published_at) > self.window:
                continue
            if len(tokens & cluster.title_tokens) < self.min_shared_tokens:
                continue
            score = cosine(vector, cluster.centroid)
            if score < self.threshold or score <= best_score:
                continue
            same_source = [m for m in cluster.members if m.source_id == article.source_id]
            if any(
                cosine(vector, vectors[m.id]) <= self.same_source_threshold for m in same_source
            ):
                continue
            best, best_score = cluster, score
        return best, best_score


def cosine(a: csr_matrix, b: csr_matrix) -> float:
    norm = float(np.sqrt(b.multiply(b).sum())) * float(np.sqrt(a.multiply(a).sum()))
    return float(a.multiply(b).sum()) / norm if norm else 0.0


def _span(members: Sequence[ArticleDoc]) -> tuple[datetime, datetime]:
    times = [m.published_at for m in members]
    return min(times), max(times)
