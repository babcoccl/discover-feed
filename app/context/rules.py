"""``context.same_event`` rules — a stdlib-only module so ``app.config`` can import
``SameEventRules`` without pulling in the context/enrich filter chain (which loops
back through ``app.normalize`` -> ``app.sources.base`` -> ``app.config``)."""

from dataclasses import dataclass

CONTEXT_TASK_TYPES = (
    "history",
    "quotes",
    "financials",
    "related_developments",
    "background",
)
"""The planner's fixed retrieval catalog (``context.actions`` keys). The LLM picks from
this list only — it never writes raw queries (``app.context.catalog`` builds them)."""

DEFAULT_EVENT_TERMS = (
    # verbs
    "announce",
    "unveil",
    "launch",
    "acquire",
    "raise",
    "approve",
    "reject",
    "veto",
    "sign",
    "ban",
    "block",
    "fine",
    "sue",
    "settle",
    "recall",
    "leak",
    "hack",
    "exploit",
    "breach",
    "kill",
    "resign",
    "appoint",
    "hire",
    "fire",
    "arrest",
    "indict",
    "charge",
    "convict",
    "plead",
    "sentence",
    "confirm",
    "deny",
    "reveal",
    "disclose",
    "buy",
    "sell",
    "expand",
    "collapse",
    "file",
    "rollout",
    "debut",
    "cut",
    "drop",
    "surge",
    "plunge",
    "crash",
    "rally",
    "soar",
    "strike",
    "attack",
    # nouns
    "acquisition",
    "merger",
    "funding",
    "ipo",
    "layoff",
    "outage",
    "settlement",
    "verdict",
    "plea",
    "bankruptcy",
    "probe",
    "trial",
    "deal",
    "partnership",
    "award",
    "contract",
    "death",
    "protest",
)
"""Headline event words (verbs/nouns); inflections meet via ``_event_stem``."""


@dataclass(frozen=True)
class SameEventRules:
    """Deterministic same-event signals (``context.same_event`` in YAML). Applied to
    every page that survives ``context_text_check`` — before and independently of the
    LLM judge, whose ``same_event`` flag is inconsistent on a small local model."""

    numbers_min_shared: int = 2
    """Shared numeric tokens (no single digits or 4-digit years) between the
    candidate's title + first ``numbers_text_chars`` and the story's title + brief
    => ``same_event`` (e.g. "960 MW", "$6.2 billion", "1,068 tokens")."""
    numbers_text_chars: int = 600
    window_hours: float = 72
    """A candidate dated within +/- this many hours of the story's ``first_at`` is
    contemporaneous; so is an undated page sharing ``undated_min_entities``."""
    undated_min_entities: int = 3
    """An undated page sharing this many of the story's entities is treated as
    contemporaneous (it cannot prove it is older)."""
    max_title_similarity: float = 0.30
    max_text_similarity: float = 0.30
    """A contemporaneous page must stay under BOTH caps — same-event coverage inside
    the event window reads a lot like the story even below ``similarity_ceiling``."""
    entities_min: int = 3
    """A candidate title sharing this many of the story's named entities plus an
    ``event_terms`` word that also appears in a story headline => ``same_event``."""
    event_terms: tuple[str, ...] = DEFAULT_EVENT_TERMS
