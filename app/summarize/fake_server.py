"""Serve the fake LLM over HTTP (OpenAI-compatible) for Docker end-to-end runs.

    python -m app.summarize.fake_server --port 8090 --mode demo --delay 2

``GET /v1/models`` lists the fake model; ``POST /v1/chat/completions`` answers like
``app.summarize.fake.FakeLLM`` (the same deterministic briefs/reports the tests and demo use).
"""

import argparse
import asyncio
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from app.summarize.fake import MODES, FakeLLM


def make_handler(fake: FakeLLM, timeout_seconds: float = 30) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.rstrip("/").endswith("/models"):
                models = {"object": "list", "data": [{"id": fake.model, "object": "model"}]}
                self._send(200, json.dumps(models).encode())
            elif self.path == "/health":
                self._send(200, b'{"status":"ok"}')
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self) -> None:
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._send(404, b'{"error":"not found"}')
                return
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            request = httpx.Request("POST", f"http://fake-llm{self.path}", content=body)
            try:
                response = asyncio.run(fake.handle(request))
            except httpx.TimeoutException:
                time.sleep(timeout_seconds)  # let the client's own timeout fire
                self._send(504, b'{"error":{"message":"fake timeout"}}')
                return
            if response.headers.get("content-type") == "text/event-stream":
                self._stream(response)
                return
            self._send(response.status_code, response.content)

        def _stream(self, response: httpx.Response) -> None:
            self.send_response(response.status_code)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.end_headers()

            async def pump() -> None:
                try:
                    async for chunk in response.stream:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, httpx.HTTPError):
                    pass
                finally:
                    await response.stream.aclose()

            asyncio.run(pump())

        def log_message(self, format: str, *args: object) -> None:
            print(f"fake-llm {self.command} {self.path} {args[1] if len(args) > 1 else ''}")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--mode", nargs="+", default=["demo"], choices=MODES)
    parser.add_argument("--delay", type=float, default=0, help="seconds per answer")
    parser.add_argument("--token-delay", type=float, default=0.03, help="seconds per chat token")
    args = parser.parse_args()
    fake = FakeLLM(args.mode, delay=args.delay, token_delay=args.token_delay)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(fake))
    print(f"fake LLM ({' '.join(args.mode)}, {args.delay}s/answer) on :{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
