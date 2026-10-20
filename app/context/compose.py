"""The 'Additional detail' composer (planner mode): one LLM call per story turns the
accepted, verified extractions into reader-facing notes (<= 30 words each, grounded
only in the extracted facts/quotes) and, for history tasks with both a verified
earlier quote and a verified current quote, a 'then and now' comparison
{relation, summary, references}. Code-side validation: notes cap at 30 words, the
comparison's quotes must be verbatim in their pages, dates come from verified page
metadata, loaded words are rejected unless they occur in a cited source, and the box
only shows when relation is ``changed`` or ``expanded`` (``consistent``/``unrelated``
keep a plain timeline). A comparison without both quotes is never produced."""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.context.extract import _norm, _words

logger = logging.getLogger(__name__)


RELATIONS = ("consistent", "changed", "expanded", "unrelated")
SHOW_RELATIONS = ("changed", "expanded")
LOADED_WORDS = ("reversal", "flip-flop", "u-turn", "backtrack", "contradicts")
"""Characterizations banned in a summary unless they occur in a cited source quote."""
MAX_NOTE_WORDS = 30
MAX_SUMMARY_WORDS = 60

COMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "note": {"type": "string"}},
                "required": ["id", "note"],
                "additionalProperties": False,
            },
        },
        "then_and_now": {
            "type": ["object", "null"],
            "properties": {
                "relation": {"type": "string", "enum": list(RELATIONS)},
                "summary": {"type": "string"},
                "references": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["relation", "summary", "references"],
            "additionalProperties": False,
        },
    },
    "required": ["notes"],
    "additionalProperties": False,
}

SYSTEM = (
    "You write short reader-facing notes for a news app's 'Additional detail' "
    "section. For each numbered item, write one note of at most 30 words, grounded "
    "only in that item's fact and quote — no outside knowledge, no loaded language "
    "(never 'reversal', 'flip-flop', 'U-turn', 'backtrack', 'contradicts'). When an "
    "earlier quote and a current quote from the same subject are both provided, you "
    "may add then_and_now: relation consistent|changed|expanded|unrelated, a summary "
    "of at most 60 words naming what stayed the same or changed, and references = "
    "the two item ids (earlier first). If the two quotes say the same thing, use "
    "relation consistent and keep it short."
)


@dataclass
class DetailItem:
    """A verified extraction feeding the composer (id = its number in the prompt)."""

    id: int
    task_type: str
    subject: str
    fact: str
    quote: str
    speaker: str
    date: datetime | None
    url: str
    outlet: str
    origin: str  # archive | web
    note: str = ""


@dataclass
class Composed:
    notes: dict[int, str] = field(default_factory=dict)
    then_and_now: dict | None = None
    raw: str = ""
    error: str = ""


def _loaded(summary: str, sources: list[str]) -> bool:
    """A loaded word in the summary that none of the cited quotes uses."""
    words = set(re.findall(r"[a-z-]+", _norm(summary)))
    cited = set(re.findall(r"[a-z-]+", _norm(" ".join(sources))))
    return any(w in words and w not in cited for w in LOADED_WORDS)


def validate_composed(payload: dict, items: list[DetailItem]) -> Composed:
    """Apply the composer JSON to the verified items; drop anything unverifiable."""
    out = Composed(raw=json.dumps(payload)[:2000] if payload else "")
    by_id = {i.id: i for i in items}
    for entry in payload.get("notes") or []:
        try:
            iid = int(entry.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        note = " ".join(str(entry.get("note") or "").split())
        if iid in by_id and note and _words(note) <= MAX_NOTE_WORDS:
            out.notes[iid] = note
    tan = payload.get("then_and_now")
    if not isinstance(tan, dict):
        return out
    relation = str(tan.get("relation") or "")
    summary = " ".join(str(tan.get("summary") or "").split())
    try:
        refs = [int(r) for r in (tan.get("references") or [])]
    except (TypeError, ValueError):
        refs = []
    if relation not in RELATIONS or not summary or _words(summary) > MAX_SUMMARY_WORDS:
        return out
    cited = [by_id[r] for r in refs if r in by_id]
    dated = [i for i in cited if i.date is not None]
    if len(dated) < 2:
        return out  # never a comparison without two verified (dated) quotes
    earlier, current = min(dated, key=lambda i: i.date), max(dated, key=lambda i: i.date)
    if earlier is current or _loaded(summary, [earlier.quote, current.quote]):
        return out
    out.then_and_now = {
        "relation": relation,
        "summary": summary,
        "references": [earlier.id, current.id],
        "show": relation in SHOW_RELATIONS,
    }
    return out


def _items_block(items: list[DetailItem]) -> str:
    lines = []
    for i in items:
        date = i.date.date().isoformat() if i.date else "undated"
        lines.append(
            f"[{i.id}] {i.task_type} on {i.subject} ({i.outlet}, {date}, {i.origin})\n"
            f"    fact: {i.fact}\n    quote: {i.quote}" + (f" — {i.speaker}" if i.speaker else "")
        )
    return "\n".join(lines)


async def compose(
    story_title: str,
    brief: str,
    items: list[DetailItem],
    *,
    llm_role,
    llm_transport=None,
) -> Composed:
    """One LLM call per story, only when at least one item was accepted."""
    if not items:
        return Composed()
    from app.llm.client import LLMClient

    role = llm_role.model_copy(update={"disable_thinking": True, "max_tokens": 700})
    client = LLMClient(role, transport=llm_transport)
    user = (
        f"Story: {story_title}\n\nBrief:\n{brief[:800]}\n\nVerified items:\n{_items_block(items)}"
    )
    try:
        chat = await client.chat(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            json_schema=COMPOSE_SCHEMA,
            schema_name="context_compose",
        )
        payload = json.loads(chat.content)
    except Exception as exc:
        logger.info("context compose failed (%s)", exc)
        return Composed(error=f"{type(exc).__name__}: {exc}"[:300])
    if not isinstance(payload, dict):
        return Composed(error="unparseable")
    out = validate_composed(payload, items)
    out.raw = chat.content[:2000]
    return out
