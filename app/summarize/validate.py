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


# --- briefs and reports -------------------------------------------------------------------

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "he",
        "her",
        "his",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "to",
        "was",
        "were",
        "which",
        "while",
        "who",
        "will",
        "with",
        "would",
        "said",
        "says",
        "after",
        "also",
        "over",
        "than",
        "then",
        "there",
        "these",
        "those",
        "not",
        "no",
        "so",
        "up",
    ]
)
_TOKEN = re.compile(r"[a-z0-9]+(?:['.,][a-z0-9]+)*")
_MARKER = re.compile(r"\s*\[\d+(?:\s*[,\u2013-]\s*\d+)*\]")


def tokens(text: str) -> list[str]:
    """Lowercase word tokens, punctuation dropped (``U.S.`` -> ``u.s``)."""
    return _TOKEN.findall(text.lower().replace("\u2019", "'"))


def overlap(a: str, b: str) -> float:
    """Jaccard overlap of the content words (stopwords dropped) of two texts."""
    ta = {t for t in tokens(a) if t not in _STOPWORDS}
    tb = {t for t in tokens(b) if t not in _STOPWORDS}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def copied_run(text: str, sources: Sequence[str], run: int) -> str | None:
    """The first run of ``run`` consecutive words of ``text`` found verbatim in a source."""
    grams: set[tuple[str, ...]] = set()
    for source in sources:
        words = tokens(source)
        grams.update(tuple(words[i : i + run]) for i in range(len(words) - run + 1))
    words = tokens(text)
    for i in range(len(words) - run + 1):
        if tuple(words[i : i + run]) in grams:
            return " ".join(words[i : i + run])
    return None


@dataclass(frozen=True)
class CitedText:
    text: str
    citations: list[int]

    def as_json(self) -> dict:
        return {"text": self.text, "citations": self.citations}


@dataclass(frozen=True)
class ValidBrief:
    lead: str
    lead_citations: list[int]
    bullets: list[CitedText]

    @property
    def text(self) -> str:
        return "\n".join([self.lead, *(b.text for b in self.bullets)])

    def as_json(self) -> dict:
        return {
            "lead": self.lead,
            "lead_citations": self.lead_citations,
            "bullets": [b.as_json() for b in self.bullets],
        }


@dataclass(frozen=True)
class ValidReport:
    paragraphs: list[CitedText]

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.paragraphs)

    @property
    def word_count(self) -> int:
        return sum(len(p.text.split()) for p in self.paragraphs)

    def as_json(self) -> dict:
        return {"paragraphs": [p.as_json() for p in self.paragraphs]}


def _clean(text: str) -> str:
    return " ".join(_MARKER.sub("", text).split())


def _cited(value: object, what: str, k: int) -> list[int]:
    if not isinstance(value, list) or not all(
        isinstance(i, int) and not isinstance(i, bool) for i in value
    ):
        raise SummaryInvalid(f"{what} citations must be a list of source indexes")
    if not value:
        raise SummaryInvalid(f"{what} has no citation")
    bad = [i for i in value if not 1 <= i <= k]
    if bad:
        raise SummaryInvalid(f"{what} cites {bad}; valid source indexes are 1..{k}")
    return list(dict.fromkeys(value))


def _point(value: object, what: str, k: int) -> CitedText:
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise SummaryInvalid(f'{what} must be an object {{"text": str, "citations": [int]}}')
    text = _clean(value["text"])
    if not text:
        raise SummaryInvalid(f"{what} is empty")
    return CitedText(text, _cited(value.get("citations"), what, k))


def _check_numbers(point: CitedText, what: str, sources: Sequence[str]) -> None:
    available = set().union(*(numbers_in(sources[i - 1]) for i in point.citations))
    missing = sorted(numbers_in(point.text) - available)
    if missing:
        raise SummaryInvalid(
            f"{what} contains number(s) {missing} not found in its cited sources {point.citations}"
        )


def _check_duplicates(points: Sequence[tuple[str, CitedText]], threshold: float) -> None:
    for i, (name_a, a) in enumerate(points):
        for name_b, b in points[i + 1 :]:
            if overlap(a.text, b.text) >= threshold:
                raise SummaryInvalid(f"{name_b} repeats {name_a}; make every point distinct")


