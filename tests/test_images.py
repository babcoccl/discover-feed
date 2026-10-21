"""Card images: page image discovery, validation, SSRF guard, cache/LRU, story selection,
no-image fallbacks and the backfill. All HTTP is mocked."""

import asyncio
import io
import re
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.config import ImagesConfig
from app.demo import build_demo
from app.images.cache import image_key, render_webp
from app.images.fetch import Rejected, fetch_image
from app.images.meta import page_images
from app.images.safety import Blocked, Guard, _GuardedBackend, guarded_get_stream, is_public
from app.images.select import story_image
from app.images.service import ImageRunResult, ImageWorker, image_stats
from app.main import create_app
from app.models import Article, ImageAsset, Story
from tests.pipeline_helpers import T0, add_article

PUBLIC = "93.184.216.34"
PAGE = "https://news.example/2026/10/story.html"


def public(host: str) -> list[str]:
    return [PUBLIC]


def png(width: int, height: int, color=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "PNG")
    return out.getvalue()


def jpeg(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(out, "JPEG")
    return out.getvalue()


def head(tags: str) -> str:
    return f"<html><head><title>t</title>{tags}</head><body><p>x</p></body></html>"


# --- page images -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ('<meta property="og:image" content="/img/lead.jpg">', "https://news.example/img/lead.jpg"),
        ('<meta property="og:image" content="lead.jpg">', "https://news.example/2026/10/lead.jpg"),
        (
            '<meta name="twitter:image" content="https://cdn.example/t.jpg">',
            "https://cdn.example/t.jpg",
        ),
        (
            '<script type="application/ld+json">{"@context":"https://schema.org","@graph":'
            '[{"@type":"WebPage"},{"@type":"NewsArticle","image":[{"@type":"ImageObject",'
            '"url":"//cdn.example/ld.jpg"}]}]}</script>',
            "https://cdn.example/ld.jpg",
        ),
        (
            '<script type="application/ld+json">{"@type":"NewsArticle","image":"/ld2.jpg"}'
            "</script>",
            "https://news.example/ld2.jpg",
        ),
        ('<meta property="og:image" content="javascript:alert(1)">', None),
        ('<meta property="og:image" content="ftp://news.example/a.jpg">', None),
        ('<meta property="og:image" content="data:image/png;base64,AAAA">', None),
        ("", None),
    ],
)
def test_page_image_variants(tags: str, expected: str | None) -> None:
    assert page_images(head(tags), PAGE).image == expected


def test_og_image_beats_twitter_and_icon_prefers_apple_touch() -> None:
    found = page_images(
        head(
            '<meta name="twitter:image" content="/tw.jpg">'
            '<meta property="og:image" content="/og.jpg">'
            '<link rel="icon" href="/favicon.png"><link rel="apple-touch-icon" href="/touch.png">'
        ),
        PAGE,
    )
    assert found.image == "https://news.example/og.jpg"
    assert found.icon == "https://news.example/touch.png"
    assert page_images(b"\x00not html", PAGE).image is None


