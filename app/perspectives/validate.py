"""Validate the per-source digest and the story-level synthesis. Pure functions.

Digest rules: ≤3 claims, each with an exact quote of ≤15 words that appears
verbatim in the article text (whitespace and quotes normalized); stance/kind
enums; framing ≤12 words; every number in a claim/framing appears in the text.

Synthesis rules: agreement/angle items have text ≤24 words and valid citations; a
disagreement is kept only when ≥2 positions each cite a different digest with a
verified quote and the cited digests' stances differ — otherwise the item is
dropped silently ("never invent balance"). Position labels describe the view,
never an outlet name or a political leaning. Numbers must appear in the cited
sources' own text; generated prose goes through the shared verbatim guard.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.perspectives.prompt import (
    AGREEMENT_WORDS,
    ANGLE_WORDS,
    CLAIM_WORDS,
    FRAMING_WORDS,
    KINDS,
    LABEL_WORDS,
    POSITION_WORDS,
    QUOTE_WORDS,
    STANCES,
    TOPIC_WORDS,
)
from app.summarize.validate import (
    SummaryInvalid,
    VerbatimGuard,
    check_verbatim,
    numbers_in,
    parse_json,
)

_QUOTE_CHARS = str.maketrans(
    {
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u00ab": '"',
        "\u00bb": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
    }
)


def normalize(text: str) -> str:
    """Lowercase, unify curly/straight quotes and collapse whitespace — the basis
    for the verbatim quote check."""
    return " ".join((text or "").translate(_QUOTE_CHARS).split()).lower()


def quote_in_text(quote: str, text: str) -> bool:
    """``quote`` appears word-for-word in ``text`` (normalized)."""
    q = normalize(quote)
    return bool(q) and q in normalize(text)


def _words(text: str) -> int:
    return len(str(text).split())


def title_digest(title: str) -> dict:
    """The digest a headline-only member gets — built from the title alone, no LLM:
    stance ``unclear``, framing the headline's first words."""
    headline = " ".join((title or "").split())
    return {
        "claims": [
            {
                "text": headline[:200],
                "quote": " ".join(headline.split()[:QUOTE_WORDS]),
            }
        ]
        if headline
        else [],
        "stance": "unclear",
        "kind": "reporting",
        "key_parties": [],
        "framing": " ".join(headline.split()[:FRAMING_WORDS]),
    }


def validate_digest(content: str, text: str) -> dict:
    """Check a raw answer against the article ``text``; raises :class:`SummaryInvalid`.

    ``text`` is the article's extracted text (or its feed summary) — the same words the
    model saw."""
    data = parse_json(content)
    claims = data.get("claims")
    if not isinstance(claims, list) or len(claims) > 3:
        raise SummaryInvalid('"claims" must be a list of at most 3 items')
    source = text or ""
    out_claims = []
    for n, claim in enumerate(claims, 1):
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str):
            raise SummaryInvalid(f'claim {n} must be {{"text": str, "quote": str}}')
        body, quote = " ".join(claim["text"].split()), str(claim.get("quote") or "")
        if not body:
            raise SummaryInvalid(f"claim {n} has an empty text")
        if _words(body) > CLAIM_WORDS:
            raise SummaryInvalid(f"claim {n} is longer than {CLAIM_WORDS} words")
        if _words(quote) > QUOTE_WORDS:
            raise SummaryInvalid(f"claim {n} quote is longer than {QUOTE_WORDS} words")
        if not quote_in_text(quote, source):
            raise SummaryInvalid(
                f'claim {n} quote "{quote[:80]}" does not appear verbatim in the article text'
            )
        missing = sorted(numbers_in(body) - numbers_in(source))
        if missing:
            raise SummaryInvalid(
                f"claim {n} contains number(s) {missing} not found in the article text"
            )
        out_claims.append({"text": body, "quote": " ".join(quote.split())})
    stance = data.get("stance")
    if stance not in STANCES:
        raise SummaryInvalid(f'"stance" must be one of {"/".join(STANCES)}')
    kind = data.get("kind")
    if kind not in KINDS:
        raise SummaryInvalid(f'"kind" must be one of {"/".join(KINDS)}')
    parties = data.get("key_parties")
    if not isinstance(parties, list) or not all(isinstance(p, str) for p in parties):
        raise SummaryInvalid('"key_parties" must be a list of strings')
    framing = " ".join(str(data.get("framing") or "").split())
    if _words(framing) > FRAMING_WORDS:
        raise SummaryInvalid(f'"framing" is longer than {FRAMING_WORDS} words')
    missing = sorted(numbers_in(framing) - numbers_in(source))
    if missing:
        raise SummaryInvalid(f'"framing" contains number(s) {missing} not in the article text')
    return {
        "claims": out_claims,
        "stance": stance,
        "kind": kind,
        "key_parties": [p.strip() for p in parties if p.strip()][:6],
        "framing": framing,
    }


