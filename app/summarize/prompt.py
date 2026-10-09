"""The versioned prompts and response schemas. Bump a prompt version on any change: it is
part of the cache key, so every story is regenerated with the new prompt.

``brief`` (cards) and ``report`` (story page) are the current artifacts; the Phase 4
``summary`` prompt is kept as the brief's first fallback."""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

PROMPT_VERSION = "summary-v1"
TARGET_WORDS = 60

BRIEF_PROMPT_VERSION = "brief-v1"
REPORT_PROMPT_VERSION = "report-v1"
PROMPT_VERSIONS = {"brief": BRIEF_PROMPT_VERSION, "report": REPORT_PROMPT_VERSION}

LEAD_MAX_WORDS = 30
BULLET_MAX_WORDS = 28
BULLET_COUNT = 3
REPORT_MIN_PARAGRAPHS = 3
REPORT_MAX_PARAGRAPHS = 5
REPORT_TARGET_WORDS = (250, 450)
REPORT_WORD_RANGE = (225, 500)
"""What the validator accepts: the prompt's target plus a 10% margin."""
VERBATIM_RUN = 8
"""Reports may not copy this many consecutive words from a source."""

_CITED_POINT = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["text", "citations"],
    "additionalProperties": False,
}

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "lead": {"type": "string"},
        "lead_citations": {"type": "array", "items": {"type": "integer"}},
        "bullets": {
            "type": "array",
            "items": _CITED_POINT,
            "minItems": BULLET_COUNT,
            "maxItems": BULLET_COUNT,
        },
    },
    "required": ["lead", "lead_citations", "bullets"],
    "additionalProperties": False,
}

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "paragraphs": {
            "type": "array",
            "items": _CITED_POINT,
            "minItems": REPORT_MIN_PARAGRAPHS,
            "maxItems": REPORT_MAX_PARAGRAPHS,
        },
    },
    "required": ["paragraphs"],
    "additionalProperties": False,
}

BRIEF_SYSTEM_PROMPT = f"""You write news briefs for a personal news reader.
You get numbered sources [1]..[k] that all cover the same story.
Write:
- "lead": exactly one sentence of at most {LEAD_MAX_WORDS} words stating what happened.
- "bullets": exactly {BULLET_COUNT} distinct supporting points of at most {BULLET_MAX_WORDS} \
words each, in this order: what happened in more detail; why it matters or the context; \
what happens next or a key detail.
Rules:
- Use only facts stated in the sources. No background knowledge, opinions or speculation.
- Neutral tone. No bullet may repeat the lead or another bullet.
- Copy every number, percentage and amount exactly as a source states it.
- Do not put citation markers like [1] in the text.
- "lead_citations" and each bullet's "citations" list the indexes of the sources that \
support it (at least one each).
Reply with JSON only, no prose: {{"lead": "<sentence>", "lead_citations": [1], \
"bullets": [{{"text": "<point>", "citations": [1, 2]}}, {{"text": "<point>", \
"citations": [2]}}, {{"text": "<point>", "citations": [3]}}]}}"""

REPORT_SYSTEM_PROMPT = f"""You write detailed news reports for a personal news reader.
You get numbered sources [1]..[k] that all cover the same story.
Write {REPORT_MIN_PARAGRAPHS}-{REPORT_MAX_PARAGRAPHS} paragraphs, \
{REPORT_TARGET_WORDS[0]}-{REPORT_TARGET_WORDS[1]} words in total, of plain journalistic \
prose covering: what happened; background and context; details and reactions from the \
sources; what to watch next.
Rules:
- Use only facts stated in the sources. No background knowledge, opinions or speculation.
- Paraphrase in your own words; never copy a sentence or a long phrase from a source.
- Attribute claims that sources disagree on ("according to ..."). Say so when sources \
conflict or when information is missing.
- Copy every number, percentage and amount exactly as a source states it.
- Do not put citation markers like [1] in the text.
- Each paragraph's "citations" lists the indexes of the sources it draws on (at least one).
Reply with JSON only, no prose: {{"paragraphs": [{{"text": "<paragraph>", \
"citations": [1, 2]}}, {{"text": "<paragraph>", "citations": [3]}}]}}"""


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "integer"}},
        },
    },
    "required": ["summary", "citations"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = f"""You summarize news stories for a personal news reader.
You get numbered sources [1]..[k] that all cover the same story.
Write a neutral summary of 2-3 sentences and at most {TARGET_WORDS} words.
Rules:
- Use only facts stated in the sources. No background knowledge, opinions or speculation.
- Copy every number, percentage and amount exactly as a source states it.
- Do not put citation markers like [1] in the summary text.
- For each sentence, in order, list the indexes of the sources that support it.
Reply with JSON only, no prose: {{"summary": "<text>", "citations": [[1, 2], [3]]}}
with exactly one list of source indexes per sentence."""


PROMPTS = {
    "summary": (SYSTEM_PROMPT, RESPONSE_SCHEMA, "Write the JSON summary."),
    "brief": (BRIEF_SYSTEM_PROMPT, BRIEF_SCHEMA, "Write the JSON brief."),
    "report": (REPORT_SYSTEM_PROMPT, REPORT_SCHEMA, "Write the JSON report."),
}


@dataclass(frozen=True)
class SourceDoc:
    """One numbered source as sent to the model."""

    index: int
    article_id: int
    source: str
    headline: str
    url: str
    text: str
    """The words sent to the model: the start of the extracted text, or the feed summary."""
    full_text: bool

    @property
    def grounding(self) -> str:
        """Everything the model saw for this source (used to check numbers)."""
        return f"{self.headline}\n{self.text}"

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(f"{self.headline}\n{self.text}".encode()).hexdigest()


def format_sources(sources: Sequence[SourceDoc]) -> str:
    return "\n\n".join(
        f"[{s.index}] Source: {s.source}\nHeadline: {s.headline}\nText: {s.text}" for s in sources
    )


def build_messages(
    sources: Sequence[SourceDoc],
    *,
    previous: str | None = None,
    error: str | None = None,
    kind: str = "summary",
) -> list[dict[str, str]]:
    """Messages for one attempt; a retry adds the rejected answer and the validation error."""
    system, _schema, ask = PROMPTS[kind]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Sources:\n\n{format_sources(sources)}\n\n{ask}"},
    ]
    if error is not None:
        messages += [
            {"role": "assistant", "content": previous or ""},
            {
                "role": "user",
                "content": f"That answer was rejected: {error}. Reply again with corrected JSON "
                "only, following every rule.",
            },
        ]
    return messages


_BLOCK = re.compile(
    r"^\[(\d+)\] Source: ([^\n]*)\nHeadline: ([^\n]*)\nText: (.*?)"
    r"(?=\n\n\[\d+\] Source: |\n\nWrite |\Z)",
    re.M | re.S,
)


def parse_sources(user_message: str) -> list[tuple[int, str, str, str]]:
    """``(index, source, headline, text)`` blocks of a prompt (used by the fake LLM)."""
    return [(int(m[1]), m[2], m[3], m[4].strip()) for m in _BLOCK.finditer(user_message)]
