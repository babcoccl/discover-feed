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

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Sequence

import httpx

from app.summarize.prompt import ATTRIBUTED_PROMPTS, PROMPTS, VERBATIM_SHINGLE, parse_sources
from app.summarize.validate import copied_run, overlap, split_sentences, tokens

CHAT_MODES = (
    "stream_error",
    "stall",
    "invalid_citation",
    "injection_echo",
)
"""Chat-only modes (``chat_mode=`` or the call's mode): ``stream_error`` drops the stream
after a few tokens, ``stall`` stops sending (the idle timeout fires), ``invalid_citation``
cites a source that does not exist, ``injection_echo`` obeys an instruction hidden in a source
(a deliberately bad model, to test the UI and verification). ``empty``, ``invented_number``,
``http_500``, ``timeout`` and ``http_401`` apply to chat too."""
PERSPECTIVES_MODES = (
    "bad_quote",
    "political_label",
    "one_sided_dispute",
)
"""Perspectives-only modes: ``bad_quote`` emits a quote absent from the article text
(rejected by the verbatim check), ``political_label`` makes the synthesis label a position
with an outlet-leaning label (rejected), ``one_sided_dispute`` makes a disagreement whose
positions all cite the same source (omitted, never invented balance)."""
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
    "truncated",
    "reasoning_only",
    "http_401",
    "demo",
    *CHAT_MODES,
    *PERSPECTIVES_MODES,
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
    passes the verbatim guards (no long runs, no copied 8-word phrases)."""
    words = text.split()
    for n in range(80):
        run = copied_run(" ".join(words), grams_sources, VERBATIM_SHINGLE)
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
            for k in range(len(spans) - VERBATIM_SHINGLE + 1)
            if spans[k][0] == first
            and " ".join(t for t, _ in spans[k : k + VERBATIM_SHINGLE]) == run
        ]
        cut = spans[starts[0] + VERBATIM_SHINGLE // 2][1] if starts else len(words) // 2
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
    while words() < 360 and any(p[2] for p in pools) and rounds < 50:
        rounds += 1
        for n in range(len(pools)):
            if words() >= 360:
                break
            added = take(n, 1)
            if added:
                details += added
                detail_cites.append(pools[n][0])
    paragraphs.append(
        {"text": " ".join(details), "citations": sorted(set(detail_cites)) or [pools[first][0]]}
    )
    for filler in _FILLERS:
        if words() >= 340:
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
            for p in paragraphs[:4]
        ]
    return {"paragraphs": paragraphs}


_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{3,}")
_QUESTION_STOP = frozenset(
    "what when where which with about after does have there their they this that from will "  # noqa: SIM905
    "would could should were been into more most other some such than then them these those "
    "said says tell know sources source story article happen happened happens next".split()
)


def _keywords(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)} - _QUESTION_STOP


def fake_chat_answer(system: str, question: str, mode: str = "ok") -> str:
    """Up to two source sentences that share words with the question, each cited; a plain
    "the sources don't say" when none does."""
    from app.chat.prompt import parse_chat_sources

    sources = parse_chat_sources(system)
    wanted = _keywords(question)
    scored = []
    for index, _label, headline, text in sources:
        for sentence in _body_sentences(f"{headline}.\n{text}", 30):
            score = len(wanted & _keywords(sentence))
            if score:
                scored.append((-score, index, sentence))
    scored.sort()
    picked, used = [], set()
    for _score, index, sentence in scored:
        if sentence in used:
            continue
        used.add(sentence)
        picked.append(f"{sentence.rstrip('.')} [{index}].")
        if len(picked) == 2:
            break
    if mode == "injection_echo":
        for _index, _label, _headline, text in sources:
            if m := re.search(r"say (?:only )?[\"']([^\"']+)[\"']", text, re.I):
                return m.group(1)
    if not picked:
        return "The sources don't say. They cover " + (
            f'"{sources[0][2]}" [1].' if sources else "nothing on that."
        )
    answer = " ".join(picked)
    k = len(sources)
    if mode == "invalid_citation":
        answer += f" More details are in another report [{k + 3}]."
    elif mode == "invented_number":
        answer += " " + INVENTED.rstrip(".") + " [1]."
    return answer


def fake_digest(user_message: str, *, mode: str = "ok") -> dict:
    """A per-source perspectives digest: claims carry verbatim quotes from the source's
    text; stance is read from cue words in it (``confirms``/``supports`` -> ``supports``,
    ``disputes``/``denies``/``rejects``/``contradicts`` -> ``disputes``, else ``neutral``)."""
    sources = parse_sources(user_message)
    text = sources[0][3] if sources else ""
    headline = sources[0][2] if sources else ""
    sentences = _body_sentences(text, max_words=15)[:3]
    claims = []
    for sentence in sentences:
        quote = " ".join(sentence.rstrip('.,;:!?"\u201d').split()[:15])
        if mode == "bad_quote":
            quote = "this passage never appeared in the article text"
        claims.append({"text": sentence[:200], "quote": quote})
    if not claims:
        claims = [
            {
                "text": headline or "The story reports the event.",
                "quote": " ".join((headline or "").split()[:15]),
            }
        ]
    lowered = text.lower()
    stance = "neutral"
    if any(w in lowered for w in ("disputes", "denies", "rejects", "contradicts", "disputed")):
        stance = "disputes"
    elif any(w in lowered for w in ("confirms", "confirmed", "supports", "corroborates")):
        stance = "supports"
    return {
        "claims": claims,
        "stance": stance,
        "kind": "reporting",
        "key_parties": [],
        "framing": " ".join(headline.split()[:12]),
    }


_DIGEST_BLOCK = re.compile(
    r"\[(\d+)\] Source: (?P<label>.+?)\nStance: (?P<stance>\w+)\nKind: (?P<kind>[\w-]+)\n"
    r"Framing: (?P<framing>.*?)\nClaims:\n(?P<claims>.*?)(?=\n\n\[\d+\] Source: |\n\nWrite |\Z)",
    re.S,
)

_STANCE_LABELS = {
    "supports": "reports the claim as established",
    "disputes": "challenges the central claim",
    "adds": "adds details without disputing",
    "neutral": "reports the claim without judgement",
    "unclear": "does not take a position",
}


def fake_perspectives(user_message: str, *, mode: str = "ok") -> dict:
    """A 'how outlets differ' synthesis over the numbered digests in the prompt: agreement
    across all digests plus one disagreement when the digests' stances differ."""
    blocks = []
    for m in _DIGEST_BLOCK.finditer(user_message):
        quote = ""
        claim_line = next((ln for ln in m["claims"].splitlines() if ln.strip().startswith("-")), "")
        if '"' in claim_line:
            quote = claim_line.rsplit('"', 1)[0].split('"', 1)[-1] if '"' in claim_line else ""
        blocks.append(
            {"index": int(m[1]), "label": m["label"].strip(), "stance": m["stance"], "quote": quote}
        )
    agreement = []
    if blocks:
        agreement = [
            {
                "text": "The sources describe the same event and its main facts.",
                "citations": [b["index"] for b in blocks],
            }
        ]
    disagreements = []
    stances = {b["stance"] for b in blocks}
    # a real disagreement needs an opposing stance — "disputes" — not just variety
    if "disputes" in stances and len(stances) >= 2:
        picked = []
        for stance in ("disputes", "supports", "adds", "neutral", "unclear"):
            for b in blocks:
                if b["stance"] == stance and b["index"] not in [p["citations"][0] for p in picked]:
                    label = _STANCE_LABELS.get(stance, f"describes it as {stance}")
                    if mode == "political_label" and len(picked) == 0:
                        label = "right-leaning outlets"
                    picked.append(
                        {
                            "label": label,
                            "summary": f"This source's coverage {stance} the central claim.",
                            "citations": [b["index"]],
                            "quote": b["quote"] or "no quote",
                        }
                    )
                    break
            if len(picked) >= 2:
                break
        if mode == "one_sided_dispute" and len(picked) >= 2:
            picked[1]["citations"] = picked[0]["citations"]
        if len(picked) >= 2:
            disagreements = [{"topic": "stance on the story", "positions": picked}]
    return {
        "agreement": agreement,
        "disagreements": disagreements,
        "different_angles": [],
    }