def validate_brief(
    content: str, sources: Sequence[str], *, duplicate_threshold: float = 0.6
) -> ValidBrief:
    """Check a brief ``{"lead", "lead_citations", "bullets": [{"text", "citations"}] x3}``.

    Rules: the lead is one sentence of at most 30 words; exactly 3 bullets of at most 28 words;
    every point cites at least one source in 1..k; numbers appear in the cited sources; no
    bullet overlaps the lead or another bullet by ``duplicate_threshold`` or more."""
    from app.summarize.prompt import BULLET_COUNT, BULLET_MAX_WORDS, LEAD_MAX_WORDS

    data = parse_json(content)
    k = len(sources)
    if not isinstance(data.get("lead"), str):
        raise SummaryInvalid('"lead" must be a non-empty string')
    lead = _point({"text": data["lead"], "citations": data.get("lead_citations")}, "lead", k)
    bullets = data.get("bullets")
    if not isinstance(bullets, list):
        raise SummaryInvalid('"bullets" must be a list')
    if len(bullets) != BULLET_COUNT:
        raise SummaryInvalid(f"the brief needs exactly {BULLET_COUNT} bullets; got {len(bullets)}")
    points = [("the lead", lead)] + [
        (f"bullet {n}", _point(b, f"bullet {n}", k)) for n, b in enumerate(bullets, 1)
    ]
    sentences = len(split_sentences(lead.text))
    if sentences != 1:
        raise SummaryInvalid(f"the lead must be exactly one sentence; it has {sentences}")
    for name, point in points:
        limit = LEAD_MAX_WORDS if point is lead else BULLET_MAX_WORDS
        words = len(point.text.split())
        if words > limit:
            raise SummaryInvalid(f"{name} has {words} words; the limit is {limit}")
        _check_numbers(point, name, sources)
    _check_duplicates(points, duplicate_threshold)
    return ValidBrief(lead.text, lead.citations, [p for _, p in points[1:]])


def validate_report(
    content: str,
    sources: Sequence[str],
    *,
    duplicate_threshold: float = 0.6,
    word_range: tuple[int, int] | None = None,
) -> ValidReport:
    """Check a report ``{"paragraphs": [{"text", "citations"}]}``.

    Rules: 3-5 paragraphs; total words in ``word_range``; every paragraph cites at least one
    source in 1..k; numbers appear in the cited sources; no two paragraphs overlap by
    ``duplicate_threshold`` or more; no run of 8+ words copied verbatim from any source."""
    from app.summarize.prompt import (
        REPORT_MAX_PARAGRAPHS,
        REPORT_MIN_PARAGRAPHS,
        REPORT_WORD_RANGE,
        VERBATIM_RUN,
    )

    low, high = word_range or REPORT_WORD_RANGE
    data = parse_json(content)
    paragraphs = data.get("paragraphs")
    if not isinstance(paragraphs, list):
        raise SummaryInvalid('"paragraphs" must be a list')
    if not REPORT_MIN_PARAGRAPHS <= len(paragraphs) <= REPORT_MAX_PARAGRAPHS:
        raise SummaryInvalid(
            f"the report needs {REPORT_MIN_PARAGRAPHS}-{REPORT_MAX_PARAGRAPHS} paragraphs; "
            f"got {len(paragraphs)}"
        )
    k = len(sources)
    points = [
        (f"paragraph {n}", _point(p, f"paragraph {n}", k)) for n, p in enumerate(paragraphs, 1)
    ]
    report = ValidReport([p for _, p in points])
    if not low <= report.word_count <= high:
        raise SummaryInvalid(
            f"the report has {report.word_count} words; write {low}-{high} words in total"
        )
    for name, point in points:
        _check_numbers(point, name, sources)
    _check_duplicates(points, duplicate_threshold)
    for name, point in points:
        copied = copied_run(point.text, sources, VERBATIM_RUN)
        if copied:
            raise SummaryInvalid(
                f'{name} copies "{copied}" word for word from a source; paraphrase instead'
            )
    return report