# --- synthesis ---------------------------------------------------------------------------

_POLITICAL = re.compile(
    r"\b(?:far-left|far-right|left-wing|right-wing|leftist|rightist|left-leaning|"
    r"right-leaning|liberals?|conservatives?|progressives?|libertarians?|centrists?|"
    r"populists?|partisan|mainstream|tabloid|establishment|biased|the left|the right)\b",
    re.I,
)
"""A position label must describe the view, never an outlet's political leaning."""


def _cited(value: object, what: str, k: int) -> list[int]:
    if not isinstance(value, list) or not value:
        raise SummaryInvalid(f"{what} needs at least one citation")
    bad = [i for i in value if not isinstance(i, int) or isinstance(i, bool) or not 1 <= i <= k]
    if bad:
        raise SummaryInvalid(f"{what} cites {bad}; valid digest indexes are 1..{k}")
    return sorted(dict.fromkeys(value))


def _text_item(value: object, what: str, k: int, limit: int) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise SummaryInvalid(f'{what} must be {{"text": str, "citations": [int]}}')
    text = " ".join(value["text"].split())
    if not text:
        raise SummaryInvalid(f"{what} is empty")
    if _words(text) > limit:
        raise SummaryInvalid(f"{what} is longer than {limit} words")
    return {"text": text, "citations": _cited(value.get("citations"), what, k)}


def _check_numbers(text: str, cited: Sequence[int], texts: Sequence[str], what: str) -> None:
    available = set().union(*(numbers_in(texts[i - 1]) for i in cited))
    missing = sorted(numbers_in(text) - available)
    if missing:
        raise SummaryInvalid(f"{what} contains number(s) {missing} not in its cited sources")


def _position(
    value: object,
    what: str,
    k: int,
    digests: Sequence[dict],
    texts: Sequence[str],
    names: Sequence[str],
) -> dict | None:
    """One disagreement position; ``None`` when it must be dropped (bad quote or no
    citation), :class:`SummaryInvalid` when the whole answer should be retried."""
    if not isinstance(value, dict):
        raise SummaryInvalid(f'{what} must be {{"label", "summary", "citations", "quote"}}')
    label = " ".join(str(value.get("label") or "").split())
    if not label:
        raise SummaryInvalid(f"{what} has an empty label")
    if _words(label) > LABEL_WORDS:
        raise SummaryInvalid(f"{what} label is longer than {LABEL_WORDS} words")
    low = normalize(label)
    if (
        _POLITICAL.search(label)
        or low in {normalize(n) for n in names}
        or any(
            len(normalize(n)) >= 3 and re.search(rf"\b{re.escape(normalize(n))}\b", low)
            for n in names
        )
    ):
        raise SummaryInvalid(
            f'{what} label "{label}" is an outlet or political label; '
            "describe the view instead (e.g. 'says the tax will raise revenue')"
        )
    summary = " ".join(str(value.get("summary") or "").split())
    if not summary:
        raise SummaryInvalid(f"{what} has an empty summary")
    if _words(summary) > POSITION_WORDS:
        raise SummaryInvalid(f"{what} summary is longer than {POSITION_WORDS} words")
    cited = _cited(value.get("citations"), what, k)
    _check_numbers(f"{label} {summary}", cited, texts, what)
    quote = " ".join(str(value.get("quote") or "").split())
    if _words(quote) > QUOTE_WORDS:
        raise SummaryInvalid(f"{what} quote is longer than {QUOTE_WORDS} words")
    if not quote or not any(quote_in_text(quote, texts[i - 1]) for i in cited):
        return None  # unverifiable evidence: drop the position, keep the rest
    return {
        "label": label,
        "summary": summary,
        "citations": cited,
        "quote": quote,
    }


