"""Tokenization shared by the TF-IDF features and the shared-token guard."""

import re

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

STOPWORDS = frozenset(ENGLISH_STOP_WORDS) | {
    *("said", "says", "say", "new", "just", "like", "year", "years", "week", "today"),
    *("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
    *("according", "including", "told", "reported", "report", "reports", "it's"),
}
_WORD = re.compile(r"[^\W_]+(?:['\u2019.&][^\W_]+)*", re.UNICODE)


def normalize(token: str) -> str:
    """Lowercase, drop possessives and a plural "s" so "rates"/"rate" and "NASA's"/"NASA" meet."""
    token = token.lower().replace("\u2019", "'")
    token = token.removesuffix("'s").replace(".", "")
    if len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        token = token[:-1]
    return token


def words(text: str) -> list[str]:
    """Normalized, non-stopword tokens (single characters dropped)."""
    out = []
    for raw in _WORD.findall(text or ""):
        token = normalize(raw)
        if len(token) > 1 and token not in STOPWORDS and raw.lower() not in STOPWORDS:
            out.append(token)
    return out


def significant_tokens(text: str) -> set[str]:
    """Non-stopwords that are 4+ characters or capitalized (entities like "Fed", "SEC")."""
    out = set()
    for raw in _WORD.findall(text or ""):
        token = normalize(raw)
        if token in STOPWORDS or raw.lower() in STOPWORDS or len(token) < 2:
            continue
        if len(token) >= 4 or raw[0].isupper():
            out.add(token)
    return out
