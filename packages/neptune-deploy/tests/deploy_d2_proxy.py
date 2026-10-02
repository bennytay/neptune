"""The D2 gate's hostile reverse proxy: one front for every connector's fake server (MVL-158).

Each connector's own fake serves its system's wire format. The gate puts this proxy in front of
it, so that every connector meets the same attacks from the same code, and so that one log holds
every request a connector sent (method, path, query, headers, body):

- ``redirect_loop``: ``302`` to the request's own URL, forever, for as long as it is followed.
- ``truncate``: the upstream's status and headers (its ``Content-Length`` included), then half of
  its body, then the connection is closed.
- ``slow``: nothing at all until well after the client's timeout.
- ``trickle``: the upstream's headers, then its body one byte per ``drip`` seconds.
- ``oversized``: ``200`` with a ``Content-Length`` of 40 MiB of JSON-looking bytes, more than
  any connector's page limit (Roboto's 8 MiB, everyone else's 32 MiB).

``attack(request)`` names the attack for one request, or ``None`` to forward it unchanged.

Links a fake writes with its own port (Foxglove's stream link) are rewritten to the proxy's, in
JSON bodies and ``Location`` headers, as a reverse proxy does; the ``Host`` header is passed on, so
a fake that builds links from it (Graph's ``nextLink``, Roboto's signed URL) names the proxy. A
connector therefore reaches its fake only through here, and a test checks that on the client side
too (``deploy_d2_support.Wire``).
"""

import http.client
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ATTACKS = ("redirect_loop", "truncate", "slow", "trickle", "oversized")
OVERSIZED = 40 * 1024 * 1024


class Upstream(http.client.HTTPConnection):
    """A connection of the test's own, never a connector's: the proxy forwarding to its fake, or
    an admin client writing to an emulator. ``Wire`` skips it."""


HOP_BY_HOP = frozenset({"connection", "keep-alive", "transfer-encoding", "content-length"})


@dataclass(frozen=True)
class Seen:
    """One request as the proxy received it from the connector."""

    method: str
    path: str  # without the query, percent-decoded
    query: str  # raw
    headers: dict[str, str]  # lower-cased names
    body: bytes


@dataclass
class HostileProxy:
    upstream: str  # ``http://127.0.0.1:<port>`` of the fake (any base path is the connector's)
    attack: Callable[[Seen], str | None] = lambda request: None
    hold: float = 3.0  # how long ``slow`` waits before it answers at all
    drip: float = 0.2  # seconds per byte of ``trickle``
    log: list[Seen] = field(default_factory=list)
    port: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def authority(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"http://{self.authority}"

    def requests(self) -> list[Seen]:
        with self._lock:
            return list(self.log)

    @contextmanager
    def serve(self) -> Iterator["HostileProxy"]:
        proxy = self

        class Handler(_Handler):
            owner = proxy

        class Quiet(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

            def handle_error(self, request: Any, client_address: Any) -> None:
                pass  # a client that gives up mid-attack is the point

        server = Quiet(("127.0.0.1", 0), Handler)
        self.port = server.server_address[1]
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield self
        finally:
            server.shutdown()
            server.server_close()


class _Handler(BaseHTTPRequestHandler):
    owner: HostileProxy
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _handle(self) -> None:
        proxy = self.owner
        raw_path, _, query = self.path.partition("?")
        headers = {k.lower(): v for k, v in self.headers.items()}
        length = headers.get("content-length", "0")
        body = self.rfile.read(int(length)) if length.isdigit() and int(length) else b""
        seen = Seen(self.command, urllib.parse.unquote(raw_path), query, headers, body)
        with proxy._lock:
            proxy.log.append(seen)
        mode = proxy.attack(seen)
        try:
            if mode == "redirect_loop":
                self._reply(302, b"", {"Location": f"{proxy.url}{self.path}"})
            elif mode == "slow":
                time.sleep(proxy.hold)
                self._reply(200, b"{}", {"Content-Type": "application/json"})
            elif mode == "oversized":
                self._oversized()
            else:
                status, sent, data = self._forward(seen)
                if mode == "truncate":
                    self._cut(status, sent, data)
                elif mode == "trickle":
                    self._trickle(status, sent, data)
                else:
                    self._reply(status, data, sent)
        except OSError:
            self.close_connection = True  # the client gave up: its deadline worked

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _handle

    def _forward(self, seen: Seen) -> tuple[int, dict[str, str], bytes]:
        proxy = self.owner
        target = urllib.parse.urlsplit(proxy.upstream)
        connection = Upstream(target.hostname or "127.0.0.1", target.port, timeout=30)
        try:
            connection.putrequest(seen.method, self.path, skip_host=True, skip_accept_encoding=True)
            for name, value in self.headers.items():
                if name.lower() not in HOP_BY_HOP:
                    connection.putheader(name, value)
            connection.putheader("Content-Length", str(len(seen.body)))
            connection.putheader("Connection", "close")
            connection.endheaders(seen.body)
            response = connection.getresponse()
            data = response.read()
            sent = {k: v for k, v in response.getheaders() if k.lower() not in HOP_BY_HOP}
            status = response.status
        finally:
            connection.close()
        own = f"127.0.0.1:{target.port}".encode()
        if own in data and "json" in str(sent.get("Content-Type", "")).lower():
            data = data.replace(own, proxy.authority.encode())
        for name in list(sent):
            if name.lower() == "location":
                sent[name] = sent[name].replace(own.decode(), proxy.authority)
        return status, sent, data

    def _reply(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _cut(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(max(len(body), 2)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body[: len(body) // 2])
        self.wfile.flush()
        self.close_connection = True

    def _trickle(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        padded = body if len(body) > 64 else body + b" " * (64 - len(body))
        self.send_header("Content-Length", str(len(padded)))
        self.end_headers()
        for index in range(len(padded)):
            self.wfile.write(padded[index : index + 1])
            self.wfile.flush()
            time.sleep(self.owner.drip)
        self.close_connection = True

    def _oversized(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(OVERSIZED))
        self.end_headers()
        block = b'{"items": [' + b'{"id": "x"},' * 87_000
        sent = 0
        while sent < OVERSIZED:
            chunk = block[: OVERSIZED - sent]
            self.wfile.write(chunk)
            sent += len(chunk)
        self.close_connection = True
