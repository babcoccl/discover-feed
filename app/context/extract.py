"""The page extractor/verifier (planner mode): one LLM call per fetched page answers
``EXTRACT_SCHEMA`` — a short grounded fact plus a verbatim evidence quote. Code then
verifies every claim against the page text before the item may be accepted: the quote
must occur verbatim (normalized), numbers in the fact must occur in the page, the
task's subject must appear in the page (ideally in the quote's paragraph), and the
date must check out (undated pages are rejected except for background; history pages
must predate the story by ``min_age_days``). One retry on invalid output, then drop —
nothing is fabricated."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from app.context.rules import CONTEXT_TASK_TYPES

logger = logging.getLogger(__name__)


EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "kind": {"type": "string", "enum": list(CONTEXT_TASK_TYPES)},
        "fact": {"type": "string"},
        "evidence_quote": {"type": "string"},
        "quote_speaker": {"type": "string"},
        "date_from_page": {"type": "string"},
    },
    "required": ["relevant", "kind", "fact", "evidence_quote", "quote_speaker", "date_from_page"],
    "additionalProperties": False,
}

EXTRACT_CHARS = 6000
MAX_FACT_WORDS = 30
MAX_QUOTE_WORDS = 25

SYSTEM = (
    "You extract one verifiable detail from a web page for a news reader. Answer JSON "
    "only: relevant (does the page contain a fact answering the reader's question?), "
    "kind (the task type it serves), fact (at most 30 words, grounded in the page — "
    "numbers must come from the page), evidence_quote (at most 25 consecutive words "
    "copied verbatim from the page proving the fact), quote_speaker (who said the "
    "quote, or empty), date_from_page (the page's publication date if shown, else "
    "empty). Never paraphrase the quote; never add facts not on the page."
)

TASK_LABELS = {
    "history": "earlier history on the subject",
    "quotes": "an on-the-record quote by the subject",
    "financials": "a funding/valuation/revenue/market fact",
    "related_developments": "a related recent development",
    "background": "background or reference material",
}


@dataclass
class Extraction:
    relevant: bool = False
    kind: str = ""
    fact: str = ""
    quote: str = ""
    speaker: str = ""
    date_from_page: str = ""
    raw: str = ""
    error: str = ""


_QUOTES = str.maketrans(
    {c: '"' for c in "“”«»„"} | {c: "'" for c in "‘’"}  # noqa: RUF001 — curly quotes ARE the point
)


def _norm(s: str) -> str:
    """Case/whitespace/quote-normalized text for verbatim checks."""
    return re.sub(r"\s+", " ", s.translate(_QUOTES).casefold()).strip()


def _words(s: str) -> int:
    return len(_norm(s).split())


def _numbers(s: str) -> set[str]:
    """Digit runs in the text (a fact's numbers must literally appear on the page)."""
    return set(re.findall(r"\d[\d,.]*", _norm(s)))


def quote_in_text(quote: str, page_text: str) -> bool:
    """The evidence quote occurs verbatim in the page (case/whitespace-normalized)."""
    q, t = _norm(quote), _norm(page_text)
    return bool(q) and q in t


def numbers_in_text(fact: str, page_text: str) -> bool:
    page = _norm(page_text)
    return all(n.rstrip(",.") in page for n in _numbers(fact))


def subject_attribution(subject: str, quote: str, page_text: str) -> str:
    """``absent`` when the subject is nowhere on the page, ``paragraph`` when it shares
    the quote's paragraph, else ``page``."""
    text = _norm(page_text)
    name = _norm(subject)
    if not name or name not in text:
        return "absent"
    q = _norm(quote)
    for para in re.split(r"\n\s*\n|\n", page_text):
        p = _norm(para)
        if q and q in p:
            return "paragraph" if name in p else "page"
    return "page"


def _parse_date(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d %B %Y", "%B %d, %Y", "%Y"):
        try:
            return datetime.strptime(s[: len(fmt) + 10], fmt)
        except ValueError:
            continue
    m = re.search(r"(20\d\d|19\d\d)", s)
    return datetime(int(m.group(1)), 1, 1) if m else None


def verify(
    ex: Extraction,
    *,
    task_type: str,
    subject: str,
    page_text: str,
    page_date: datetime | None,
    first_at: datetime,
    min_age_days: float,
    now: datetime,
) -> tuple[bool, str]:
    """Accept/reject an extraction against the page itself. ``page_date`` is the
    verified date (page metadata preferred over the model's ``date_from_page``)."""
    if ex.error:
        return False, "extract_error"
    if not ex.relevant:
        return False, "not_relevant"
    if ex.kind != task_type:
        return False, "kind_mismatch"
    if not ex.fact or _words(ex.fact) > MAX_FACT_WORDS:
        return False, "bad_fact"
    if not ex.quote or _words(ex.quote) > MAX_QUOTE_WORDS:
        return False, "bad_quote"
    if not quote_in_text(ex.quote, page_text):
        return False, "quote_not_verbatim"
    if not numbers_in_text(ex.fact, page_text):
        return False, "numbers_not_on_page"
    if subject and subject_attribution(subject, ex.quote, page_text) == "absent":
        return False, "subject_absent"
    date = page_date or _parse_date(ex.date_from_page)
    if task_type != "background":
        if date is None:
            return False, "undated"
        if task_type == "history" and date > first_at:
            return False, "not_history"  # page postdates the story
        if task_type == "history" and date > first_at - _days(min_age_days):
            return False, "too_recent"  # inside min_age_days: not "history"
        if date > now + _days(2):
            return False, "future_dated"
    return True, ""


def _days(n: float):
    from datetime import timedelta

    return timedelta(days=n)


def parse_extraction(raw: str) -> Extraction:
    ex = Extraction(raw=raw[:2000])
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        ex.error = "unparseable"
        return ex
    if not isinstance(data, dict):
        ex.error = "unparseable"
        return ex
    ex.relevant = bool(data.get("relevant"))
    ex.kind = str(data.get("kind") or "")
    ex.fact = " ".join(str(data.get("fact") or "").split())
    ex.quote = " ".join(str(data.get("evidence_quote") or "").split())
    ex.speaker = " ".join(str(data.get("quote_speaker") or "").split())
    ex.date_from_page = " ".join(str(data.get("date_from_page") or "").split())
    return ex


async def extract(
    page_text: str,
    *,
    task_type: str,
    subject: str,
    question: str,
    story_title: str,
    llm_role,
    llm_transport=None,
    retries: int = 1,
) -> Extraction:
    """One LLM call per candidate page (retried once on unparseable output). The
    planner's ``question`` is passed as reader intent — never a search query."""
    from app.llm.client import LLMClient

    role = llm_role.model_copy(update={"disable_thinking": True, "max_tokens": 300})
    client = LLMClient(role, transport=llm_transport)
    user = (
        f"Task: find {TASK_LABELS.get(task_type, task_type)} about {subject}.\n"
        f"Reader's question: {question}\n"
        f"For the story: {story_title}\n\n"
        f"Page text:\n{page_text[:EXTRACT_CHARS]}"
    )
    last = Extraction()
    purpose = "history_extract" if task_type == "history" else "context_extract"
    for _try in range(1 + retries):
        try:
            chat = await client.chat(
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                purpose=purpose,
                attempt=_try + 1,
                json_schema=EXTRACT_SCHEMA,
                schema_name="context_extract",
            )
            last = parse_extraction(chat.content)
        except Exception as exc:
            last = Extraction(error=f"{type(exc).__name__}: {exc}"[:300])
        if not last.error:
            return last
    return last