def test_extractor_stores_page_image_from_fetched_html(session_factory) -> None:
    from app.extract import Extractor

    body = head('<meta property="og:image" content="/lead.jpg">').replace(
        "<p>x</p>", "<p>" + "word " * 200 + "</p>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    aid = add_article(session_factory, url=PAGE)
    extractor = Extractor(
        session_factory, transport=httpx.MockTransport(handler), domain_delay=0, min_words=10
    )
    assert asyncio.run(extractor.run()).ok == 1
    with session_factory() as session:
        article = session.get(Article, aid)
        assert article.page_image_url == "https://news.example/lead.jpg"
        assert article.page_scanned_at is not None


# --- validation --------------------------------------------------------------------------


def serve(routes: dict[str, tuple[int, bytes, dict]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "cookie" not in request.headers and "referer" not in request.headers
        status, body, headers = routes.get(str(request.url), (404, b"", {}))
        return httpx.Response(status, content=body, headers=headers)

    return httpx.MockTransport(handler)


def fetch(url: str, routes, guard: Guard | None = None, **kwargs):
    async def go():
        async with httpx.AsyncClient(transport=serve(routes)) as client:
            return await fetch_image(
                client,
                guard or Guard(public),
                url,
                max_bytes=kwargs.pop("max_bytes", 3 * 1024 * 1024),
                max_pixels=kwargs.pop("max_pixels", 40_000_000),
                min_width=320,
                min_height=180,
                **kwargs,
            )

    return asyncio.run(go())


IMG = "https://cdn.example/a"


@pytest.mark.parametrize(
    ("headers", "body", "url", "reason"),
    [
        ({"content-type": "image/gif"}, png(1, 1), IMG, "tracking_pixel"),
        (
            {"content-type": "image/png"},
            png(800, 450),
            "https://cdn.example/pixel.png",
            "tracking_pixel",
        ),
        ({"content-type": "image/svg+xml"}, b"<svg></svg>", IMG, "svg"),
        ({"content-type": "image/png"}, b"<svg xmlns='x'></svg>", IMG, "svg"),
        ({"content-type": "image/x-icon"}, b"\x00\x00\x01\x00", IMG, "ico"),
        ({"content-type": "image/png"}, png(300, 200), IMG, "too_small"),
        ({"content-type": "text/html"}, b"<html>", IMG, "not_image"),
        ({"content-type": "image/png"}, b"garbage", IMG, "undecodable"),
        ({"content-type": "image/png", "content-length": "9999999"}, b"", IMG, "too_large"),
    ],
)
def test_invalid_images_are_rejected(headers, body, url, reason) -> None:
    with pytest.raises(Rejected) as exc:
        fetch(url, {url: (200, body, headers)})
    assert exc.value.reason == reason


def test_streamed_size_cap_and_decompression_bomb() -> None:
    big = {IMG: (200, png(800, 450), {"content-type": "image/png"})}
    with pytest.raises(Rejected, match="too_large"):
        fetch(IMG, big, max_bytes=100)
    with pytest.raises(Rejected, match="decompression_bomb"):
        fetch(IMG, big, max_pixels=1000)
    got = fetch(IMG, big)
    assert (got.width, got.height, got.content_type) == (800, 450, "image/png")


# --- SSRF ---------------------------------------------------------------------------------


def test_ssrf_private_ip_redirect_and_scheme() -> None:
    ok = {IMG: (200, png(800, 450), {"content-type": "image/png"})}
    with pytest.raises(Rejected, match="private_address"):
        fetch(IMG, ok, Guard(lambda host: ["10.0.0.7"]))
    with pytest.raises(Rejected, match="private_address"):
        fetch("http://127.0.0.1/a.png", ok)
    with pytest.raises(Rejected, match="private_address"):
        fetch("http://[::ffff:192.168.1.1]/a.png", ok)
    redirect = {IMG: (302, b"", {"location": "http://169.254.169.254/latest/meta-data"})}
    with pytest.raises(Rejected, match="private_address"):
        fetch(IMG, redirect)
    rebind = {IMG: (302, b"", {"location": "https://internal.example/x.png"})}
    with pytest.raises(Rejected, match="private_address"):
        fetch(
            IMG, rebind, Guard(lambda h: ["192.168.0.2"] if h == "internal.example" else [PUBLIC])
        )
    for url in ("ftp://cdn.example/a.png", "file:///etc/passwd", "gopher://cdn.example/"):
        with pytest.raises(Rejected, match="bad_scheme"):
            fetch(url, ok)
    loop = {f"https://cdn.example/{i}": (302, b"", {"location": f"/{i + 1}"}) for i in range(5)}
    with pytest.raises(Rejected, match="too_many_redirects"):
        fetch("https://cdn.example/0", loop)
    assert is_public(PUBLIC) and not is_public("::1") and not is_public("100.64.0.1")


def test_connect_time_check_and_allow_list() -> None:
    backend = _GuardedBackend(Guard(lambda host: ["127.0.0.1"]))
    with pytest.raises(Blocked):
        asyncio.run(backend.connect_tcp("cdn.example", 443))
    guard = Guard(lambda host: ["172.18.0.3"], allow_hosts=["fixtures"])
    assert guard.addresses("fixtures") == ["172.18.0.3"]
    with pytest.raises(Blocked):
        guard.addresses("other")

    async def go():
        transport = serve({"https://cdn.example/x": (200, b"", {"content-type": "image/png"})})
        async with httpx.AsyncClient(transport=transport, cookies={"s": "1"}) as client:
            response = await guarded_get_stream(client, Guard(public), "https://cdn.example/x")
            await response.aclose()

    asyncio.run(go())  # the handler asserts no cookie / referer is sent


# --- worker, cache, LRU, selection, backfill ---------------------------------------------


class Server:
    def __init__(self) -> None:
        self.routes: dict[str, bytes] = {}
        self.hits: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.hits.append(str(request.url))
            body = self.routes.get(str(request.url))
            if body is None:
                return httpx.Response(404)
            ctype = "image/png" if body[:4] == b"\x89PNG" else "image/jpeg"
            return httpx.Response(200, content=body, headers={"content-type": ctype})

        return httpx.MockTransport(handler)


def worker(session_factory, tmp_path, server: Server, **config) -> ImageWorker:
    return ImageWorker(
        session_factory,
        ImagesConfig(**config),
        tmp_path / "images",
        transport=server.transport(),
        resolver=public,
        sleep=lambda s: asyncio.sleep(0),
        now=lambda: T0 + timedelta(hours=1),
    )


def story_with(session_factory, *article_ids: int, published: bool = True) -> int:
    with session_factory() as session:
        story = Story(
            title="s",
            representative_article_id=article_ids[0],
            first_seen_at=T0,
            last_updated_at=T0,
            published_version=1 if published else None,
            published_at=T0 if published else None,
        )
        session.add(story)
        session.flush()
        for aid in article_ids:
            session.get(Article, aid).story_id = story.id
        session.commit()
        return story.id


def set_images(session_factory, aid: int, feed=None, page=None, scanned=True) -> None:
    with session_factory() as session:
        article = session.get(Article, aid)
        article.image_url, article.page_image_url = feed, page
        article.page_scanned_at = T0 if scanned else None
        session.commit()


def test_cache_resizes_to_webp_and_route_serves_it(session_factory, tmp_path) -> None:
    server = Server()
    server.routes["https://cdn.example/big.jpg"] = jpeg(1600, 900)
    aid = add_article(session_factory, url="https://pub.example/a", text="t")
    set_images(session_factory, aid, page="https://cdn.example/big.jpg")
    w = worker(session_factory, tmp_path, server)
    assert asyncio.run(w.run()).ok == 1
    key = image_key("https://cdn.example/big.jpg")
    with Image.open(w.cache.path(key)) as img:
        assert img.format == "WEBP" and img.width == 640 and img.height == 360
    with session_factory() as session:
        article = session.get(Article, aid)
        assert article.image_source == "page" and article.image_checked_at is not None
        row = session.scalar(select(ImageAsset).where(ImageAsset.key == key))
        assert row.url == "https://cdn.example/big.jpg" and row.width == 1600


def test_img_route(tmp_path) -> None:
    build = build_demo(tmp_path / "demo.db")
    key = image_key("https://picsum.photos/seed/ars-technica-1/800/450")
    with TestClient(create_app(build.settings)) as client:
        resp = client.get(f"/img/{key}")
        assert resp.status_code == 200 and resp.headers["content-type"] == "image/webp"
        assert "immutable" in resp.headers["cache-control"]
        assert client.get("/img/" + "0" * 32).status_code == 404
        assert client.get("/img/../../etc").status_code == 404
        client.app.state.pipeline.images.cache.delete(key)
        resp = client.get(f"/img/{key}", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://picsum.photos/seed/ars-technica-1/800/450"


def test_lru_evicts_least_recently_used(session_factory, tmp_path) -> None:
    server = Server()
    urls = [f"https://cdn.example/{i}.jpg" for i in range(3)]
    for i, url in enumerate(urls):
        server.routes[url] = jpeg(800 + i, 450)
    w = worker(session_factory, tmp_path, server)
    for url in urls:
        set_images(
            session_factory, add_article(session_factory, url=url + ".html", text="t"), page=url
        )
    asyncio.run(w.run())
    sizes = [w.cache.path(image_key(u)).stat().st_size for u in urls]
    for hours, url in ((2, urls[0]), (3, urls[2])):  # urls[1] is now least recently used
        w.now = lambda hours=hours: T0 + timedelta(hours=hours)
        w.touch(image_key(url))
    w.cache.max_bytes = sizes[0] + sizes[2] + 1
    with session_factory() as session:
        assert w.evict(session) == 1
        session.commit()
    assert w.cache.has(image_key(urls[0])) and not w.cache.has(image_key(urls[1]))
    assert w.cache.has(image_key(urls[2]))
    assert w.cache.size() <= w.cache.max_bytes
    with session_factory() as session:
        stats = image_stats(session, w.cache)
        assert stats["cached_files"] == 2


def test_cache_off_keeps_original_urls(session_factory, tmp_path) -> None:
    server = Server()
    server.routes["https://cdn.example/a.jpg"] = jpeg(800, 450)
    aid = add_article(session_factory, url="https://pub.example/a", text="t")
    set_images(session_factory, aid, feed="https://cdn.example/a.jpg")
    w = worker(session_factory, tmp_path, server, cache=False)
    asyncio.run(w.run())
    assert w.cache.size() == 0
    with session_factory() as session:
        image, _ = story_image(session, [session.get(Article, aid)], aid, ImagesConfig())
        assert image["url"] == "https://cdn.example/a.jpg" and image["key"] is None


def test_story_image_selection(session_factory, tmp_path) -> None:
    server = Server()
    server.routes |= {
        "https://cdn.example/tiny-feed.jpg": jpeg(400, 225),
        "https://cdn.example/page.jpg": jpeg(1200, 675),
        "https://cdn.example/other.jpg": jpeg(1000, 600),
        "https://cdn.example/huge.jpg": jpeg(1900, 1000),
    }
    rep = add_article(session_factory, url="https://pub.example/rep", text="t")
    other = add_article(session_factory, url="https://other.example/o", source_id="b", text="t")
    set_images(
        session_factory,
        rep,
        feed="https://cdn.example/tiny-feed.jpg",
        page="https://cdn.example/page.jpg",
    )
    set_images(session_factory, other, feed="https://cdn.example/other.jpg")
    asyncio.run(worker(session_factory, tmp_path, server).run())
    config = ImagesConfig()
    with session_factory() as session:
        members = [session.get(Article, rep), session.get(Article, other)]
        assert members[0].image_source == "page"  # page beats a tiny feed image
        image, _ = story_image(session, members, rep, config)
        assert image["url"] == "https://cdn.example/page.jpg" and image["source"] == "page"
        session.get(Article, other).image_url = "https://cdn.example/huge.jpg"
        session.add(
            ImageAsset(
                url="https://cdn.example/huge.jpg",
                key=image_key("https://cdn.example/huge.jpg"),
                status="ok",
                width=1900,
                height=1000,
            )
        )
        session.flush()
        image, _ = story_image(session, members, rep, config)
        assert image["url"] == "https://cdn.example/huge.jpg" and image["source"] == "member"


def test_generic_image_is_last_resort(session_factory, tmp_path) -> None:
    server = Server()
    seal = "https://pub.example/static/seal.jpg"
    server.routes |= {
        seal: jpeg(1200, 630),
        "https://cdn.example/small-but-real.jpg": jpeg(500, 300),
    }
    ids = [
        add_article(session_factory, url=f"https://www.pub.example/{i}", text="t") for i in range(3)
    ]
    for aid in ids:
        set_images(session_factory, aid, page=seal)
    other = add_article(session_factory, url="https://else.example/x", source_id="b", text="t")
    set_images(session_factory, other, feed="https://cdn.example/small-but-real.jpg")
    asyncio.run(worker(session_factory, tmp_path, server).run())
    with session_factory() as session:
        assert session.scalar(select(ImageAsset.generic).where(ImageAsset.url == seal))
        members = [session.get(Article, ids[0]), session.get(Article, other)]
        image, _ = story_image(session, members, ids[0], ImagesConfig())
        assert image["url"] == "https://cdn.example/small-but-real.jpg"
        image, _ = story_image(session, members[:1], ids[0], ImagesConfig())
        assert image["url"] == seal and image["generic"] is True  # only when nothing else


def test_no_image_story_gets_logo_with_tint(session_factory, tmp_path) -> None:
    server = Server()
    server.routes["https://pub.example/favicon.ico"] = png(64, 64, (0, 90, 200))
    aid = add_article(session_factory, url="https://pub.example/a", text="t")
    set_images(session_factory, aid, page="https://cdn.example/missing.jpg")
    asyncio.run(worker(session_factory, tmp_path, server).run())
    with session_factory() as session:
        image, logo = story_image(session, [session.get(Article, aid)], aid, ImagesConfig())
        assert image["url"] is None and image["source"] == "logo"
        assert logo["url"] == "https://pub.example/favicon.ico" and logo["key"]
        assert re.fullmatch(r"#[0-9a-f]{6}", logo["tint"]) and logo["tint"] != logo["tint_dark"]
        assert image_stats(session, None)["failures"] == {"http_404": 2}


def test_backfill_is_rate_limited_resumable_and_idempotent(session_factory, tmp_path) -> None:
    server = Server()
    pages = []
    for i in range(3):
        aid = add_article(session_factory, url=f"https://pub.example/{i}", text="t")
        set_images(session_factory, aid, scanned=False)  # extracted before image support
        story_with(session_factory, aid)
        server.routes[f"https://pub.example/{i}.jpg"] = jpeg(900, 500)
        pages.append(aid)
    old = add_article(session_factory, url="https://pub.example/old", text="t")
    set_images(session_factory, old, scanned=False)
    with session_factory() as session:
        sid = story_with(session_factory, old)
        session.get(Story, sid).published_at = T0 - timedelta(days=30)
        session.commit()

    async def page(url):
        from app.images.meta import PageImages

        return PageImages(image=url + ".jpg")

    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)

    w = worker(session_factory, tmp_path, server)
    w.pages, w.sleep, w.backfill_delay = page, sleep, 2.0
    first = asyncio.run(w.backfill(limit=2))
    assert (first.checked, first.remaining) == (2, 1) and sleeps == [2.0]
    second = asyncio.run(w.backfill())
    assert (second.checked, second.remaining) == (1, 0)
    hits = len(server.hits)
    third = asyncio.run(w.backfill())
    assert third.checked == 0 and len(server.hits) == hits  # nothing refetched
    with session_factory() as session:
        assert session.get(Article, old).image_checked_at is None  # outside the window
        assert all(session.get(Article, a).image_source == "page" for a in pages)


def test_already_stored_image_is_reused_as_cache_hit(session_factory, tmp_path) -> None:
    """An ImageAsset row that already exists is reused: the worker neither re-inserts
    it (UNIQUE constraint on images.key) nor counts a failure — it is a cache hit."""
    server = Server()
    stored = "https://cdn.example/stored.jpg"
    now = T0 + timedelta(hours=1)
    aid = add_article(session_factory, url="https://pub.example/a", text="t")
    set_images(session_factory, aid, feed=stored)
    with session_factory() as session:
        session.add(
            ImageAsset(
                url=stored,
                key=image_key(stored),
                kind="photo",
                status="ok",
                checked_at=now,
                cached_bytes=1,
                width=800,
                height=450,
            )
        )
        session.commit()
    result = asyncio.run(worker(session_factory, tmp_path, server).run())
    assert result.status == "ok" and result.error is None
    assert result.cache_hits >= 1 and stored not in server.hits  # reused, not refetched
    with session_factory() as session:
        rows = list(session.scalars(select(ImageAsset).where(ImageAsset.url == stored)))
        assert len(rows) == 1 and rows[0].key == image_key(stored)
        assert session.get(Article, aid).image_checked_at is not None


def test_icon_url_matching_domain_default_is_not_inserted_twice(session_factory, tmp_path) -> None:
    """Regression: a declared icon equal to a domain default is requested twice in one
    article check; autoflush is off, so without an eager flush the second lookup
    inserted a second row and the commit died on UNIQUE(images.key)."""
    server = Server()  # every URL 404s
    aid = add_article(session_factory, url="https://pub.example/a", text="t")
    set_images(session_factory, aid)
    with session_factory() as session:
        session.get(Article, aid).icon_url = "https://pub.example/favicon.ico"
        session.commit()
    result = asyncio.run(worker(session_factory, tmp_path, server).run())
    assert result.status == "ok" and result.error is None
    assert result.cache_hits >= 1  # the repeated favicon URL reused its row
    assert server.hits.count("https://pub.example/favicon.ico") == 1  # fetched once
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(ImageAsset).where(ImageAsset.url == "https://pub.example/favicon.ico")
            )
        )
        assert len(rows) == 1
        assert session.get(Article, aid).image_checked_at is not None


def test_insert_race_with_another_worker_is_a_cache_hit(session_factory, tmp_path) -> None:
    """Safety net: another worker storing the row between our SELECT and our INSERT
    means our INSERT OR IGNORE is skipped — the stored row is reused (a cache hit),
    not a failure."""
    server = Server()
    url = "https://cdn.example/raced.jpg"
    now = T0 + timedelta(hours=1)
    with session_factory() as other:  # the "other worker" already stored the image
        other.add(
            ImageAsset(
                url=url,
                key=image_key(url),
                kind="photo",
                status="ok",
                checked_at=now,
                cached_bytes=1,
            )
        )
        other.commit()
    w = worker(session_factory, tmp_path, server)
    with session_factory() as session:
        real_scalar = session.scalar
        calls = {"n": 0}

        def flaky(statement, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # our SELECT predates the other worker's commit
            return real_scalar(statement, *args, **kwargs)

        session.scalar = flaky
        result = ImageRunResult()

        async def go():
            async with httpx.AsyncClient(transport=server.transport()) as client:
                return await w.asset(client, session, url, "photo", result)

        result_row = asyncio.run(go())
        session.commit()
    assert result.cache_hits == 1 and result.fetched == 0  # cache hit, not a fetch/failure
    assert result_row is not None and result_row.url == url and result_row.status == "ok"
    with session_factory() as session:
        rows = list(session.scalars(select(ImageAsset).where(ImageAsset.url == url)))
        assert len(rows) == 1


def test_render_webp_never_enlarges() -> None:
    with Image.open(io.BytesIO(render_webp(jpeg(400, 300), 640, max_pixels=10**7))) as img:
        assert img.size == (400, 300)


# --- fallback rendering ------------------------------------------------------------------


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    build = build_demo(tmp_path_factory.mktemp("images") / "demo.db")
    with TestClient(create_app(build.settings, llm_transport=build.llm_transport)) as c:
        yield c


def test_fallback_styles_render(demo: TestClient) -> None:
    html = demo.get("/p/market-monitor?no_image=logo").text
    assert "data-image" in html and 'data-fallback="logo"' in html and "data-logo" in html
    assert re.search(r"--tint: #[0-9a-f]{6}; --tint-dark: #[0-9a-f]{6}", html)
    text_only = demo.get("/p/market-monitor?no_image=text_only").text
    assert "data-compact" in text_only and "data-fallback" not in text_only
    assert text_only.count("<article") == html.count("<article")
    gradient = demo.get("/p/market-monitor?no_image=gradient").text
    assert 'data-fallback="gradient"' in gradient and "data-logo" not in gradient


def test_gradients_vary_per_card(demo: TestClient) -> None:
    html = demo.get("/p/market-monitor?no_image=gradient").text
    cards = re.findall(
        r'overflow-hidden bg-gradient-to-br ([^"]+)"\s+data-fallback="gradient"',
        html,
    )
    assert len(cards) >= 2 and len(set(cards)) > 1


def test_api_and_pipeline_expose_images(demo: TestClient) -> None:
    stats = demo.get("/p/personal-reader/settings/pipeline").text
    assert "data-image-stats" in stats and "tracking_pixel" in stats and "svg" in stats
    for source in ("feed", "page", "logo"):
        assert f"<span>{source}</span>" in stats
    items = demo.get("/api/v1/profiles/personal-reader/feed?limit=100").json()["items"]
    sources = {i["image"]["source"] for i in items}
    assert {"feed", "page"} <= sources
    cached = next(i for i in items if i["image"]["proxy_url"])
    assert cached["image"]["proxy_url"].endswith("/img/" + image_key(cached["image"]["url"]))
    assert cached["image_url"] == cached["image"]["url"] and cached["image"]["width"]
