"""An in-process Formant admin API: a real HTTP server on a loopback port (ADR 0010 §8).

It serves the five query routes the connector uses, each ``POST /v1/admin/<route>/query``, with
``{"items": [...], "continuationToken": ...}`` pages taken from recorded-shape fixtures. A
request's body is parsed, so the page size, the continuation token, the organisation and the
filters the connector sent are checked. Knobs make it hostile: redirects, error statuses, a
malformed page, a continuation token that loops, a page of absurd size, a slow trickle. Every
request is logged, so a test can assert that nothing but queries was sent and what credentials
went where. No request mutates anything: the server holds nothing a request could change.
"""

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "fleet_ops" / "formant"
ROUTE = {
    "devices": "devices",
    "events": "events",
    "annotations": "annotations",
    "intervention-requests": "interventions",
    "files": "recordings",
}


def fixture(name: str) -> dict[str, Any]:
    found: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return found


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    body: dict[str, Any] | None
    headers: dict[str, str]


@dataclass
class FakeFormant:
    """One organisation's data, by part. ``pages[part]`` is the list of pages the part serves."""

    organization: str = "org-acme"
    token: str = "test-token-123"
    pages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    requests: list[Request] = field(default_factory=list)
    status: dict[str, int] = field(default_factory=dict)  # part -> forced HTTP status
    raw: dict[str, bytes] = field(default_factory=dict)  # part -> forced body bytes
    loop: set[str] = field(default_factory=set)  # parts whose last page names its own token again
    bad_page: dict[str, int] = field(default_factory=dict)  # part -> page number with a broken body
    trickle: set[str] = field(default_factory=set)  # parts that send a byte at a time, slowly

    @classmethod
    def standard(cls) -> "FakeFormant":
        """The recorded-shape fixtures: interventions arrive on two pages."""
        store = cls()
        for part in ("devices", "events", "annotations", "recordings"):
            store.pages[part] = [fixture(part)]
        store.pages["interventions"] = [
            fixture("interventions_page_1"),
            fixture("interventions_page_2"),
        ]
        return store


def _handler(store: FakeFormant) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def _answer(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else None
            store.requests.append(Request(method, self.path, body, dict(self.headers)))
            if self.headers.get("Authorization") != f"Bearer {store.token}":
                self._answer(401, b"{}")
                return
            parts = self.path.strip("/").split("/")
            if method != "POST" or parts[:2] != ["v1", "admin"] or parts[-1] != "query":
                self._answer(405, b"{}")
                return
            part = ROUTE.get(parts[2]) if len(parts) == 4 else None
            if part is None or body is None or body.get("organizationId") != store.organization:
                self._answer(404, b"{}")
                return
            if part in store.status:
                status = store.status[part]
                self._answer(
                    status,
                    b"{}",
                    {"Location": "http://127.0.0.1:9/x"} if 300 <= status < 400 else {},
                )
                return
            if part in store.raw:
                self._answer(200, store.raw[part])
                return
            if part in store.trickle:
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                try:
                    for _ in range(100000):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.05)
                except OSError:  # the client's deadline shut the connection
                    pass
                return
            pages = store.pages.get(part, [])
            token = body.get("continuationToken")
            number = 0 if token is None else int(str(token).removeprefix("page-")) - 1
            if not 0 <= number < len(pages):
                self._answer(200, b'{"items": []}')
                return
            if store.bad_page.get(part) == number + 1:
                self._answer(200, b'{"items": [}')
                return
            page = dict(pages[number])
            if number + 1 < len(pages):
                page["continuationToken"] = f"page-{number + 2}"
            elif part in store.loop:
                page["continuationToken"] = f"page-{number + 1}"
            self._answer(200, json.dumps(page).encode())

        def do_POST(self) -> None:
            self._serve("POST")

        def do_GET(self) -> None:
            self._serve("GET")

        def do_PUT(self) -> None:
            self._serve("PUT")

        def do_DELETE(self) -> None:
            self._serve("DELETE")

    return Handler


@contextmanager
def serve(store: FakeFormant) -> Iterator[str]:
    """Serve ``store`` on a loopback port; yields its endpoint URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(store))
    server.daemon_threads = True  # a trickling handler must not outlive the test
    server.block_on_close = False
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
