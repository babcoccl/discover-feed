"""Validate a model's summary answer. Pure functions: no I/O, no model calls."""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MAX_WORDS = 70


class SummaryInvalid(ValueError):
    """The answer breaks a rule; the message is sent back to the model on retry."""


@dataclass(frozen=True)
class ValidSummary:
    summary: str
    citations: list[list[int]]
    sentences: list[str]


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def parse_json(content: str) -> dict:
    """The JSON object in ``content``; tolerates code fences or prose around it."""
    text = (content or "").strip()
    if not text:
        raise SummaryInvalid("empty response")
    candidates = [text]
    candidates += [m.strip() for m in _FENCE.findall(text)]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise SummaryInvalid("response is not a valid JSON object")


_ABBREVIATIONS = {
    "u.s.", "u.k.", "e.u.", "u.n.", "mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "inc.",
    "corp.", "co.", "ltd.", "jr.", "sr.", "vs.", "no.", "e.g.", "i.e.", "a.m.", "p.m.", "gov.",
    "sen.", "rep.", "jan.", "feb.", "mar.", "apr.", "jun.", "jul.", "aug.", "sep.", "sept.",
    "oct.", "nov.", "dec.",
}  # fmt: skip
_CLOSERS = "\"'\u201d\u2019)]"


def split_sentences(text: str) -> list[str]:
    """Split on . ! ? followed by a capitalized word, a digit or a quote; keeps abbreviations
    (U.S., Inc., Dr.) and initials (J.) inside their sentence."""
    words = text.split()
    sentences: list[str] = []
    current: list[str] = []
    for i, word in enumerate(words):
        current.append(word)
        bare = word.rstrip(_CLOSERS)
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if (
            nxt
            and bare[-1:] in ".!?"
            and bare.lower() not in _ABBREVIATIONS
            and not re.fullmatch(r"[A-Z]\.", bare)
            and (nxt[0].isupper() or nxt[0].isdigit() or nxt[0] in "\"'\u201c\u2018(")
        ):
            sentences.append(" ".join(current))
            current = []
    if current:
        sentences.append(" ".join(current))
    return sentences


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _normalize_number(raw: str) -> str:
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):
        raw = raw.replace(",", "")
    raw = raw.replace(",", ".") if raw.count(",") == 1 and "." not in raw else raw
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return raw
    text = format(value.normalize(), "f")
    return text


def numbers_in(text: str) -> set[str]:
    """Numeric values in ``text`` (digits, percentages, amounts), normalized so that
    ``1,200`` == ``1200`` and ``4.50`` == ``4.5``; units and currency symbols are ignored."""
    return {_normalize_number(m) for m in _NUMBER.findall(text)}


def validate_summary(
    content: str, sources: Sequence[str], *, max_words: int = MAX_WORDS
) -> ValidSummary:
    """Check a raw answer against the numbered sources (``sources[0]`` is [1]).

    Rules: a JSON object ``{"summary": str, "citations": [[int, ...], ...]}``; one non-empty
    citation list per sentence; every index in 1..k; at most ``max_words`` words; every number
    in a sentence appears in at least one source that sentence cites.
    """
    data = parse_json(content)
    summary = data.get("summary")
    citations = data.get("citations")
    if not isinstance(summary, str) or not summary.strip():
        raise SummaryInvalid('"summary" must be a non-empty string')
    if not isinstance(citations, list) or not all(
        isinstance(c, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in c)
        for c in citations
    ):
        raise SummaryInvalid('"citations" must be a list of lists of source indexes')
    summary = " ".join(summary.split())
    words = len(summary.split())
    if words > max_words:
        raise SummaryInvalid(f"summary has {words} words; the limit is {max_words}")
    sentences = split_sentences(summary)
    if len(citations) != len(sentences):
        raise SummaryInvalid(
            f"summary has {len(sentences)} sentence(s) but citations has {len(citations)} "
            "list(s); give exactly one list per sentence"
        )
    k = len(sources)
    for n, cited in enumerate(citations, 1):
        if not cited:
            raise SummaryInvalid(f"sentence {n} has no citation")
        bad = [i for i in cited if not 1 <= i <= k]
        if bad:
            raise SummaryInvalid(f"sentence {n} cites {bad}; valid source indexes are 1..{k}")
    for n, (sentence, cited) in enumerate(zip(sentences, citations, strict=True), 1):
        available = set().union(*(numbers_in(sources[i - 1]) for i in cited))
        missing = sorted(numbers_in(sentence) - available)
        if missing:
            raise SummaryInvalid(
                f"sentence {n} contains number(s) {missing} not found in its cited sources {cited}"
            )
    return ValidSummary(summary=summary, citations=citations, sentences=sentences)
