"""Demo fixtures: every feed item links to a local demo page with real article text."""

import re
from pathlib import Path

import pytest
import trafilatura
from fastapi.testclient import TestClient

from app.demo_pages import DEMO_PATH, FIXTURE_DIR, PLACEHOLDER, page_html
from app.main import create_app
from app.settings import Settings

MIN_WORDS = 300
# Real, stable pages a fixture may link to instead of a local demo article (none today).
REAL_PAGES: set[str] = set()


def fixture_items() -> list[tuple[str, str]]:
    items = []
    for path in sorted(FIXTURE_DIR.glob("*.xml")):
        for block in re.findall(r"<item>.*?</item>", path.read_text(encoding="utf-8"), re.S):
            link = re.search(r"<link>([^<]+)</link>", block).group(1).strip()
            items.append((path.stem, link))
    return items


ITEMS = fixture_items()


def test_fixtures_present() -> None:
    assert len(ITEMS) == 66


@pytest.mark.parametrize(("feed", "link"), ITEMS)
def test_item_links_to_local_page_with_text(feed: str, link: str) -> None:
    if link in REAL_PAGES:
        return
    assert link.startswith(PLACEHOLDER + DEMO_PATH), f"{feed}: invented or external URL {link}"
    article_id = link.removeprefix(PLACEHOLDER + DEMO_PATH)
    html = page_html(FIXTURE_DIR, article_id)
    assert html is not None, f"{feed}: no page for {article_id}"
    words = len((trafilatura.extract(html.decode()) or "").split())
    assert words >= MIN_WORDS, f"{article_id}: only {words} words"


def test_demo_article_route_marks_page(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'a.db').as_posix()}",
        config_path=tmp_path / "missing.yaml",
        scheduler_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        page = client.get("/demo-article/the-verge-1")
        assert page.status_code == 200 and "Demo article" in page.text
        assert client.get("/demo-article/../pyproject").status_code == 404
        assert client.get("/demo-article/nope").status_code == 404
