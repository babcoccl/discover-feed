"""Startup check of the summarizer endpoint: ``GET {base_url}/models`` (a 401 still proves
the server is up; generation will show the auth error)."""

import logging
from dataclasses import dataclass

import httpx

from app.config import LLMRole
from app.llm.client import _api_key, build_headers, redact

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EndpointHealth:
    state: str
    """``ok`` | ``unset`` | ``unreachable`` | ``unauthorized`` | ``error``."""
    detail: str = ""
    host: str = ""


async def check_endpoint(
    role: LLMRole | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 5,
) -> EndpointHealth:
    if role is None:
        return EndpointHealth("unset", "no summarizer endpoint is configured")
    base = str(role.base_url).rstrip("/")
    host = httpx.URL(base).netloc.decode()
    key = _api_key(role)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            response = await client.get(f"{base}/models", headers=build_headers(role))
    except httpx.HTTPError as exc:
        health = EndpointHealth("unreachable", redact(f"{type(exc).__name__}: {exc}", key), host)
        logger.warning("LLM endpoint %s unreachable: %s", host, health.detail)
        return health
    if response.status_code == 401:
        logger.info("LLM endpoint %s is up (GET /models: HTTP 401, check the API key)", host)
        return EndpointHealth("unauthorized", "HTTP 401 from /models", host)
    if response.status_code >= 400:
        logger.info("LLM endpoint %s is up (GET /models: HTTP %s)", host, response.status_code)
        return EndpointHealth("ok", f"HTTP {response.status_code} from /models", host)
    try:
        models = [m.get("id") for m in response.json().get("data", [])]
    except (ValueError, AttributeError):
        models = []
    logger.info("LLM endpoint %s is up; models: %s", host, ", ".join(map(str, models)) or "?")
    return EndpointHealth("ok", ", ".join(map(str, models)), host)
