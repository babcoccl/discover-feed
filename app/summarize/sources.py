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


def select_sources(
    members: Sequence[Article], names: dict[str, str], *, max_articles: int, max_words: int
) -> list[SourceDoc]:
    """Up to ``max_articles`` members, one per source first (earliest coverage first), then
    the rest; numbered [1]..[k] in publication order."""
    ordered = sorted(members, key=lambda m: (m.published_at, m.id))
    picked: list[Article] = []
    seen: set[str] = set()
    for article in ordered:
        if article.source_id not in seen:
            seen.add(article.source_id)
            picked.append(article)
    picked += [a for a in ordered if a not in picked]
    chosen = sorted(picked[:max_articles], key=lambda m: (m.published_at, m.id))
    return [source_doc(i, a, names, max_words) for i, a in enumerate(chosen, 1)]


def basis(sources: Sequence[SourceDoc]) -> str:
    full = sum(s.full_text for s in sources)
    if full == len(sources):
        return "full_text"
    return "feed_summary" if full == 0 else "mixed"


def input_hash(
    member_ids: Sequence[int], sources: Sequence[SourceDoc], prompt_version: str, model: str
) -> str:
    """Changes when the story gains a member, a source text changes, or the prompt/model does."""
    key = {
        "members": sorted(member_ids),
        "sources": [[s.article_id, s.text_hash] for s in sources],
        "prompt_version": prompt_version,
        "model": model,
    }
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
