"""Deterministic in-process OpenAI-compatible fake LLM (tests and the demo; no network).

``FakeLLM(mode).transport()`` answers ``POST .../chat/completions`` like llama-server would,
writing a brief, report or (Phase 4) summary from the numbered sources in the prompt; the
kind is recognised from the system prompt. Modes simulate failures: ``ok``, ``prose`` (valid
JSON wrapped in prose), ``malformed``, ``out_of_range``, ``invented_number``, ``empty``,
``http_500``, ``timeout``; briefs: ``wrong_bullet_count``, ``duplicate_bullets``; reports:
``copied_report`` (a source sentence copied verbatim), ``short_report``. A mode that does not
apply to the requested kind answers correctly. Pass a list to change mode per call (the last
one repeats), e.g. ``["malformed", "ok"]`` to pass on the retry. ``demo`` is ``ok`` except
that about one brief in five gets an invented number (so the demo shows fallbacks) and one
story's report fails validation the first time (so the demo shows Retry).
"""

import hashlib
import json
from collections.abc import Sequence

import httpx

from app.summarize.prompt import PROMPTS, VERBATIM_RUN, parse_sources
from app.summarize.validate import copied_run, overlap, split_sentences, tokens

MODES = (
    "ok",
    "prose",
    "malformed",
    "out_of_range",
    "invented_number",
    "empty",
    "http_500",
    "timeout",
    "wrong_bullet_count",
    "duplicate_bullets",
    "copied_report",
    "short_report",
    "demo",
)
FAKE_MODEL = "fake-llm"
INVENTED = "Officials said 987654 people were affected."


def _body_sentences(text: str, max_words: int = 24) -> list[str]:
    """Sentences of the real paragraphs (skips headline-like lines without a period), cut to
    ``max_words``."""
    paragraphs = [p for p in text.splitlines() if p.strip()]
    body = [p for p in paragraphs if p.rstrip()[-1:] in '.!?"\u201d'] or paragraphs
    out = []
    for sentence in split_sentences(" ".join(" ".join(body).split())):
        words = sentence.split()[:max_words]
        if len(words) >= 4:
            text = " ".join(words).rstrip(".,;:!?") + "."
            out.append(text[0].upper() + text[1:])
    return out


def _sentence(text: str, max_words: int = 24) -> str:
    found = _body_sentences(text, max_words)
    return found[0] if found else ""


def fake_summary(user_message: str) -> dict:
    sentences: list[str] = []
    citations: list[list[int]] = []
    for index, _source, headline, text in parse_sources(user_message)[:2]:
        sentence = _sentence(text) or _sentence(headline + ".")
        if sentence:
            sentences.append(sentence)
            citations.append([index])
    if not sentences:
        sentences, citations = ["No details were provided."], [[1]]
    return {"summary": " ".join(sentences), "citations": citations}


def fake_brief(user_message: str) -> dict:
    sources = parse_sources(user_message)
    per_source = [(i, _body_sentences(text, 22)) for i, _s, _h, text in sources]
    lead_index, lead = 1, "No details were provided."
    for index, sentences in per_source:
        if sentences:
            lead_index, lead = index, sentences[0]
            break
    bullets: list[dict] = []
    depth = max((len(s) for _, s in per_source), default=0)
    candidates = [
        (index, sentences[n])
        for n in range(depth)
        for index, sentences in per_source[1:] + per_source[:1]
        if n < len(sentences)
    ]
    for index, sentence in candidates:
        if len(bullets) == 3:
            break
        if all(overlap(sentence, t) < 0.4 for t in [lead, *(b["text"] for b in bullets)]):
            bullets.append({"text": sentence, "citations": [index]})
    fillers = [
        "Coverage so far comes from the outlets listed below.",
        "Further details were not provided in the coverage.",
        "The sources did not say what happens next.",
    ]
    while len(bullets) < 3:
        text = fillers[len(bullets)]
        bullets.append({"text": text, "citations": [1]})
    return {"lead": lead, "lead_citations": [lead_index], "bullets": bullets}


