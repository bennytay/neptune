"""An in-process Foxglove Data Platform API serving recorded responses (ADR 0007 §9).

CI has no network and no Foxglove account, so the connector's tests run against this: a real HTTP
server on a loopback port that answers the documented endpoints (``GET /v1/recordings``,
``/v1/recordings/{id}``, ``/v1/devices``, ``/v1/data/topics`` and ``POST /v1/data/stream``) from
JSON files in ``tests/fixtures/foxglove`` (the API's response shapes, per
https://docs.foxglove.dev/docs/api), and serves the signed download link a stream request returns.
Knobs make it hostile: pages smaller than asked, redirects, links to other hosts, servers that
ignore ranges or state no length, truncated bodies, rate limits, refused keys and raw bodies.
Every request is logged so a test can assert what was (and was not) fetched, and that the API key
reached the API and nothing else.
"""

import json
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "foxglove"
API_KEY = "fox_sk_recorded_fixture_key"
SIGNATURE = "a+b/c=="  # a link's signature: base64 punctuation that must reach the server verbatim


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


STREAM = (FIXTURES / "stream_robot.mcap").read_bytes()


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes = b""


@dataclass
class FakeFoxglove:
    """One Foxglove deployment: an index, devices, topics and a stream per recording."""

    recordings: list[dict[str, Any]] = field(default_factory=lambda: load("recordings.json"))
    devices: list[dict[str, Any]] = field(default_factory=lambda: load("devices.json"))
    topics: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: load("topics.json"))
    streams: dict[str, bytes] = field(default_factory=dict)
    requests: list[Request] = field(default_factory=list)
    # Hostile knobs.
    page_cap: int | None = None  # serve at most this many entries per page, whatever limit is asked
    skip_one: bool = False  # a recording is deleted while paging: later pages shift by one
    raw: dict[str, bytes] = field(default_factory=dict)  # path -> body served instead of JSON
    status: dict[str, int] = field(default_factory=dict)  # path -> status served instead
    link_host: str | None = None  # the host the link names (default: this server)
    link_override: str | None = None  # the whole link, verbatim
    link_redirect: bool = False  # the link answers with a 302
    ignore_range: bool = False  # the link answers a ranged GET with 200 and the whole stream
    no_length: bool = False  # ... and states no Content-Length (chunked)
    no_total: bool = False  # a 206 whose Content-Range total is *
    content_encoding: str | None = None  # the link's Content-Encoding header
    truncate_after: int | None = None  # the link's body is cut after this many bytes
    wrong_range: bool = False  # a 206 for other bytes than were asked for
    stream_total_delta: int = 0  # the link's stated total differs from the stream's length
    rate_limit_streams: bool = False  # POST /data/stream answers 429
    forbid: bool = False  # every API call answers 403
    drop_idle: bool = False  # close every connection after its response
    on_request: Callable[[Request], None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _port: int = 0

    def stream_of(self, recording_id: str) -> bytes | None:
        if recording_id in self.streams:
            return self.streams[recording_id]
        known = {r["id"] for r in self.recordings}
        return STREAM if recording_id in known else None

    def api_requests(self) -> list[Request]:
        return [r for r in self.requests if r.path.startswith("/v1/")]

    def link_requests(self) -> list[Request]:
        return [r for r in self.requests if r.path.startswith("/blob/")]

    @contextmanager
    def serve(self) -> Iterator[str]:
        """Run the server; yield the API endpoint (``http://127.0.0.1:<port>/v1``)."""
        fake = self

        class Handler(_Handler):
            owner = fake

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._port = server.server_address[1]
        try:
            yield f"http://127.0.0.1:{self._port}/v1"
        finally:
            server.shutdown()
            server.server_close()


class _Handler(BaseHTTPRequestHandler):
    owner: FakeFoxglove
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _record(self) -> tuple[str, dict[str, str]]:
        path, _, query = self.path.partition("?")
        params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        headers = {k.lower(): v for k, v in self.headers.items()}
        length = int(headers.get("content-length", "0"))
        body = self.rfile.read(length) if length else b""
        request = Request(self.command, path, params, headers, body)
        with self.owner._lock:
            self.owner.requests.append(request)
        if self.owner.on_request is not None:
            self.owner.on_request(request)
        return path, params

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if not any(name.lower() == "content-length" for name in (headers or {})):
            self.send_header("Content-Length", str(len(body)))
        if self.owner.drop_idle:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, document: Any) -> None:
        self._send(200, json.dumps(document).encode("utf-8"), {"Content-Type": "application/json"})

    def _authorised(self) -> bool:
        ok = self.headers.get("Authorization") == f"Bearer {API_KEY}" and not self.owner.forbid
        if not ok:
            self._send(403, b'{"error":"forbidden"}', {"Content-Type": "application/json"})
        return ok

    def do_GET(self) -> None:
        path, params = self._record()
        fake = self.owner
        if path.startswith("/blob/"):
            self._blob(path, params)
            return
        if not self._authorised():
            return
        if path in fake.status:
            self._send(fake.status[path], b'{"error":"scripted"}')
            return
        if path in fake.raw:
            self._send(200, fake.raw[path], {"Content-Type": "application/json"})
            return
        if path == "/v1/recordings":
            self._recordings(params)
        elif path.startswith("/v1/recordings/"):
            wanted = urllib.parse.unquote(path.rpartition("/")[2])
            found = [r for r in fake.recordings if r["id"] == wanted or r.get("key") == wanted]
            if found:
                self._json(found[0])
            else:
                self._send(404, b'{"error":"not found"}', {"Content-Type": "application/json"})
        elif path == "/v1/devices":
            self._page(fake.devices, params)
        elif path == "/v1/data/topics":
            self._page(fake.topics.get(params.get("recordingId", ""), []), params)
        else:
            self._send(404, b'{"error":"no such route"}')

    def _page(self, items: list[Any], params: dict[str, str]) -> None:
        limit = int(params.get("limit", "2000"))
        if limit > 2000:
            self._send(400, b'{"error":"limit"}')
            return
        if self.owner.page_cap is not None:
            limit = min(limit, self.owner.page_cap)
        offset = int(params.get("offset", "0"))
        self._json(items[offset : offset + limit])

    def _recordings(self, params: dict[str, str]) -> None:
        items = list(self.owner.recordings)
        if "projectId" in params:
            items = [r for r in items if r.get("projectId") == params["projectId"]]
        if "deviceId" in params:
            items = [r for r in items if r.get("device", {}).get("id") == params["deviceId"]]
        if "deviceName" in params:
            items = [r for r in items if r.get("device", {}).get("name") == params["deviceName"]]
        items.sort(key=lambda r: r["createdAt"])
        if self.owner.skip_one and params.get("offset", "0") != "0":
            items = items[1:]  # the first recording was deleted between pages
        self._page(items, params)

    def do_POST(self) -> None:
        path, _ = self._record()
        fake = self.owner
        if not self._authorised():
            return
        if path != "/v1/data/stream":
            self._send(404, b'{"error":"no such route"}')
            return
        if fake.rate_limit_streams:
            self._send(429, b'{"error":"slow down"}', {"Retry-After": "3"})
            return
        if path in fake.status:
            self._send(fake.status[path], b'{"error":"scripted"}')
            return
        request = json.loads(fake.requests[-1].body)
        recording_id = request.get("recordingId", "")
        if fake.stream_of(recording_id) is None:
            self._send(
                404, b'{"error":"NoStreamableRecordings"}', {"Content-Type": "application/json"}
            )
            return
        if fake.link_override is not None:
            link = fake.link_override
        else:
            host = fake.link_host or "127.0.0.1"
            quoted = urllib.parse.quote(SIGNATURE, safe="")
            link = (
                f"http://{host}:{fake._port}/blob/{urllib.parse.quote(recording_id)}?sig={quoted}"
            )
        self._json({"link": link})

    def _blob(self, path: str, params: dict[str, str]) -> None:
        fake = self.owner
        if "authorization" in {k.lower() for k in self.headers}:
            self._send(400, b"the API key was sent to a download link")
            return
        recording_id = urllib.parse.unquote(path[len("/blob/") :])
        data = fake.stream_of(recording_id)
        if params.get("sig") != SIGNATURE or data is None:
            self._send(403, b"expired or wrong signature")
            return
        if fake.link_redirect:
            self._send(302, b"", {"Location": "http://127.0.0.1:9/elsewhere"})
            return
        total = len(data) + fake.stream_total_delta
        headers = {"Content-Type": "application/octet-stream"}
        if fake.content_encoding is not None:
            headers["Content-Encoding"] = fake.content_encoding
        spec = self.headers.get("Range", "")
        if spec.startswith("bytes=") and not fake.ignore_range:
            first, _, last = spec[len("bytes=") :].partition("-")
            start, end = int(first), min(int(last), len(data) - 1)
            if start >= len(data):
                self._send(416, b"", {"Content-Range": f"bytes */{total}"})
                return
            if fake.wrong_range:
                start, end = max(0, start - 1), max(0, end - 1)
            body = data[start : end + 1]
            span = f"{int(first)}-{int(last)}" if fake.wrong_range else f"{start}-{end}"
            headers["Content-Range"] = f"bytes {span}/{'*' if fake.no_total else total}"
            if fake.truncate_after is not None:
                headers["Content-Length"] = str(len(body))
                body = body[: fake.truncate_after]
                self.close_connection = True
            self._send(206, body, headers)
            return
        if fake.no_length:
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n0\r\n\r\n")
            self.close_connection = True
            return
        if fake.truncate_after is not None:
            headers["Content-Length"] = str(total)
            self._send(200, data[: fake.truncate_after], headers)
            self.close_connection = True
            return
        self._send(200, data, headers)
