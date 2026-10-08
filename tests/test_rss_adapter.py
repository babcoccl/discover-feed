import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.config import Source, SourceType
from app.sources import (
    FetchError,
    FetchState,
    RSSAdapter,
    UnsupportedSourceType,
    create_adapter,
)
from tests.conftest import fixture_bytes


def _fetch(handler, source: Source, state: FetchState | None = None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await create_adapter(source.type, client).fetch(source, state)

    return asyncio.run(go())


def test_registry_maps_feed_types_to_rss_adapter() -> None:
    client = httpx.AsyncClient()
    assert isinstance(create_adapter(SourceType.RSS, client), RSSAdapter)
    assert isinstance(create_adapter(SourceType.ATOM, client), RSSAdapter)
    with pytest.raises(UnsupportedSourceType):
        create_adapter(SourceType.API, client)


def test_parses_rss_fields_and_images() -> None:
    source = Source(name="Tech", url="https://www.example-tech.com/feed")
    items = _fetch(lambda r: httpx.Response(200, content=fixture_bytes("tech_rss.xml")), source)

    assert len(items) == 3
    first = items[0]
    assert first.url == "https://www.example-tech.com/2026/10/python-315-jit/"
    assert first.author == "Ada Lovelace"
    assert first.published_at == datetime(2026, 10, 6, 17, 0, tzinfo=UTC)
    assert first.image_url == "https://cdn.example-tech.com/img/python-jit.jpg"
    assert "<strong>" in first.summary  # raw HTML is kept until normalization
    assert first.raw["id"] == "example-tech-90210"
    assert items[1].image_url == "https://cdn.example-tech.com/img/oss.png"  # <img> in summary
    assert items[2].image_url == "https://cdn.example-tech.com/img/framework.webp"  # enclosure


def test_parses_atom_and_resolves_relative_links() -> None:
    source = Source(name="Sci", type="atom", url="https://science.example.org/feed.atom")
    items = _fetch(lambda r: httpx.Response(200, content=fixture_bytes("science_atom.xml")), source)

    assert [i.author for i in items] == ["Vera Rubin", "Chien-Shiung Wu"]
    assert items[0].published_at == datetime(2026, 10, 6, 18, 30, tzinfo=UTC)
    assert items[0].image_url == "https://science.example.org/media/webb.jpg"
    assert items[1].url == "https://science.example.org/articles/muon-g2-final"
    assert "anomaly" in items[1].summary  # falls back to <content>
    assert items[1].image_url == "https://science.example.org/media/muon-thumb.jpg"


def test_sends_user_agent_and_conditional_headers_and_stores_validators() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            content=fixture_bytes("tech_rss.xml"),
            headers={"ETag": '"v2"', "Last-Modified": "Wed, 07 Oct 2026 10:00:00 GMT"},
        )

    state = FetchState(etag='"v1"', last_modified="Tue, 06 Oct 2026 10:00:00 GMT")
    source = Source(name="Tech", url="https://www.example-tech.com/feed")

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), headers={"User-Agent": "ua-test"}
        ) as client:
            return await RSSAdapter(client).fetch(source, state)

    asyncio.run(go())
    assert seen[0].headers["If-None-Match"] == '"v1"'
    assert seen[0].headers["If-Modified-Since"] == "Tue, 06 Oct 2026 10:00:00 GMT"
    assert seen[0].headers["User-Agent"] == "ua-test"
    assert state.etag == '"v2"'
    assert state.last_modified == "Wed, 07 Oct 2026 10:00:00 GMT"
    assert not state.not_modified


def test_304_returns_nothing_and_flags_not_modified() -> None:
    state = FetchState(etag='"v1"')
    source = Source(name="Tech", url="https://www.example-tech.com/feed")
    assert _fetch(lambda r: httpx.Response(304), source, state) == []
    assert state.not_modified
    assert state.etag == '"v1"'


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500, text="boom"), httpx.Response(200, text="<html>not a feed</html>")],
)
def test_errors_raise(response: httpx.Response) -> None:
    source = Source(name="Bad", url="https://bad.example/feed")
    with pytest.raises(FetchError):
        _fetch(lambda r: response, source)