_SYNONYMS = {
    "said": "stated", "says": "states", "announced": "unveiled", "announces": "unveils",
    "company": "firm", "also": "additionally", "after": "following", "about": "roughly",
    "because": "since", "but": "though", "including": "such as", "began": "started",
    "expected": "anticipated", "customers": "users", "officials": "authorities",
    "reported": "noted", "largest": "biggest", "biggest": "largest", "showed": "indicated",
    "shows": "indicates", "help": "assist", "major": "significant", "earlier": "previously",
}  # fmt: skip
_BREAKERS = ("reportedly", "notably")
_FILLERS = (
    "Coverage so far is limited to the outlets cited here, and none of them describes "
    "independent confirmation beyond its own reporting.",
    "Readers should treat early details as provisional until the people involved publish "
    "fuller accounts or respond to questions from reporters.",
    "Where the outlets differ, the differences are mostly in emphasis rather than in the "
    "underlying facts they describe.",
    "None of the sources offers a firm timeline for what comes next, so further updates "
    "are likely as the situation develops.",
    "Several questions remain open, including how the people directly affected will "
    "respond in the coming days.",
    "The available reporting focuses on the immediate facts and says little about the "
    "longer history behind them.",
    "Any reactions from outside groups, rivals or regulators were not described in detail "
    "by the coverage reviewed here.",
    "It is also unclear whether the people and organisations named have commented beyond "
    "the statements already quoted.",
)


def _paraphrase(sentence: str) -> str:
    """Swap common words for synonyms."""
    return " ".join(
        _SYNONYMS.get(w, w) if w.isalpha() and w.islower() else w for w in sentence.split()
    )


