"""OpenAI-compatible chat completions client (llama.cpp, vLLM, Ollama, OpenAI, ...)."""

from app.llm.client import (
    ChatResult,
    LLMBadResponse,
    LLMClient,
    LLMConnectionError,
    LLMError,
    LLMHTTPError,
    LLMTimeout,
    build_headers,
    build_payload,
    parse_response,
    redact,
)

__all__ = [
    "ChatResult",
    "LLMBadResponse",
    "LLMClient",
    "LLMConnectionError",
    "LLMError",
    "LLMHTTPError",
    "LLMTimeout",
    "build_headers",
    "build_payload",
    "parse_response",
    "redact",
]
