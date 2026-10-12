"""Pure rule matching on top of the topic matcher (whole words, exclude wins)."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.config import AlertKeyword
from app.topics import contains_keyword


@dataclass(frozen=True)
class Keyword:
    keyword: str
    case_sensitive: bool = False

    @property
    def label(self) -> str:
        return f"{self.keyword} (Aa)" if self.case_sensitive else self.keyword


@dataclass(frozen=True)
class RuleSpec:
    include: Sequence[Keyword]
    exclude: Sequence[Keyword] = ()
    match_all: bool = False
    match_in: Sequence[str] = ("title", "summary")
    source_ids: Sequence[str] = ()


def keywords(items: Iterable[dict]) -> list[Keyword]:
    return [Keyword(i["keyword"], bool(i.get("case_sensitive"))) for i in items or ()]


def normalize(items: Iterable[str | dict | AlertKeyword]) -> list[dict]:
    """Keyword dicts (``$AAPL`` -> case-sensitive ``AAPL``), blanks and duplicates dropped."""
    out: list[dict] = []
    seen: set[tuple[str, bool]] = set()
    for item in items:
        parsed = item if isinstance(item, AlertKeyword) else AlertKeyword.model_validate(item)
        key = (
            parsed.keyword if parsed.case_sensitive else parsed.keyword.casefold(),
            parsed.case_sensitive,
        )
        if key not in seen:
            seen.add(key)
            out.append({"keyword": parsed.keyword, "case_sensitive": parsed.case_sensitive})
    return out


def split(text: str) -> list[dict]:
    """A comma/newline separated field (``$AAPL`` entries are case-sensitive tickers)."""
    return normalize(p for p in (" ".join(v.split()) for v in re.split(r"[,\n]", text or "")) if p)


def matched_keywords(
    rule: RuleSpec, *, source_id: str, fields: dict[str, str | None]
) -> list[str] | None:
    """The include keywords found (None when the rule does not match). ``fields`` maps
    ``title`` / ``summary`` / ``text`` to their text; only ``rule.match_in`` are searched."""
    if rule.source_ids and source_id not in rule.source_ids:
        return None
    text = "\n".join(fields.get(name) or "" for name in rule.match_in)
    if not text.strip():
        return None
    if any(contains_keyword(text, k.keyword, k.case_sensitive) for k in rule.exclude):
        return None
    found = [k for k in rule.include if contains_keyword(text, k.keyword, k.case_sensitive)]
    if not found or (rule.match_all and len(found) < len(rule.include)):
        return None
    return [k.keyword for k in found]