def validate_synthesis(
    content: str,
    digests: Sequence[dict],
    texts: Sequence[str],
    *,
    title: str = "",
    guard: VerbatimGuard | None = None,
) -> dict:
    """Check a raw synthesis answer; ``digests``[n-1] and ``texts``[n-1] are digest [n]'s
    digest dict and article text.

    Returns the cleaned synthesis (invalid disagreements omitted). Raises
    :class:`SummaryInvalid` for retryable structural problems."""
    data = parse_json(content)
    k = len(digests)
    names = [d.get("label", "") for d in digests]
    agreement_raw = data.get("agreement")
    disagreements_raw = data.get("disagreements")
    angles_raw = data.get("different_angles")
    for field, value in (
        ("agreement", agreement_raw),
        ("disagreements", disagreements_raw),
        ("different_angles", angles_raw),
    ):
        if not isinstance(value, list):
            raise SummaryInvalid(f'"{field}" must be a list')
    agreement = [
        _text_item(item, f"agreement {n}", k, AGREEMENT_WORDS)
        for n, item in enumerate(agreement_raw, 1)
    ]
    angles = [
        _text_item(item, f"different_angles {n}", k, ANGLE_WORDS)
        for n, item in enumerate(angles_raw, 1)
    ]
    disagreements: list[dict] = []
    for n, item in enumerate(disagreements_raw, 1):
        what = f"disagreement {n}"
        if not isinstance(item, dict) or not isinstance(item.get("positions"), list):
            raise SummaryInvalid(f'{what} must be {{"topic", "positions": [...]}}')
        topic = " ".join(str(item.get("topic") or "").split())
        if not topic:
            raise SummaryInvalid(f"{what} has an empty topic")
        if _words(topic) > TOPIC_WORDS:
            raise SummaryInvalid(f"{what} topic is longer than {TOPIC_WORDS} words")
        positions = [
            p
            for m, raw in enumerate(item["positions"], 1)
            if (p := _position(raw, f"{what} position {m}", k, digests, texts, names)) is not None
        ]
        # Accept only if ≥2 positions cite different sources and the cited digests'
        # stances differ; otherwise omit the disagreement entirely.
        distinct = {(p["citations"][0],) for p in positions}
        stances = {digests[p["citations"][0] - 1]["digest"]["stance"] for p in positions}
        if len(positions) >= 2 and len(distinct) >= 2 and len(stances) >= 2:
            disagreements.append({"topic": topic, "positions": positions})
    for n, item in enumerate(agreement + angles, 1):
        _check_numbers(item["text"], item["citations"], texts, f"item {n}")
    if guard is not None:
        points = [
            *[(f"agreement {n}", a["text"]) for n, a in enumerate(agreement, 1)],
            *[(f"{d['topic']} label", p["label"]) for d in disagreements for p in d["positions"]],
            *[
                (f"{d['topic']} summary", p["summary"])
                for d in disagreements
                for p in d["positions"]
            ],
            *[(f"angle {n}", a["text"]) for n, a in enumerate(angles, 1)],
        ]
        check_verbatim(points, texts, guard, title=title)
    return {
        "agreement": agreement,
        "disagreements": disagreements,
        "different_angles": angles,
    }
