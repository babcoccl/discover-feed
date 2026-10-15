"""Search queries from a story's headline (never its article text, which stays internal)."""

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass

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


# --- per-provider queries -----------------------------------------------------------------

STRICT_TERMS = {"gdelt": 4, "guardian": 4, "newsdata": 5, "searxng": 6, "brave": 6}
"""Terms in the first query. GDELT and Guardian AND every term, so a long query finds
nothing; 3-5 high-signal terms is the sweet spot."""
LOOSE_TERMS = 4
QUERY_CHARS = {"newsdata": 100}
MIN_TERM_CHARS = {"gdelt": 3}
"""GDELT rejects keywords shorter than 3 characters ("too short")."""
OR_SYNTAX = frozenset({"gdelt", "guardian", "newsdata"})
_NUMBER = re.compile(r"^\d[\d,.]*$")


@dataclass
class Term:
    text: str
    score: float
    pos: int

    @property
    def query(self) -> str:
        return f'"{self.text}"' if " " in self.text else self.text


def _title_case(words: list[str]) -> bool:
    content = [w for w in words if w[:1].isalpha() and not _is_stop(w)]
    return bool(content) and sum(w[:1].isupper() for w in content) / len(content) > 0.6


def _possessive(raw: str) -> bool:
    return raw.lower().replace("\u2019", "'").endswith("'s")


def _runs(words: list[str]) -> list[list[str]]:
    """Runs of capitalized, non-stop words (entity names); a possessive ends a run."""
    runs, run = [], []
    for raw in words:
        if raw[:1].isupper() and not _is_stop(raw):
            run.append(_clean(raw))
            if _possessive(raw):
                runs.append(run)
                run = []
            continue
        if run:
            runs.append(run)
        run = []
    if run:
        runs.append(run)
    return runs


def story_terms(headlines: list[str]) -> list[Term]:
    """High-signal terms of a story, best first: quoted multi-word entity names, other
    names, numbers, then distinctive (long) words. Terms the other member headlines repeat
    score higher; title-case headlines only count multi-word names seen twice."""
    if not headlines:
        return []
    others = [h.casefold() for h in headlines[1:]]
    tokenized = [_WORD.findall(h) for h in headlines]
    phrases: Counter[str] = Counter()
    for words in tokenized:
        title = _title_case(words)
        for run in _runs(words):
            for size in (3, 2):
                for i in range(len(run) - size + 1):
                    phrases[" ".join(run[i : i + size])] += 2 if not title else 1
    phrases = Counter({p: n for p, n in phrases.items() if n >= 2})
    main = tokenized[0]
    main_title = _title_case(main)
    terms: list[Term] = []
    used: set[int] = set()
    joined = [_clean(w) for w in main]
    for size in (3, 2):
        for i in range(len(joined) - size + 1):
            if used & set(range(i, i + size)):
                continue
            text = " ".join(joined[i : i + size])
            broken = any(_possessive(main[j]) for j in range(i, i + size - 1))
            if text in phrases and not broken:
                rep = sum(text.casefold() in o for o in others)
                terms.append(Term(text, 4.0 + rep, i))
                used |= set(range(i, i + size))
    for i, raw in enumerate(main):
        word = _clean(raw)
        if i in used or _is_stop(raw) or (len(word) < 2 and not word.isdigit()):
            continue
        rep = sum(word.casefold() in o for o in others)
        if _NUMBER.match(word):
            score = 2.5
        elif raw[:1].isupper() and not main_title and i > 0:
            score = 3.0
        elif raw[:1].isupper() and main_title and rep:
            score = 2.0
        else:
            score = 1.5 if len(word) >= 7 else 1.0
        terms.append(Term(word, score + 0.75 * rep, i))
    seen = {t.text.casefold() for t in terms}
    extra: Counter[str] = Counter()
    for words in tokenized[1:]:
        for raw in words:
            word = _clean(raw)
            if raw[:1].isupper() and not _is_stop(raw) and len(word) > 2:
                extra[word] += 1
    for word, count in extra.most_common():
        if (
            count >= 2
            and word.casefold() not in seen
            and not any(word.casefold() in t.text.casefold().split() for t in terms)
        ):
            terms.append(Term(word, 2.0 + 0.5 * count, 1000 + len(terms)))
            seen.add(word.casefold())
    unique: dict[str, Term] = {}
    for term in terms:
        unique.setdefault(term.text.casefold(), term)
    return sorted(unique.values(), key=lambda t: (-t.score, t.pos))


def _usable(provider: str, terms: list[Term]) -> list[Term]:
    least = MIN_TERM_CHARS.get(provider, 1)
    return [t for t in terms if len(t.text) >= least]


def _fit(provider: str, query: str, parts: list[str]) -> str:
    limit = QUERY_CHARS.get(provider)
    while limit and len(query) > limit and len(parts) > 1:
        parts = parts[:-1]
        query = " ".join(parts)
    return query[:limit] if limit else query


def strict_query(provider: str, terms: list[Term]) -> str:
    """The best terms, all required (in headline order, which reads naturally)."""
    best = _usable(provider, terms)[: STRICT_TERMS.get(provider, 6)]
    parts = [t.query for t in sorted(best, key=lambda t: t.pos)]
    ranked = [t.query for t in best]
    query = " ".join(parts)
    limit = QUERY_CHARS.get(provider)
    while limit and len(query) > limit and len(ranked) > 1:
        ranked = ranked[:-1]
        query = " ".join(p for p in parts if p in ranked)
    return query[:limit] if limit else query


def loose_query(provider: str, terms: list[Term]) -> str:
    """Fewer required terms: the best term AND any of the next few (``(a OR b)``)."""
    usable = _usable(provider, terms)
    if len(usable) < 2:
        return ""
    if provider not in OR_SYNTAX:
        return " ".join(t.query for t in usable[:3])
    head, rest = usable[0], [t.query for t in usable[1:LOOSE_TERMS]]
    while rest:
        group = rest[0] if len(rest) == 1 else "(" + " OR ".join(rest) + ")"
        query = f"{head.query} {group}"
        if len(query) <= QUERY_CHARS.get(provider, 10**6):
            return query
        rest = rest[:-1]
    return head.query


def adapt_query(provider: str, query: str) -> str:
    """An LLM (or hand-written) query within the provider's limits: at most the strict
    term count, no too-short words, no quotes (they are added for multi-word names only)."""
    words = [w for w in re.sub(r"[\"\u201c\u201d()]", " ", query).split()]
    words = [w for w in words if w.upper() not in {"AND", "OR", "NOT"}]
    words = [w for w in words if len(w) >= MIN_TERM_CHARS.get(provider, 1)]
    words = words[: STRICT_TERMS.get(provider, 6)]
    return _fit(provider, " ".join(words), words)


def provider_queries(
    provider: str,
    headlines: list[str],
    *,
    llm: list[str] | None = None,
    max_queries: int = 2,
) -> list[str]:
    """Queries for one provider, in order: the strict one first, then looser fallbacks
    (the caller only sends a fallback when the previous query found nothing)."""
    terms = story_terms(headlines)
    queries = [adapt_query(provider, q) for q in llm or []]
    if not queries:
        queries.append(strict_query(provider, terms))
    queries.append(loose_query(provider, terms))
    return [q for q in dict.fromkeys(q for q in queries if q.strip())][:max_queries]


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
