"""Deterministic in-process OpenAI-compatible fake LLM (tests and the demo; no network).

``FakeLLM(mode).transport()`` answers ``POST .../chat/completions`` like llama-server would,
writing a summary from the numbered sources in the prompt. Modes simulate failures:
``ok``, ``prose`` (valid JSON wrapped in prose), ``malformed``, ``out_of_range``,
``invented_number``, ``empty``, ``http_500``, ``timeout``. Pass a list to change mode per call
(the last one repeats), e.g. ``["malformed", "ok"]`` to pass on the retry. ``demo`` is ``ok``
except that about one story in five gets an invented number (so the demo shows fallbacks).
"""

import hashlib
import json
from collections.abc import Sequence

import httpx

from app.summarize.prompt import parse_sources
from app.summarize.validate import split_sentences

MODES = (
    "ok",
    "prose",
    "malformed",
    "out_of_range",
    "invented_number",
    "empty",
    "http_500",
    "timeout",
    "demo",
)
FAKE_MODEL = "fake-llm"


def _sentence(text: str, max_words: int = 24) -> str:
    """First sentence of the first real paragraph (skips a headline line without a period)."""
    paragraphs = [p for p in text.splitlines() if p.strip()]
    body = next((p for p in paragraphs if p.rstrip()[-1:] in '.!?"\u201d'), "")
    body = body or " ".join(paragraphs)
    first = split_sentences(" ".join(body.split()))[0] if body.strip() else ""
    words = first.split()[:max_words]
    return " ".join(words).rstrip(".,;:!?") + "." if words else ""


def fake_summary(user_message: str) -> dict:
    sources = parse_sources(user_message)
    sentences: list[str] = []
    citations: list[list[int]] = []
    for index, _source, headline, text in sources[:2]:
        sentence = _sentence(text) or _sentence(headline)
        if sentence and sentence[0].isalpha():
            sentence = sentence[0].upper() + sentence[1:]
        if sentence:
            sentences.append(sentence)
            citations.append([index])
    if not sentences:
        sentences, citations = ["No details were provided."], [[1]]
    return {"summary": " ".join(sentences), "citations": citations}


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

    @property
    def calls(self) -> int:
        return len(self.requests)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.delay:
            import asyncio

            await asyncio.sleep(self.delay)
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        mode = self.modes[min(len(self.requests), len(self.modes)) - 1]
        user = next((m["content"] for m in body.get("messages", []) if m.get("role") == "user"), "")
        if mode == "demo":
            first = parse_sources(user)[:1]
            digest = hashlib.sha256((first[0][2] if first else "").encode()).digest()
            mode = "invented_number" if digest[0] % 5 == 0 else "ok"
        if mode == "http_500":
            return httpx.Response(500, json={"error": {"message": "fake server error"}})
        if mode == "timeout":
            raise httpx.ReadTimeout("fake timeout", request=request)
        answer = fake_summary(user)
        k = len(parse_sources(user))
        if mode == "out_of_range":
            answer["citations"][-1] = [k + 1]
        elif mode == "invented_number":
            answer["summary"] += " Officials said 987654 people were affected."
            answer["citations"].append([1])
        content = json.dumps(answer)
        if mode == "prose":
            content = f"Sure! Here is the summary:\n```json\n{content}\n```\nHope this helps."
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
