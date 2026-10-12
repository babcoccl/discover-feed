"""Demo fixtures: feeds and local article pages from ``tests/fixtures/demo``.

Fixture feed items link to ``{{demo_base}}/demo-article/<id>``; the placeholder is replaced
with the URL the pages are served from (the demo app, or the Docker fixture server), so no
fixture points at an invented URL on a real publisher's domain.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "demo"
PLACEHOLDER = "{{demo_base}}"
DEMO_PATH = "/demo-article/"
_ID = re.compile(r"^[a-z0-9-]+$")
BANNER = (
    '<div class="demo-article-banner" role="note" style="background:#fef3c7;color:#78350f;'
    'padding:8px 12px;font:14px system-ui,sans-serif">Demo article: fixture text written for '
    "the discover-feed demo, not a real publisher page.</div>"
)
DEFAULT_ROBOTS = "User-agent: *\nAllow: /\n"


def fixture_id(url: str) -> str:
    """``.../demo-article/the-verge-1`` -> ``the-verge-1``."""
    return url.rstrip("/").rsplit("/", 1)[-1]


def feed_xml(fixture_dir: Path, source_id: str, base: str) -> bytes | None:
    path = fixture_dir / f"{source_id}.xml"
    if not _ID.match(source_id) or not path.exists():
        return None
    return path.read_text(encoding="utf-8").replace(PLACEHOLDER, base.rstrip("/")).encode()


def page_html(fixture_dir: Path, article_id: str, *, banner: bool = False) -> bytes | None:
    path = fixture_dir / "pages" / f"{article_id}.html"
    if not _ID.match(article_id) or not path.exists():
        return None
    html = path.read_text(encoding="utf-8")
    if banner:
        html = html.replace("<body>", f"<body>\n{BANNER}", 1)
    return html.encode()


def robots_txt(fixture_dir: Path) -> str:
    path = fixture_dir / "pages" / "robots.txt"
    return path.read_text(encoding="utf-8") if path.exists() else DEFAULT_ROBOTS
