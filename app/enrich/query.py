"""Search queries from a story's headline (never its article text, which stays internal)."""

import json
import logging
import re
from collections import Counter

from app.cluster.tokens import STOPWORDS
from app.config import LLMRole

logger = logging.getLogger(__name__)

MAX_TOKENS = 10
MAX_LLM_QUERIES = 2
MAX_LLM_WORDS = 8
_WORD = re.compile(r"[^\W_]+(?:['\u2019.&-][^\W_]+)*", re.UNICODE)
_EXTRA_STOP = frozenset({"breaking", "live", "update", "updates", "exclusive", "watch", "video"})

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": MAX_LLM_QUERIES,
        }
    },
    "required": ["queries"],
    "additionalProperties": False,
}


def _is_stop(raw: str) -> bool:
    low = raw.lower().replace("\u2019", "'")
    return low in STOPWORDS or low.removesuffix("'s") in STOPWORDS or low in _EXTRA_STOP


def _clean(raw: str) -> str:
    return raw.replace("\u2019", "'").removesuffix("'s").strip("'.-&")


def heuristic_query(headlines: list[str]) -> str:
    """The first headline minus stopwords, then the entities (capitalized words) the other
    member headlines repeat most; at most ``MAX_TOKENS`` tokens."""
    if not headlines:
        return ""
    main = [_clean(w) for w in _WORD.findall(headlines[0]) if not _is_stop(w)]
    main = [w for w in main if len(w) > 1 or w.isdigit()]
    seen = {w.casefold() for w in main}
    entities: Counter[str] = Counter()
    for title in headlines[1:]:
        for raw in _WORD.findall(title):
            word = _clean(raw)
            if raw[:1].isupper() and not _is_stop(raw) and len(word) > 1:
                entities[word] += 1
    tokens = list(dict.fromkeys(main))
    for word, _ in entities.most_common():
        if word.casefold() not in seen:
            tokens.append(word)
            seen.add(word.casefold())
    return " ".join(tokens[:MAX_TOKENS])


def _llm_messages(headlines: list[str]) -> list[dict]:
    listed = "\n".join(f"- {h}" for h in headlines[:5])
    return [
        {
            "role": "system",
            "content": (
                "You write web news search queries. Reply with JSON "
                '{"queries": [...]}: one or two queries of at most 8 words each that would '
                "find other outlets' coverage of the same news event. Use names, places and "
                "the key action from the headlines only; no quotes, operators or dates."
            ),
        },
        {"role": "user", "content": f"Headlines:\n{listed}"},
    ]


def parse_llm_queries(content: str) -> list[str]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    data = json.loads(text)
    raw = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError("no queries list")
    queries = []
    for item in raw[:MAX_LLM_QUERIES]:
        if not isinstance(item, str):
            continue
        words = re.sub(r"[\"“”]", "", item).split()
        if words:
            queries.append(" ".join(words[:MAX_LLM_WORDS]))
    if not queries:
        raise ValueError("empty queries")
    return queries


async def llm_queries(role: LLMRole, headlines: list[str], transport=None) -> list[str]:
    """Up to two short queries from the summarizer model (thinking off); raises on failure."""
    from app.llm.client import LLMClient

    role = role.model_copy(update={"disable_thinking": True, "max_tokens": 200})
    client = LLMClient(role, transport=transport)
    result = await client.chat(
        _llm_messages(headlines), json_schema=QUERY_SCHEMA, schema_name="search_queries"
    )
    return parse_llm_queries(result.content)


async def build_queries(
    headlines: list[str], *, mode: str = "heuristic", role: LLMRole | None = None, transport=None
) -> tuple[list[str], str]:
    """``(queries, how)`` where ``how`` is ``heuristic`` | ``llm`` | ``llm_fallback``."""
    heuristic = heuristic_query(headlines)
    if mode != "llm" or role is None:
        return ([heuristic] if heuristic else []), "heuristic"
    try:
        return await llm_queries(role, headlines, transport), "llm"
    except Exception as exc:  # any LLM failure: fall back, never block enrichment
        logger.info("LLM search query failed (%s); using the heuristic query", exc)
        return ([heuristic] if heuristic else []), "llm_fallback"
