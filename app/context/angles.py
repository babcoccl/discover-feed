"""Search angles for the context search: what kind of background to look for.

The LLM (summarizer role) proposes up to ``max_angles`` angles — each a short ``label``
(``"history"``, ``"opposing view"`` ...) plus a keyword ``query`` — from the story's
title, brief and top entities. Without an LLM (or when it fails) a heuristic fallback
builds angles from the story's top entity + a context word (``history`` / ``explained`` /
``background``); those angles are marked ``heuristic`` and need an extra shared entity in
``filters`` (they are blunter).

Queries use ``app.enrich.query``'s format rules: few signal terms, entities quoted,
no standalone token shorter than 3 chars; ``adapt_query`` applies per-provider limits.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.config import LLMRole
from app.enrich.query import Term, adapt_query
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)

ANGLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "angles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": ["label", "query"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["angles"],
    "additionalProperties": False,
}

CONTEXT_WORDS = ("history", "explained", "background")


@dataclass
class Angle:
    """One way to search for background: ``label`` is shown to readers, ``query`` is sent
    to providers (normalized per provider), ``heuristic`` marks fallback angles."""

    label: str
    query: str
    heuristic: bool = False


def parse_angles(content: str, *, max_angles: int) -> list[Angle]:
    """The LLM's angles; ``[]`` on malformed output (the caller falls back, nothing is
    fabricated)."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return []
    items = data.get("angles") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label, query = str(item.get("label") or "").strip(), str(item.get("query") or "")
        if label and query.strip():
            out.append(Angle(label=label[:80], query=query.strip()))
    return out[:max_angles]


def heuristic_angles(terms: list[Term], *, max_angles: int) -> list[Angle]:
    """Fallback: the story's top entity + each context word
    (``"Springfield transit authority" history`` ...), ``heuristic=True``."""
    entity = next((t.text for t in terms if t.text), "")
    if not entity:
        return []
    words = CONTEXT_WORDS[: max(1, max_angles)]
    return [
        Angle(label=word, query=f'"{entity}" {word}', heuristic=True) for word in words[:max_angles]
    ]


def _messages(title: str, brief: str, entities: list[str], max_angles: int) -> list[dict]:
    entity_note = f"Top entities: {', '.join(entities)}." if entities else ""
    return [
        {
            "role": "system",
            "content": (
                f"Suggest up to {max_angles} search angles to find pages that add "
                "background to a news story: history, context, data, or opposing views. "
                "They must NOT be about the event itself or its recent news coverage. "
                "Each angle has a short 'label' (2-3 words) and a 'query' of 3-5 signal "
                "terms; quote multi-word entities."
            ),
        },
        {
            "role": "user",
            "content": f"Story: {title}\n{entity_note}\n\nBrief:\n{brief[:1500]}",
        },
    ]


async def make_angles(
    title: str,
    brief: str,
    terms: list[Term],
    *,
    max_angles: int,
    providers: list[str],
    llm_role: LLMRole | None = None,
    llm_transport=None,
) -> list[Angle]:
    """The story's search angles: LLM-suggested when a role is configured and answers
    parseably, else the heuristic fallback. Every query is normalized to the providers'
    rules; an empty query drops the angle."""
    angles: list[Angle] = []
    if llm_role is not None:
        role = llm_role.model_copy(update={"disable_thinking": True, "max_tokens": 300})
        entities = [t.text for t in terms[:6]]
        try:
            client = LLMClient(role, transport=llm_transport)
            result = await client.chat(
                _messages(title, brief, entities, max_angles),
                json_schema=ANGLE_SCHEMA,
                schema_name="context_angles",
            )
            angles = parse_angles(result.content, max_angles=max_angles)
        except Exception as exc:
            logger.warning("context angles LLM failed (%s); heuristic fallback", exc)
    if not angles:
        angles = heuristic_angles(terms, max_angles=max_angles)
    provider = providers[0] if providers else ""
    for angle in angles:
        angle.query = adapt_query(provider, angle.query)
    return [a for a in angles if a.query]