def _break_runs(text: str, grams_sources: Sequence[str]) -> str:
    """Break any run of 8 words still copied from a source with an adverb, so the report
    passes the verbatim-overlap guard."""
    words = text.split()
    for n in range(80):
        run = copied_run(" ".join(words), grams_sources, VERBATIM_RUN)
        if run is None:
            break
        first = run.split()[0]
        spans, position = [], 0
        for i, w in enumerate(words):
            for t in tokens(w):
                spans.append((t, i))
                position += 1
        starts = [
            k
            for k in range(len(spans) - VERBATIM_RUN + 1)
            if spans[k][0] == first and " ".join(t for t, _ in spans[k : k + VERBATIM_RUN]) == run
        ]
        cut = spans[starts[0] + VERBATIM_RUN // 2][1] if starts else len(words) // 2
        words.insert(cut, _BREAKERS[n % len(_BREAKERS)])
    return " ".join(words)


def fake_report(user_message: str, *, copy: bool = False, short: bool = False) -> dict:
    sources = parse_sources(user_message)
    texts = [f"{h}\n{t}" for _i, _s, h, t in sources]
    pools = [(i, name, _body_sentences(text, 40)) for i, name, _h, text in sources]
    if not pools:
        pools = [(1, "The source", ["No details were provided."])]

    def take(pool_index: int, count: int) -> list[str]:
        _i, _name, sentences = pools[pool_index]
        picked, pools[pool_index] = sentences[:count], (_i, _name, sentences[count:])
        return [_paraphrase(s) for s in picked]

    first, second = 0, 1 % len(pools)
    paragraphs = [
        {
            "text": " ".join([f"{pools[first][1]} reports the news.", *take(first, 2)]),
            "citations": [pools[first][0]],
        },
        {
            "text": " ".join(
                [f"For background, {pools[second][1]} offers context.", *take(second, 2)]
            ),
            "citations": [pools[second][0]],
        },
    ]
    details: list[str] = ["Other details and reactions vary by outlet."]
    detail_cites: list[int] = []
    closing = {"text": "It is not yet clear from the coverage what happens next.", "citations": [1]}

    def words() -> int:
        body = [p["text"] for p in paragraphs] + details + [closing["text"]]
        return len(" ".join(body).split())

    rounds = 0
    while words() < 300 and any(p[2] for p in pools) and rounds < 50:
        rounds += 1
        for n in range(len(pools)):
            if words() >= 300:
                break
            added = take(n, 1)
            if added:
                details += added
                detail_cites.append(pools[n][0])
    paragraphs.append(
        {"text": " ".join(details), "citations": sorted(set(detail_cites)) or [pools[first][0]]}
    )
    for filler in _FILLERS:
        if words() >= 280:
            break
        closing["text"] += " " + filler
    paragraphs.append(closing)
    for p in paragraphs:
        p["text"] = _break_runs(p["text"], texts)
    if copy:
        raw = " ".join(sources[0][3].split()[:14]) if sources else ""
        paragraphs[0]["text"] += " " + raw.rstrip(".") + "."
    if short:
        paragraphs = [
            {"text": p["text"].split(". ")[0].rstrip(".") + ".", "citations": p["citations"]}
            for p in paragraphs[:3]
        ]
    return {"paragraphs": paragraphs}


class FakeLLM:
    def __init__(
        self, mode: str | Sequence[str] = "ok", *, model: str = FAKE_MODEL, delay: float = 0
    ) -> None:
        modes = [mode] if isinstance(mode, str) else list(mode)
        unknown = set(modes) - set(MODES)
        if unknown or not modes:
            raise ValueError(f"unknown fake LLM mode(s) {sorted(unknown)}; choose from {MODES}")
        self.modes = modes
        self.model = model
        self.delay = delay
        self.requests: list[dict] = []
        self._demo_report_failures: dict[str, int] = {}

    @property
    def calls(self) -> int:
        return len(self.requests)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def kind_of(body: dict) -> str:
        system = next(
            (m["content"] for m in body.get("messages", []) if m.get("role") == "system"), ""
        )
        for kind in ("report", "brief"):
            if system == PROMPTS[kind][0]:
                return kind
        return "summary"

    def _demo_mode(self, kind: str, headline: str) -> str:
        digest = hashlib.sha256(headline.encode()).digest()
        if kind == "report":
            if digest[0] % 4 == 3:  # fails validation twice, then passes (Retry works)
                seen = self._demo_report_failures.get(headline, 0)
                self._demo_report_failures[headline] = seen + 1
                return "copied_report" if seen < 2 else "ok"
            return "ok"
        return "invented_number" if digest[0] % 5 == 0 else "ok"

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.delay:
            import asyncio

            await asyncio.sleep(self.delay)
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        mode = self.modes[min(len(self.requests), len(self.modes)) - 1]
        user = next((m["content"] for m in body.get("messages", []) if m.get("role") == "user"), "")
        kind = self.kind_of(body)
        sources = parse_sources(user)
        k = len(sources)
        if mode == "demo":
            mode = self._demo_mode(kind, sources[0][2] if sources else "")
        if mode == "http_500":
            return httpx.Response(500, json={"error": {"message": "fake server error"}})
        if mode == "timeout":
            raise httpx.ReadTimeout("fake timeout", request=request)
        if kind == "report":
            answer = fake_report(user, copy=mode == "copied_report", short=mode == "short_report")
            points = answer["paragraphs"]
        elif kind == "brief":
            answer = fake_brief(user)
            points = answer["bullets"]
            if mode == "wrong_bullet_count":
                answer["bullets"] = points = points[:2]
            elif mode == "duplicate_bullets":
                points[1] = dict(points[0])
        else:
            answer = fake_summary(user)
            points = None
        if mode == "out_of_range":
            if points is None:
                answer["citations"][-1] = [k + 1]
            else:
                points[-1]["citations"] = [k + 1]
        elif mode == "invented_number":
            if points is None:
                answer["summary"] += " " + INVENTED
                answer["citations"].append([1])
            else:
                points[-1]["text"] = points[-1]["text"].rstrip(".") + ". " + INVENTED
                if kind == "brief":
                    points[-1]["text"] = INVENTED
        content = json.dumps(answer)
        if mode == "prose":
            content = f"Sure! Here is the {kind}:\n```json\n{content}\n```\nHope this helps."
        elif mode == "malformed":
            content = content[: len(content) // 2]
        elif mode == "empty":
            content = ""
        prompt_tokens = sum(len(str(m.get("content", "")).split()) for m in body["messages"])
        return httpx.Response(
            200,
            json={
                "id": f"fake-{len(self.requests)}",
                "object": "chat.completion",
                "model": self.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": len(content.split()),
                    "total_tokens": prompt_tokens + len(content.split()),
                },
            },
        )
