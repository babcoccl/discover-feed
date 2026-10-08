import asyncio

import httpx

from app.extract import Extractor
from app.models import Article
from tests.pipeline_helpers import add_article

WORDS = " ".join(f"word{i}" for i in range(150))
ARTICLE_HTML = f"""<html><head><title>Story</title></head><body>
<nav><a href="/">Home</a> <a href="/news">News</a></nav>
<article><h1>Big news today</h1><p>{WORDS}</p><p>{WORDS}</p></article>
<footer>Copyright 2026. Privacy. Terms.</footer></body></html>"""
BOILERPLATE_HTML = """<html><body><nav><a href="/">Home</a></nav>
<p>Please enable JavaScript to continue.</p><footer>Cookie settings</footer></body></html>"""


def make_transport(pages: dict[str, httpx.Response], robots: str = "User-agent: *\nAllow: /\n"):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return pages.get(str(request.url), httpx.Response(404))

    return httpx.MockTransport(handler), seen


def html(body: str, **headers) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html", **headers})


def extractor(session_factory, transport, **kwargs) -> Extractor:
    return Extractor(session_factory, transport=transport, domain_delay=0, **kwargs)


def article(session_factory, article_id: int) -> Article:
    with session_factory() as session:
        return session.get(Article, article_id)


def test_extracts_main_text_and_marks_ok(session_factory) -> None:
    aid = add_article(session_factory, url="https://a.example/story")
    transport, seen = make_transport({"https://a.example/story": html(ARTICLE_HTML)})
    result = asyncio.run(extractor(session_factory, transport).run())
    assert (result.processed, result.ok) == (1, 1)
    stored = article(session_factory, aid)
    assert stored.text_status == "ok" and stored.word_count >= 80
    assert "word149" in stored.text and "Cookie" not in stored.text
    assert stored.text_fetched_at is not None
    assert seen == ["https://a.example/robots.txt", "https://a.example/story"]


def test_too_short_404_and_oversized_fail_without_raising(session_factory) -> None:
    short = add_article(session_factory, url="https://a.example/short", hours=3)
    missing = add_article(session_factory, url="https://a.example/missing", hours=2)
    big = add_article(session_factory, url="https://a.example/big", hours=1)
    huge = "<html><body><p>" + "x " * 3000 + "</p></body></html>"
    transport, _ = make_transport(
        {
            "https://a.example/short": html(BOILERPLATE_HTML),
            "https://a.example/big": html(huge),  # no content-length: capped while streaming
        }
    )
    result = asyncio.run(extractor(session_factory, transport, max_bytes=2000).run())
    reasons = {r.article_id: (r.status, r.reason) for r in result.results}
    assert reasons[short][0] == "failed" and "words" in reasons[short][1]
    assert reasons[missing] == ("failed", "HTTP 404")
    assert reasons[big] == ("failed", "larger than 2000 bytes")
    for aid in (short, missing, big):
        stored = article(session_factory, aid)
        assert stored.text_status == "failed" and stored.text is None


def test_content_length_over_cap_is_rejected_before_download(session_factory) -> None:
    aid = add_article(session_factory, url="https://a.example/big")
    transport, _ = make_transport(
        {"https://a.example/big": html(ARTICLE_HTML, **{"content-length": str(5 * 1024**2)})}
    )
    asyncio.run(extractor(session_factory, transport).run())
    assert article(session_factory, aid).text_status == "failed"


def test_robots_disallowed_is_skipped_without_fetching(session_factory) -> None:
    aid = add_article(session_factory, url="https://a.example/private/story")
    transport, seen = make_transport(
        {"https://a.example/private/story": html(ARTICLE_HTML)},
        robots="User-agent: *\nDisallow: /private/\n",
    )
    result = asyncio.run(extractor(session_factory, transport).run())
    assert result.results[0].reason == "disallowed by robots.txt"
    assert article(session_factory, aid).text_status == "skipped"
    assert seen == ["https://a.example/robots.txt"]


def test_network_errors_never_escape(session_factory) -> None:
    aid = add_article(session_factory, url="https://down.example/story")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)  # no robots.txt: everything allowed
        raise httpx.ConnectError("connection refused")

    result = asyncio.run(extractor(session_factory, httpx.MockTransport(handler)).run())
    assert result.failed == 1 and "ConnectError" in result.results[0].reason
    assert article(session_factory, aid).text_status == "failed"


def test_newest_first_limit_and_already_processed_untouched(session_factory) -> None:
    ids = [add_article(session_factory, url=f"https://a.example/{i}", hours=i) for i in range(4)]
    transport, _ = make_transport({})
    ex = extractor(session_factory, transport, max_articles=2)
    first = asyncio.run(ex.run())
    assert [r.article_id for r in first.results] == [ids[3], ids[2]]
    second = asyncio.run(ex.run())
    assert [r.article_id for r in second.results] == [ids[1], ids[0]]
    assert asyncio.run(ex.run()).processed == 0


def test_per_domain_delay_uses_clock(session_factory) -> None:
    for i in range(3):
        add_article(session_factory, url=f"https://a.example/{i}", hours=i)
    add_article(session_factory, url="https://b.example/1")
    now = [100.0]
    sleeps: list[tuple[float, float]] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append((now[0], seconds))
        now[0] += seconds

    transport, seen = make_transport({})
    ex = Extractor(
        session_factory,
        transport=transport,
        domain_delay=2.0,
        clock=lambda: now[0],
        sleep=fake_sleep,
    )
    asyncio.run(ex.run())
    # a.example: robots + 3 pages => 3 waits of 2s; b.example: robots + 1 page.
    a_requests = [u for u in seen if "a.example" in u]
    assert len(a_requests) == 4
    assert all(seconds <= 2.0 for _, seconds in sleeps)
    assert sum(seconds for _, seconds in sleeps) >= 2.0 * 3
    assert ex._last_request.keys() == {"a.example", "b.example"}
