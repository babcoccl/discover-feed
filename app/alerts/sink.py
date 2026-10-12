"""A local fake webhook / ntfy / SMTP receiver for tests and the demo (``/demo/sink``).

python -m app.alerts.sink --port 8099 --smtp-port 8025   # standalone
"""

import argparse
import email
import hashlib
import hmac
import json
import socketserver
import threading
import time
from datetime import UTC, datetime
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeSink:
    def __init__(
        self,
        host: str = "127.0.0.1",
        http_port: int = 0,
        smtp_port: int = 0,
        webhook_secret: str | None = None,
    ) -> None:
        self.host = host
        self.webhook_secret = webhook_secret
        self.messages: list[dict] = []
        self.fail: dict[str, int] = {}
        """``{"webhook": 2}``: fail the next two webhook deliveries (HTTP 500 / SMTP 451)."""
        self.delay = 0.0
        self._lock = threading.Lock()
        self._http = ThreadingHTTPServer((host, http_port), _http_handler(self))
        self._smtp = _SMTPServer((host, smtp_port), _SMTPHandler)
        self._smtp.sink = self
        self._threads: list[threading.Thread] = []

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self._http.server_address[1]}"

    @property
    def webhook_url(self) -> str:
        return f"{self.http_url}/webhook"

    @property
    def ntfy_server(self) -> str:
        return f"{self.http_url}/ntfy"

    @property
    def smtp_port(self) -> int:
        return self._smtp.server_address[1]

    def start(self) -> "FakeSink":
        for server in (self._http, self._smtp):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        for server in (self._http, self._smtp):
            server.shutdown()
            server.server_close()

    def __enter__(self) -> "FakeSink":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def received(self, channel: str | None = None) -> list[dict]:
        with self._lock:
            return [m for m in self.messages if channel is None or m["channel"] == channel]

    def _should_fail(self, channel: str) -> bool:
        with self._lock:
            if self.fail.get(channel, 0) > 0:
                self.fail[channel] -= 1
                return True
        return False

    def _record(self, item: dict) -> None:
        item["received_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            self.messages.append(item)


def _http_handler(sink: FakeSink):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # quiet
            pass

        def _reply(self, status: int, doc: object) -> None:
            body = json.dumps(doc).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/messages":
                self._reply(200, sink.received())
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            channel = "webhook" if self.path.startswith("/webhook") else "ntfy"
            if not self.path.startswith(("/webhook", "/ntfy")):
                return self._reply(404, {"error": "not found"})
            if sink.delay:
                time.sleep(sink.delay)
            if sink._should_fail(channel):
                return self._reply(500, {"error": "sink told to fail"})
            try:
                doc = json.loads(body or b"{}")
            except ValueError:
                return self._reply(400, {"error": "invalid json"})
            item = {
                "channel": channel,
                "delivery": self.headers.get("X-Discover-Delivery"),
                "title": doc.get("title"),
            }
            if channel == "webhook":
                given = self.headers.get("X-Discover-Signature")
                if sink.webhook_secret:
                    expected = (
                        "sha256="
                        + hmac.new(sink.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
                    )
                    item["signature_ok"] = bool(given) and hmac.compare_digest(given, expected)
                else:
                    item["signature_ok"] = None
                item["body"] = doc
            else:
                item |= {
                    "topic": doc.get("topic"),
                    "message": doc.get("message"),
                    "priority": doc.get("priority"),
                    "tags": doc.get("tags"),
                    "click": doc.get("click"),
                    "authorized": self.headers.get("Authorization", "").startswith("Bearer "),
                }
            sink._record(item)
            self._reply(200, {"id": len(sink.messages)})

    return Handler


class _SMTPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    sink: FakeSink


class _SMTPHandler(socketserver.StreamRequestHandler):
    """Just enough SMTP for smtplib: EHLO, AUTH (accepts anything), MAIL, RCPT, DATA, QUIT."""

    def _w(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")

    def handle(self) -> None:
        sink: FakeSink = self.server.sink
        self._w("220 fake-smtp ready")
        sender, rcpts, authed = None, [], False
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            cmd = raw.decode("utf-8", "replace").rstrip("\r\n")
            verb = cmd.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                self._w("250-fake-smtp")
                self._w("250-8BITMIME")
                self._w("250-SMTPUTF8")
                self._w("250 AUTH PLAIN LOGIN")
            elif verb == "AUTH":
                authed = True
                self._w("235 ok")
            elif verb == "MAIL":
                sender, rcpts = cmd.split(":", 1)[1].split()[0].strip("<>"), []
                self._w("250 ok")
            elif verb == "RCPT":
                rcpts.append(cmd.split(":", 1)[1].split()[0].strip("<>"))
                self._w("250 ok")
            elif verb == "DATA":
                self._w("354 end with .")
                lines = []
                while True:
                    line = self.rfile.readline()
                    if not line or line in (b".\r\n", b".\n"):
                        break
                    lines.append(line[1:] if line.startswith(b"..") else line)
                if sink._should_fail("email"):
                    self._w("451 sink told to fail")
                    continue
                message = email.message_from_bytes(b"".join(lines), policy=policy.default)
                plain = message.get_body(("plain",))
                sink._record(
                    {
                        "channel": "email",
                        "delivery": message["Message-ID"],
                        "title": str(message["Subject"]),
                        "from": sender,
                        "to": rcpts,
                        "text": plain.get_content() if plain else "",
                        "has_html": message.get_body(("html",)) is not None,
                        "authenticated": authed,
                    }
                )
                self._w("250 queued")
            elif verb in ("RSET", "NOOP"):
                self._w("250 ok")
            elif verb == "QUIT":
                self._w("221 bye")
                return
            else:
                self._w("502 not implemented")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--smtp-port", type=int, default=8025)
    args = parser.parse_args()
    sink = FakeSink(args.host, args.port, args.smtp_port).start()
    print(f"webhook {sink.webhook_url}  ntfy {sink.ntfy_server}  smtp {args.host}:{sink.smtp_port}")
    print(f"received: {sink.http_url}/messages  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sink.stop()


if __name__ == "__main__":
    main()
