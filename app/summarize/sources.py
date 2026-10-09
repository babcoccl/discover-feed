"""Pick and number a story's sources for the model, and the cache key of that input."""

import hashlib
import json
from collections.abc import Sequence

from app.models import Article, TextStatus
from app.summarize.prompt import SourceDoc


def _words(text: str | None, limit: int) -> str:
    """The first ``limit`` words, keeping paragraph breaks."""
    paragraphs: list[str] = []
    for line in (text or "").splitlines():
        words = line.split()[: limit - sum(len(p.split()) for p in paragraphs)]
        if words:
            paragraphs.append(" ".join(words))
        if sum(len(p.split()) for p in paragraphs) >= limit:
            break
    return "\n".join(paragraphs)


def source_doc(index: int, article: Article, names: dict[str, str], max_words: int) -> SourceDoc:
    full = article.text_status == TextStatus.OK and bool(article.text)
    return SourceDoc(
        index=index,
        article_id=article.id,
        source=names.get(article.source_id, article.source_id),
        headline=" ".join((article.title or "").split()),
        url=article.url,
        text=_words(article.text if full else article.summary_raw, max_words),
        full_text=full,
    )


def _by_published(articles: Sequence[Article]) -> list[Article]:
    return sorted(articles, key=lambda m: (m.published_at, m.id))


def _priority(members: Sequence[Article]) -> list[Article]:
    """One per source first (earliest coverage first), then the rest."""
    ordered = _by_published(members)
    picked: list[Article] = []
    seen: set[str] = set()
    for article in ordered:
        if article.source_id not in seen:
            seen.add(article.source_id)
            picked.append(article)
    return picked + [a for a in ordered if a not in picked]


def select_sources(
    members: Sequence[Article],
    names: dict[str, str],
    *,
    max_articles: int,
    max_words: int,
    numbered_first: int = 0,
) -> list[SourceDoc]:
    """Up to ``max_articles`` members by priority, numbered [1]..[k] in publication order.

    With ``numbered_first`` (the brief's article budget), the first that many picks keep the
    numbers they have in the brief and extra picks are appended after them, so a report's
    [1]..[n] cite the same articles as the brief's."""
    picked = _priority(members)[:max_articles]
    split = min(numbered_first, len(picked)) if numbered_first else len(picked)
    chosen = _by_published(picked[:split]) + _by_published(picked[split:])
    return [source_doc(i, a, names, max_words) for i, a in enumerate(chosen, 1)]


def basis(sources: Sequence[SourceDoc]) -> str:
    full = sum(s.full_text for s in sources)
    if full == len(sources):
        return "full_text"
    return "feed_summary" if full == 0 else "mixed"


def input_hash(
    member_ids: Sequence[int],
    sources: Sequence[SourceDoc],
    prompt_version: str,
    model: str,
    kind: str = "brief",
) -> str:
    """Changes when the story gains a member, a source text changes, or the prompt/model or
    artifact kind does."""
    key = {
        "kind": kind,
        "members": sorted(member_ids),
        "sources": [[s.article_id, s.text_hash] for s in sources],
        "prompt_version": prompt_version,
        "model": model,
    }
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
