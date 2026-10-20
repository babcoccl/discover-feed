"""Prompts for the per-source digest and the story-level perspectives synthesis.

The digest runs once per member article (cached by the article's content); the
synthesis runs over the numbered digests. Both are JSON-only prompts kept small
for slow local models.
"""

from collections.abc import Sequence

from app.summarize.prompt import SourceDoc, format_sources

DIGEST_PROMPT_VERSION = "digest-v1"
PERSPECTIVES_PROMPT_VERSION = "perspectives-v1"

STANCES = ("supports", "disputes", "adds", "neutral", "unclear")
KINDS = ("reporting", "analysis", "opinion", "fact-check")

QUOTE_WORDS = 15
CLAIM_WORDS = 24
FRAMING_WORDS = 12
AGREEMENT_WORDS = 24
TOPIC_WORDS = 8
LABEL_WORDS = 10
POSITION_WORDS = 24
ANGLE_WORDS = 24

DIGEST_SYSTEM = """You distil one article's coverage of a news story into a JSON digest.
Rules:
- claims: up to 3 short claims the article makes; every "quote" must be copied word
  for word from the article text, at most 15 words.
- stance: how the article treats the story's central claim — supports | disputes |
  adds | neutral | unclear.
- kind: reporting | analysis | opinion | fact-check.
- key_parties: people, organisations or places the article centres on (at most 6).
- framing: at most 12 words describing how the article presents the story.
- Every number must appear in the article text.
Reply with JSON only, no prose:
{"claims": [{"text": "...", "quote": "..."}], "stance": "...",
"kind": "...", "key_parties": ["..."], "framing": "..."}"""

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["text", "quote"],
                "additionalProperties": False,
            },
        },
        "stance": {"type": "string", "enum": list(STANCES)},
        "kind": {"type": "string", "enum": list(KINDS)},
        "key_parties": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        "framing": {"type": "string"},
    },
    "required": ["claims", "stance", "kind", "key_parties", "framing"],
    "additionalProperties": False,
}

SYNTH_SYSTEM = """You compare how different outlets cover the same story, using only the
numbered per-source digests below.
Rules:
- agreement: facts most sources state — {"text": <=24 words, "citations": [digest indexes]}.
- disagreements: real differences — {"topic": <=8 words, "positions": [{"label",
  "summary", "citations", "quote"}]}. A position's "label" describes the view in a few
  words ("says the tax will raise revenue") — never an outlet name, a political
  leaning or a side ("the left", "right-wing media"). "summary" <=24 words; "quote"
  must be copied word for word from that cited source's article, at most 15 words.
- different_angles: distinct emphases only some outlets add — {"text", "citations"}.
- Every number must appear in a cited source's own text.
- If the sources agree, return an empty disagreements list; never invent balance.
Reply with JSON only, no prose:
{"agreement": [...], "disagreements": [...], "different_angles": [...]}"""

SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "agreement": {"type": "array", "items": {"type": "object"}},
        "disagreements": {"type": "array", "items": {"type": "object"}},
        "different_angles": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["agreement", "disagreements", "different_angles"],
    "additionalProperties": False,
}

DIGEST_ASK = "Write the JSON digest."
SYNTH_ASK = "Write the JSON comparison."


def digest_messages(
    source: SourceDoc,
    central_claim: str,
    *,
    previous: str | None = None,
    error: str | None = None,
) -> list[dict[str, str]]:
    """One source plus the story's central claim; a retry adds the rejected answer and
    the validation error as feedback."""
    messages = [
        {"role": "system", "content": DIGEST_SYSTEM},
        {
            "role": "user",
            "content": (
                f"The story's central claim: {central_claim}\n\n"
                f"Sources:\n\n{format_sources([source])}\n\n{DIGEST_ASK}"
            ),
        },
    ]
    if error is not None:
        messages += [
            {"role": "assistant", "content": previous or ""},
            {
                "role": "user",
                "content": f"That answer was rejected: {error}. "
                "Reply again with corrected JSON only, following every rule.",
            },
        ]
    return messages


def format_digests(digests: Sequence[dict]) -> str:
    """The numbered digest list sent to the synthesis prompt ([1] is index 1)."""
    out = []
    for n, d in enumerate(digests, 1):
        claims = "\n".join(f'- {c["text"]} ("{c["quote"]}")' for c in d["digest"]["claims"])
        out.append(
            f"[{n}] Source: {d['label']}\nStance: {d['digest']['stance']}\n"
            f"Kind: {d['digest']['kind']}\nFraming: {d['digest']['framing']}\n"
            f"Claims:\n{claims or '- (none)'}"
        )
    return "\n\n".join(out)


def synthesis_messages(
    title: str,
    central_claim: str,
    digests: Sequence[dict],
    *,
    previous: str | None = None,
    error: str | None = None,
) -> list[dict[str, str]]:
    messages = [
        {"role": "system", "content": SYNTH_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Story: {title}\nThe story's central claim: {central_claim}\n\n"
                f"Digests:\n\n{format_digests(digests)}\n\n{SYNTH_ASK}"
            ),
        },
    ]
    if error is not None:
        messages += [
            {"role": "assistant", "content": previous or ""},
            {
                "role": "user",
                "content": f"That answer was rejected: {error}. "
                "Reply again with corrected JSON only, following every rule.",
            },
        ]
    return messages
