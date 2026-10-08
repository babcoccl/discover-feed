"""Keyword topic matching (no LLM): pure functions, evaluated at query time."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class TopicRule:
    include: Sequence[str] = ()
    exclude: Sequence[str] = ()
    source_ids: Sequence[str] = ()  # empty = any source


def normalize_keywords(values: Iterable[str]) -> list[str]:
    """Trim, collapse inner whitespace, drop blanks and case-insensitive duplicates."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        keyword = " ".join(value.split())
        if keyword and keyword.casefold() not in seen:
            seen.add(keyword.casefold())
            out.append(keyword)
    return out


def split_keywords(text: str) -> list[str]:
    """Parse a comma- or newline-separated keyword field."""
    return normalize_keywords(re.split(r"[,\n]", text or ""))


@lru_cache(maxsize=4096)
def keyword_pattern(keyword: str) -> re.Pattern[str] | None:
    words = keyword.split()
    if not words:
        return None
    # Lookarounds instead of \b so keywords that start/end with symbols ("S&P 500", "C++")
    # still only match as whole words.
    body = r"\s+".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def contains_keyword(text: str, keyword: str) -> bool:
    pattern = keyword_pattern(keyword)
    return bool(pattern and text and pattern.search(text))


def matches(rule: TopicRule, *, title: str, summary: str, source_id: str) -> bool:
    """Any include keyword (or no includes) in title/summary, and no exclude keyword."""
    if rule.source_ids and source_id not in rule.source_ids:
        return False
    text = f"{title or ''}\n{summary or ''}"
    if any(contains_keyword(text, kw) for kw in rule.exclude):
        return False
    return not rule.include or any(contains_keyword(text, kw) for kw in rule.include)