def fake_followups(user_message: str) -> dict:
    sources = parse_sources(user_message)
    headline = sources[0][2] if sources else "this story"
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]+", headline) if len(w) > 2][:3]
    topic = " ".join(words) or "this story"
    return {
        "questions": [
            f"What happens next with {topic}?",
            f"Who is affected by {topic}?",
            f"How did the sources describe {topic}?",
        ]
    }


class _ChatStream(httpx.AsyncByteStream):
    """``text/event-stream`` chunks like llama-server's, with optional per-token delay and
    failure modes. Records an early close (the client cancelled) on the fake."""

    def __init__(self, fake: "FakeLLM", text: str, mode: str, prompt_tokens: int) -> None:
        self.fake, self.text, self.mode, self.prompt_tokens = fake, text, mode, prompt_tokens
        self.finished = False

    def _chunk(self, data: dict) -> bytes:
        return f"data: {json.dumps(data)}\n\n".encode()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        pieces = re.findall(r"\S+\s*", self.text)
        base = {"id": "fake-stream", "object": "chat.completion.chunk", "model": self.fake.model}
        yield self._chunk({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}}]})
        for n, piece in enumerate(pieces):
            if self.mode == "stream_error" and n == 4:
                raise httpx.RemoteProtocolError("fake: upstream closed mid-stream")
            if self.mode == "stall" and n == 3:
                await asyncio.sleep(3600)
            if self.fake.token_delay:
                await asyncio.sleep(self.fake.token_delay)
            self.fake.streamed_tokens += 1
            yield self._chunk({**base, "choices": [{"index": 0, "delta": {"content": piece}}]})
        usage = {"prompt_tokens": self.prompt_tokens, "completion_tokens": len(pieces)}
        yield self._chunk(
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": usage,
            }
        )
        yield b"data: [DONE]\n\n"
        self.finished = True

    async def aclose(self) -> None:
        if not self.finished:
            self.fake.cancelled_streams += 1


