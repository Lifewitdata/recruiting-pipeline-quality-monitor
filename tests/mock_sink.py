"""A local HTTP endpoint that records what an adapter sends.

This is the whole stand-in for a Slack workspace: bind a real socket on loopback,
return whatever status the test asked for, and keep the bytes. The adapter under test
does a genuine POST over a real socket with a genuine timeout, so the code that runs
in CI is the code that runs in production up to the hostname - and nothing leaves the
machine.

Deliberately stdlib only. An alert path is the last thing you want to be unable to
test because a package failed to install.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Received:
    method: str
    path: str
    content_type: str
    body: bytes

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


@dataclass
class MockSink:
    """`responses` are the status codes to return in order; the last one repeats."""

    responses: list[int] = field(default_factory=lambda: [200])
    reply_body: bytes = b"ok"
    messages: list[Received] = field(default_factory=list)
    _server: object = None
    _thread: object = None

    def next_status(self) -> int:
        index = min(len(self.messages), len(self.responses) - 1)
        return self.responses[index]

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/services/T000/B000/AAA"

    def __enter__(self) -> "MockSink":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - the name http.server requires
                length = int(self.headers.get("Content-Length", 0))
                outer.messages.append(
                    Received(
                        method="POST",
                        path=self.path,
                        content_type=self.headers.get("Content-Type", ""),
                        body=self.rfile.read(length),
                    )
                )
                self.send_response(outer.next_status())
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(outer.reply_body)

            def log_message(self, *_args):  # keep pytest output clean
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()
        return False

    @property
    def last(self) -> Received:
        return self.messages[-1]
