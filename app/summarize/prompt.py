"""The versioned summary prompt and response schema. Bump PROMPT_VERSION on any change:
it is part of the cache key, so every story is re-summarized with the new prompt."""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

PROMPT_VERSION = "summary-v1"
TARGET_WORDS = 60

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
    sources: Sequence[SourceDoc], *, previous: str | None = None, error: str | None = None
) -> list[dict[str, str]]:
    """Messages for one attempt; a retry adds the rejected answer and the validation error."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Sources:\n\n{format_sources(sources)}\n\nWrite the JSON summary.",
        },
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
