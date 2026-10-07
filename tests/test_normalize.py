from datetime import UTC, datetime

import pytest

from app.normalize import (
    canonicalize_url,
    content_hash,
    normalize,
    parse_date,
    source_domain,
    strip_html,
)
from app.sources import RawArticle

FETCHED = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://WWW.Example.COM/Path/", "https://www.example.com/Path"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com:443/a//b/", "https://example.com/a/b"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://example.com/a#section-2", "https://example.com/a"),
        (
            "https://example.com/a?utm_source=rss&utm_medium=feed&id=3&fbclid=x&gclid=y",
            "https://example.com/a?id=3",
        ),
        ("https://example.com/a?b=2&a=1&UTM_Campaign=z", "https://example.com/a?a=1&b=2"),
        ("  https://example.com/a  ", "https://example.com/a"),
    ],
)
def test_canonicalize_url(url: str, expected: str) -> None:
    assert canonicalize_url(url) == expected


def test_canonicalize_resolves_relative_urls() -> None:
    assert (
        canonicalize_url("/news/x/", base="https://site.example/feed.xml")
        == "https://site.example/news/x"
    )


def test_strip_html() -> None:
    html = (
        "<p>Hello&nbsp;<b>world</b> &amp; friends</p><script>evil()</script>"
        "<style>p{}</style><div>  next\n\n   para </div>"
    )
    assert strip_html(html) == "Hello world & friends next para"
    assert strip_html(None) == ""
    assert strip_html("plain   text ") == "plain text"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Tue, 06 Oct 2026 15:45:00 +0200", datetime(2026, 10, 6, 13, 45, tzinfo=UTC)),
        ("2026-10-06T14:30:00-04:00", datetime(2026, 10, 6, 18, 30, tzinfo=UTC)),
        ("2026-10-06T14:30:00Z", datetime(2026, 10, 6, 14, 30, tzinfo=UTC)),
        ("2026-10-06T14:30:00", datetime(2026, 10, 6, 14, 30, tzinfo=UTC)),
        ("Thursday, 32 Smarch 2026 25:61:00 PST", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_date(value: str | None, expected: datetime | None) -> None:
    assert parse_date(value) == expected


def test_content_hash_ignores_case_punctuation_and_www() -> None:
    a = content_hash("Oil climbs as supply concerns mount", "https://www.wire.example/a?id=1")
    b = content_hash("Oil Climbs As Supply Concerns Mount!", "https://wire.example/2026/oil")
    c = content_hash("Oil climbs as supply concerns mount", "https://other.example/a")
    assert a == b
    assert a != c
    assert source_domain("https://m.wire.example/x") == "wire.example"


def test_normalize_falls_back_to_fetched_at_and_trims() -> None:
    raw = RawArticle(
        url="https://Example.com/story/?utm_source=x",
        title="  <i>Big</i>   news ",
        summary="<p> Some  <b>text</b> </p>",
        author="  Jane   Doe ",
        published_raw="not a date",
        image_url="/img/a.jpg",
    )
    item = normalize(raw, source_id="s", fetched_at=FETCHED, base_url="https://example.com/feed")
    assert item is not None
    assert item.canonical_url == "https://example.com/story"
    assert item.title == "Big news"
    assert item.summary_raw == "Some text"
    assert item.author == "Jane Doe"
    assert item.published_at == FETCHED
    assert item.fetched_at == FETCHED
    assert item.image_url == "https://example.com/img/a.jpg"


def test_normalize_converts_to_utc_and_rejects_missing_urls() -> None:
    from datetime import timedelta, timezone

    tz = timezone(timedelta(hours=-5))
    published = datetime(2026, 1, 1, tzinfo=tz)
    raw = RawArticle(url="https://e.example/a", title="t", published_at=published)
    item = normalize(raw, source_id="s", fetched_at=FETCHED)
    assert item is not None and item.published_at == datetime(2026, 1, 1, 5, tzinfo=UTC)
    assert normalize(RawArticle(url=None), source_id="s", fetched_at=FETCHED) is None
    assert normalize(RawArticle(url="mailto:x@y.z"), source_id="s", fetched_at=FETCHED) is None
