"""Post-hoc checks of a streamed answer (never blocking the stream, no retries):
every [n] is a listed source, every number appears in the sources, and (warning level) no
factual sentence goes uncited."""

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from app.summarize.prompt import SourceDoc
from app.summarize.validate import numbers_in, split_sentences

MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_REFUSAL = re.compile(
    r"\b(?:sources?|articles?|coverage|reporting)\b[^.?!]{0,40}\b"
    r"(?:do(?:es)?n['\u2019]?t|do(?:es)? not|never)\b[^.?!]{0,20}\b"
    r"(?:say|mention|state|contain|include|cover|specify|provide|address)"
    r"|\bnot (?:mentioned|stated|covered|specified) in the\b|\bno information\b|"
    r"\bcan(?:no|['\u2019])t (?:find|tell|say)\b",
    re.I,
)
_CAPITALIZED = re.compile(r"(?<!^)(?<![.!?]\s)\b[A-Z][a-z]+")


@dataclass
class Check:
    name: str
    ok: bool
    level: str
    """``error`` (a hard rule) or ``warning`` (a heuristic)."""
    detail: str = ""


@dataclass
class Verification:
    checks: list[Check] = field(default_factory=list)
    cited: list[int] = field(default_factory=list)
    """Valid source indexes the answer cites, in first-use order."""
    refusal: bool = False
    """The answer says the sources don't contain it."""

    @property
    def verified(self) -> bool:
        return all(c.ok for c in self.checks)

    def as_json(self) -> dict:
        return {
            "verified": self.verified,
            "refusal": self.refusal,
            "cited": self.cited,
            "checks": [asdict(c) for c in self.checks],
        }


def cited_indexes(answer: str) -> list[int]:
    found: list[int] = []
    for m in MARKER.finditer(answer):
        for n in m.group(1).split(","):
            found.append(int(n))
    return list(dict.fromkeys(found))


def strip_markers(answer: str) -> str:
    return " ".join(MARKER.sub(" ", answer).split())


def _sentences(answer: str) -> list[str]:
    """Sentences with their markers kept inside (``X. [1] Y`` -> ``X [1].``, ``Y``)."""
    moved = re.sub(r"([.!?])((?:\s*\[\d+(?:\s*,\s*\d+)*\])+)", r"\2\1", answer)
    return [s for s in split_sentences(" ".join(moved.split())) if s.strip()]


def _factual(sentence: str) -> bool:
    bare = strip_markers(sentence)
    words = bare.split()
    if len(words) < 6 or bare.rstrip().endswith("?"):
        return False
    return bool(re.search(r"\d", bare)) or bool(_CAPITALIZED.search(bare)) or len(words) >= 10


def verify(answer: str, sources: Sequence[SourceDoc], question: str = "") -> Verification:
    k = len(sources)
    indexes = cited_indexes(answer)
    bad = [i for i in indexes if not 1 <= i <= k]
    result = Verification(cited=[i for i in indexes if 1 <= i <= k])
    result.checks.append(
        Check(
            "citations_in_range",
            not bad,
            "error",
            f"cites {bad}; the sources are [1]..[{k}]" if bad else "",
        )
    )
    available = set().union(*(numbers_in(s.grounding) for s in sources), numbers_in(question))
    missing = sorted(numbers_in(strip_markers(answer)) - available)
    result.checks.append(
        Check(
            "numbers_in_sources",
            not missing,
            "error",
            f"number(s) {missing} do not appear in the sources" if missing else "",
        )
    )
    result.refusal = bool(_REFUSAL.search(answer))
    uncited = []
    if not result.refusal:
        uncited = [s for s in _sentences(answer) if _factual(s) and not MARKER.search(s)]
    result.checks.append(
        Check(
            "uncited_sentences",
            not uncited,
            "warning",
            f"{len(uncited)} sentence(s) without a citation: {uncited[0][:80]!r}"
            if uncited
            else "",
        )
    )
    return result
