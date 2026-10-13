"""Shared plumbing for JSON API sources: key lookup, budgets, rate limits, safe errors.

API keys come from the environment variable named by ``source.api_key`` and are sent in
headers only (never in URLs, so they cannot leak into httpx's request logs); every error
message is scrubbed of the key before it is raised.
"""

import json
import os
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.config import Source
from app.sources.base import FetchError, FetchState, Quota, RateLimited, SourceAdapter


class MissingApiKey(FetchError):
    pass


def api_key(source: Source) -> str:
    name = source.api_key
    if not name:
        raise MissingApiKey(f"source {source.id!r} has no api_key environment variable set")
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingApiKey(f"environment variable {name} is not set")
    return value


def scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def retry_after_seconds(response: httpx.Response, now: datetime | None = None) -> float | None:
    value = response.headers.get("Retry-After")
    if value:
        value = value.strip()
        if value.isdigit():
            return float(value)
        try:
            at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, (at - (now or datetime.now(UTC))).total_seconds())
    reset = response.headers.get("x-rate-limit-reset")  # X: epoch seconds
    if reset and reset.isdigit():
        return max(0.0, int(reset) - (now or datetime.now(UTC)).timestamp())
    return None


class JsonApiAdapter(SourceAdapter):
    provider = "API"

    def quota(self, state: FetchState | None) -> Quota:
        if state is not None and state.quota is not None:
            return state.quota
        return Quota()

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str],
        secret: str,
        quota: Quota,
    ) -> Any:
        quota.before_request()
        try:
            response = await self.client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise FetchError(
                scrub(f"{self.provider} request failed: {type(exc).__name__}: {exc}", secret)
            ) from None
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        detail = scrub(self.error_detail(body) or response.reason_phrase or "", secret)
        if response.status_code == 429:
            wait = retry_after_seconds(response)
            when = f"; retry after {int(wait)} s" if wait is not None else ""
            raise RateLimited(f"{self.provider} rate limit (HTTP 429){when}: {detail}", wait)
        if response.status_code in (401, 403) and not self.is_quota_error(body):
            raise FetchError(
                f"{self.provider} rejected the API key (HTTP {response.status_code}): {detail}"
            )
        if self.is_quota_error(body) or response.status_code == 402:
            raise FetchError(
                f"{self.provider} quota exhausted (HTTP {response.status_code}): {detail}"
            )
        if response.status_code >= 400:
            raise FetchError(f"{self.provider} HTTP {response.status_code}: {detail}")
        if body is None:
            raise FetchError(f"{self.provider} returned malformed JSON")
        return body

    def error_detail(self, body: Any) -> str | None:
        return None

    def is_quota_error(self, body: Any) -> bool:
        return False


def parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default
