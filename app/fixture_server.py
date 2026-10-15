"""Serve the demo fixture feeds and article pages over HTTP (Docker end-to-end runs).

    python -m app.fixture_server --port 8081 --base http://fixtures:8081 \
        --fixture-dir /fixtures/demo

``/feeds/<source-id>.xml`` (item links point at ``<base>/demo-article/<id>``),
``/demo-article/<id>`` (marked "Demo article") and ``/robots.txt``. With ``--wire-base`` it
also fakes web search for enrichment: a GDELT-shaped ``/gdelt`` whose hits are
``<wire-base>/wire/<id>`` pages (another host name, so another "publisher") that reword a
fixture article (each sentence's words reversed: same vocabulary, no shared phrases).
``--gdelt-faults 429,limit,timeout,empty`` answers the first ``/gdelt`` requests with those
failure modes (HTTP 429 + Retry-After, GDELT's plain-text 200 "limit requests" reply, a
reply slower than the client timeout, an empty 200 body) before answering normally;
``/gdelt-stats`` counts what was served.
"""

import argparse
import html as htmllib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.demo_images import demo_image
from app.demo_pages import DEMO_PATH, FIXTURE_DIR, feed_xml, page_html, robots_txt

_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[a-z0-9]+")


def _page_parts(fixture_dir: Path, article_id: str) -> tuple[str, list[str]] | None:
    raw = page_html(fixture_dir, article_id)
    if raw is None:
        return None
    text = raw.decode("utf-8")
    found = re.search(r"<title>(.*?)</title>", text, re.S)
    title = htmllib.unescape(found.group(1).strip()) if found else article_id
    paras = [
        htmllib.unescape(_TAG.sub("", p)).strip() for p in re.findall(r"<p>(.*?)</p>", text, re.S)
    ]
    return title, [p for p in paras if p]


def gdelt_json(fixture_dir: Path, query: str, wire_base: str) -> bytes:
    """Fixture pages whose titles share 2+ words with ``query``, best first (max 3)."""
    words = set(_WORD.findall(query.split(" sourcelang:")[0].lower()))
    scored = []
    for path in sorted((fixture_dir / "pages").glob("*.html")):
        parts = _page_parts(fixture_dir, path.stem)
        if parts is None:
            continue
        shared = len(words & set(_WORD.findall(parts[0].lower())))
        if shared >= 2:
            scored.append((-shared, path.stem, parts[0]))
    articles = [
        {
            "url": f"{wire_base.rstrip('/')}/wire/{stem}",
            "title": title,
            "domain": "otherwire",
            "language": "English",
            "socialimage": "",
        }
        for _, stem, title in sorted(scored)[:3]
    ]
    return json.dumps({"articles": articles}).encode()


def wire_html(fixture_dir: Path, article_id: str) -> bytes | None:
    parts = _page_parts(fixture_dir, article_id)
    if parts is None:
        return None
    title, paras = parts
    reworded = []
    for para in paras:
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", para) if s]
        reworded.append(
            " ".join(" ".join(reversed(s.rstrip(".!?").split())) + "." for s in sentences)
        )
    body = "".join(f"<p>{htmllib.escape(p)}</p>" for p in reworded)
    return (
        f"<!doctype html><html><head><title>{htmllib.escape(title)}</title></head><body>"
        f"<p><em>Demo wire page: fixture text reworded for the enrichment end-to-end run.</em></p>"
        f"<article><h1>{htmllib.escape(title)}</h1>{body}</article></body></html>"
    ).encode()


FAULTS = ("429", "limit", "timeout", "empty")
RETRY_AFTER = 15
SLOW_SECONDS = 8.0


class GdeltFaults:
    """The failure modes to serve first, in order, and counters for ``/gdelt-stats``."""

    def __init__(self, faults: list[str]) -> None:
        unknown = [f for f in faults if f not in FAULTS]
        if unknown:
            raise SystemExit(f"unknown --gdelt-faults {unknown}; choose from {FAULTS}")
        self.queue = list(faults)
        self.served: dict[str, int] = {"ok": 0, **{f: 0 for f in FAULTS}}
        self.lock = threading.Lock()

    def next(self) -> str:
        with self.lock:
            fault = self.queue.pop(0) if self.queue else "ok"
            self.served[fault] += 1
            return fault


def make_handler(
    fixture_dir: Path,
    base: str,
    wire_base: str | None = None,
    faults: GdeltFaults | None = None,
) -> type[BaseHTTPRequestHandler]:
    faults = faults or GdeltFaults([])

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, ctype: str, headers=None) -> None:
            self.send_response(status)
            self.send_header("content-type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _gdelt_fault(self) -> bool:
            fault = faults.next()
            if fault == "429":
                self._send(
                    429, b"Too Many Requests", "text/plain", {"Retry-After": str(RETRY_AFTER)}
                )
            elif fault == "limit":
                text = b"Please limit requests to one every 5 seconds or contact us."
                self._send(200, text, "text/html")
            elif fault == "timeout":
                time.sleep(SLOW_SECONDS)
                self._send(200, b'{"articles": []}', "application/json")
            elif fault == "empty":
                self._send(200, b"", "application/json")
            return fault != "ok"

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            body, ctype = None, "text/html; charset=utf-8"
            if wire_base and path == "/gdelt" and self._gdelt_fault():
                return
            if path == "/robots.txt":
                body, ctype = robots_txt(fixture_dir).encode(), "text/plain"
            elif path == "/health":
                body, ctype = b'{"status":"ok"}', "application/json"
            elif path.startswith("/feeds/") and path.endswith(".xml"):
                source_id = path.removeprefix("/feeds/").removesuffix(".xml")
                body, ctype = feed_xml(fixture_dir, source_id, base), "application/rss+xml"
            elif path.startswith(DEMO_PATH):
                body = page_html(fixture_dir, path.removeprefix(DEMO_PATH), banner=True, base=base)
            elif wire_base and path == "/gdelt":
                query = parse_qs(urlsplit(self.path).query).get("query", [""])[0]
                body, ctype = gdelt_json(fixture_dir, query, wire_base), "application/json"
            elif wire_base and path == "/gdelt-stats":
                with faults.lock:
                    stats = {**faults.served, "pending_faults": list(faults.queue)}
                body, ctype = json.dumps(stats).encode(), "application/json"
            elif wire_base and path.startswith("/wire/"):
                body = wire_html(fixture_dir, path.removeprefix("/wire/"))
            elif path.startswith("/demo-image/"):
                found = demo_image(path.removeprefix("/demo-image/"))
                if found is not None:
                    body, ctype = found
            self._send(200 if body is not None else 404, body or b"not found", ctype)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--base", default="http://localhost:8081", help="public URL of this server")
    parser.add_argument("--fixture-dir", type=Path, default=FIXTURE_DIR)
    parser.add_argument("--wire-base", help="serve /gdelt + /wire/<id> (enrichment E2E)")
    parser.add_argument(
        "--gdelt-faults", default="", help=f"comma-separated, served first: {', '.join(FAULTS)}"
    )
    args = parser.parse_args()
    faults = GdeltFaults([f for f in args.gdelt_faults.split(",") if f])
    handler = make_handler(args.fixture_dir, args.base, args.wire_base, faults)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"fixture feeds from {args.fixture_dir} on :{args.port} as {args.base}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
