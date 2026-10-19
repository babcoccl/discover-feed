"""Fetch candidate pages for enrichment: the Extractor's politeness (robots.txt, per-domain
delay, size caps) plus the SSRF guard on every hop (http(s) only, public addresses only,
re-checked after each redirect and at connect time)."""

import asyncio
import re
from dataclasses import dataclass, field

import httpx

from app.extract import Extractor, _describe, _Rejected, domain_of, extract_text
from app.images.meta import page_images
from app.images.safety import Blocked, Guard, guarded_get_stream, guarded_transport
from app.page_meta import PageMeta, page_meta

_PAYWALL = re.compile(rb"""["']isAccessibleForFree["']\s*:\s*["']?(false|no)["']?""", re.IGNORECASE)


@dataclass
class FetchedPage:
    url: str
    status: str
    """``ok`` | ``failed`` | ``skipped`` (robots.txt) | ``blocked`` (SSRF) | ``paywalled``."""
    reason: str = ""
    text: str = ""
    words: int = 0
    image_url: str | None = None
    meta: PageMeta = field(default_factory=PageMeta)
    """Publication date / og:type / article:section (``app.page_meta``); empty when the
    page declares none."""


class CandidateFetcher(Extractor):
    def __init__(self, session_factory=None, *, guard: Guard | None = None, **kwargs) -> None:
        super().__init__(session_factory, **kwargs)
        self.guard = guard or Guard()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=False,
            transport=self.transport or guarded_transport(self.guard),
        )

    async def _get(
        self, client: httpx.AsyncClient, domain: str, url: str, *, html_only: bool = True
    ) -> bytes:
        await self._wait_turn(domain)
        try:
            resp = await guarded_get_stream(client, self.guard, url)
        except Blocked as exc:
            raise _Rejected(f"blocked: {exc.reason}") from None
        try:
            if resp.status_code >= 400:
                raise _Rejected(f"HTTP {resp.status_code}")
            length = resp.headers.get("content-length")
            if length and length.isdigit() and int(length) > self.max_bytes:
                raise _Rejected(f"larger than {self.max_bytes} bytes")
            ctype = resp.headers.get("content-type", "").lower()
            if html_only and ctype and "html" not in ctype and "xml" not in ctype:
                raise _Rejected(f"not HTML ({ctype.split(';')[0]})")
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > self.max_bytes:
                    raise _Rejected(f"larger than {self.max_bytes} bytes")
            return bytes(body)
        finally:
            await resp.aclose()

    async def fetch(self, url: str) -> FetchedPage:
        try:
            self.guard.check_url(httpx.URL(url))
        except Blocked as exc:
            return FetchedPage(url, "blocked", exc.reason)
        except Exception:
            return FetchedPage(url, "blocked", "bad_url")
        async with self._client() as client:
            try:
                html = await self._fetch_allowed(client, domain_of(url), url, {})
            except Blocked as exc:  # robots.txt hop refused by the guard
                return FetchedPage(url, "blocked", exc.reason)
            except Exception as exc:
                return FetchedPage(url, "failed", _describe(exc))
        if not isinstance(html, bytes):
            status = "blocked" if (html.reason or "").startswith("blocked") else html.status
            return FetchedPage(url, status, html.reason or "")
        if _PAYWALL.search(html):
            return FetchedPage(url, "paywalled", "isAccessibleForFree: false")
        text = await asyncio.to_thread(extract_text, html, url)
        words = len(text.split())
        if words < self.min_words:
            return FetchedPage(url, "failed", f"extracted {words} words (< {self.min_words})")
        images = page_images(html, url)
        return FetchedPage(
            url, "ok", text=text, words=words, image_url=images.image, meta=page_meta(html, url)
        )
