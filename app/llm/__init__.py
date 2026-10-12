"""OpenAI-compatible chat completions client (llama.cpp, vLLM, Ollama, OpenAI, ...)."""

from app.llm.client import (
    ChatResult,
    LLMBadResponse,
    LLMBusy,
    LLMClient,
    LLMConnectionError,
    LLMError,
    LLMHTTPError,
    LLMStreamError,
    LLMTimeout,
    StreamEvent,
    build_headers,
    build_payload,
    parse_response,
    redact,
)

__all__ = [
    "ChatResult",
    "LLMBadResponse",
    "LLMBusy",
    "LLMClient",
    "LLMConnectionError",
    "LLMError",
    "LLMHTTPError",
    "LLMStreamError",
    "LLMTimeout",
    "StreamEvent",
    "build_headers",
    "build_payload",
    "parse_response",
    "redact",
]
