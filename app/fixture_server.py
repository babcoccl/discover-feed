"""Serve the demo fixture feeds and article pages over HTTP (Docker end-to-end runs).

    python -m app.fixture_server --port 8081 --base http://fixtures:8081 \
        --fixture-dir /fixtures/demo

``/feeds/<source-id>.xml`` (item links point at ``<base>/demo-article/<id>``),
``/demo-article/<id>`` (marked "Demo article") and ``/robots.txt``.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.demo_images import demo_image
from app.demo_pages import DEMO_PATH, FIXTURE_DIR, feed_xml, page_html, robots_txt


def make_handler(fixture_dir: Path, base: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            body, ctype = None, "text/html; charset=utf-8"
            if path == "/robots.txt":
                body, ctype = robots_txt(fixture_dir).encode(), "text/plain"
            elif path == "/health":
                body, ctype = b'{"status":"ok"}', "application/json"
            elif path.startswith("/feeds/") and path.endswith(".xml"):
                source_id = path.removeprefix("/feeds/").removesuffix(".xml")
                body, ctype = feed_xml(fixture_dir, source_id, base), "application/rss+xml"
            elif path.startswith(DEMO_PATH):
                body = page_html(fixture_dir, path.removeprefix(DEMO_PATH), banner=True, base=base)
            elif path.startswith("/demo-image/"):
                found = demo_image(path.removeprefix("/demo-image/"))
                if found is not None:
                    body, ctype = found
            self.send_response(200 if body is not None else 404)
            self.send_header("content-type", ctype)
            body = body if body is not None else b"not found"
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--base", default="http://localhost:8081", help="public URL of this server")
    parser.add_argument("--fixture-dir", type=Path, default=FIXTURE_DIR)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.fixture_dir, args.base))
    print(f"fixture feeds from {args.fixture_dir} on :{args.port} as {args.base}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