class FakeLLM:
    def __init__(
        self,
        mode: str | Sequence[str] = "ok",
        *,
        model: str = FAKE_MODEL,
        delay: float = 0,
        chat_mode: str | None = None,
        token_delay: float = 0,
    ) -> None:
        modes = [mode] if isinstance(mode, str) else list(mode)
        unknown = set(modes + ([chat_mode] if chat_mode else [])) - set(MODES)
        if unknown or not modes:
            raise ValueError(f"unknown fake LLM mode(s) {sorted(unknown)}; choose from {MODES}")
        self.modes = modes
        self.model = model
        self.delay = delay
        self.chat_mode = chat_mode
        """Overrides the mode for chat requests (the demo's ``--fake-chat-mode``)."""
        self.token_delay = token_delay
        """Seconds between streamed chat tokens."""
        self.requests: list[dict] = []
        self.cancelled_streams = 0
        """Chat streams the client closed before the end (upstream cancellation)."""
        self.streamed_tokens = 0
        self._demo_report_failures: dict[str, int] = {}

    @property
    def calls(self) -> int:
        return len(self.requests)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def kind_of(body: dict) -> str:
        from app.chat.prompt import CHAT_SYSTEM_PROMPT

        system = next(
            (m["content"] for m in body.get("messages", []) if m.get("role") == "system"), ""
        )
        if system.startswith(CHAT_SYSTEM_PROMPT):
            return "chat"
        from app.perspectives.prompt import DIGEST_SYSTEM, SYNTH_SYSTEM

        if system == DIGEST_SYSTEM:
            return "perspectives_digest"
        if system == SYNTH_SYSTEM:
            return "perspectives"
        if system == PROMPTS["followups"][0]:
            return "followups"
        for kind in ("report", "brief"):
            if system in (PROMPTS[kind][0], ATTRIBUTED_PROMPTS[kind]):
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
        if request.method == "GET":  # GET /models (the startup health check)
            return httpx.Response(
                200, json={"object": "list", "data": [{"id": self.model, "object": "model"}]}
            )
        body = json.loads(request.content or b"{}")
        if self.delay and self.kind_of(body) != "chat":
            await asyncio.sleep(self.delay)
        self.requests.append(body)
        mode = self.modes[min(len(self.requests), len(self.modes)) - 1]
        user = next((m["content"] for m in body.get("messages", []) if m.get("role") == "user"), "")
        kind = self.kind_of(body)
        sources = parse_sources(user)
        k = len(sources)
        if kind == "chat":
            return self._chat(body, self.chat_mode or mode, request)
        if mode == "demo":
            mode = self._demo_mode(kind, sources[0][2] if sources else "")
        if mode == "http_500":
            return httpx.Response(500, json={"error": {"message": "fake server error"}})
        if mode == "http_401":
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})
        if mode == "timeout":
            raise httpx.ReadTimeout("fake timeout", request=request)
        if kind == "perspectives_digest":
            answer = fake_digest(user, mode=mode)
            points = None
        elif kind == "perspectives":
            answer = fake_perspectives(user, mode=mode)
            points = None
        elif kind == "followups":
            answer = fake_followups(user)
            points = None
            if mode == "duplicate_bullets":
                answer["questions"][1] = answer["questions"][0]
        elif kind == "report":
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
        if kind in ("followups", "perspectives_digest", "perspectives"):
            pass
        elif mode == "out_of_range":
            if points is None:
                answer["citations"][-1] = [k + 1]
            else:
                points[-1]["citations"] = [k + 1]
        elif mode == "invented_number":
            if points is None and "summary" in answer:
                answer["summary"] += " " + INVENTED
                answer["citations"].append([1])
            elif points is not None:
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
        finish, reasoning = "stop", None
        if mode == "truncated":  # hit max_tokens mid-answer
            content, finish = content[: len(content) // 3], "length"
        elif mode == "reasoning_only":  # thinking used the whole budget
            content, finish, reasoning = "", "length", "Let me think step by step. " * 40
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
                        "message": {
                            "role": "assistant",
                            "content": content,
                            **({"reasoning_content": reasoning} if reasoning else {}),
                        },
                        "finish_reason": finish,
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": len(content.split()),
                    "total_tokens": prompt_tokens + len(content.split()),
                },
            },
        )

    def _chat(self, body: dict, mode: str, request: httpx.Request) -> httpx.Response:
        messages = body.get("messages", [])
        system = messages[0]["content"] if messages else ""
        question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if mode == "http_500":
            return httpx.Response(500, json={"error": {"message": "fake server error"}})
        if mode == "http_401":
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})
        if mode == "timeout":
            raise httpx.ReadTimeout("fake timeout", request=request)
        text = "" if mode == "empty" else fake_chat_answer(system, question, mode)
        prompt_tokens = sum(len(str(m.get("content", "")).split()) for m in messages)
        if not body.get("stream"):
            return httpx.Response(
                200,
                json={
                    "model": self.model,
                    "choices": [
                        {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": len(text.split()),
                    },
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_ChatStream(self, text, mode, prompt_tokens),
        )
