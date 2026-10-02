"""An in-process Roboto API serving recorded-shape fixtures over real HTTP (ADR 0009 §10).

It serves the documented subset the connector uses: the dataset record, the dataset files query (a
``POST``), events, comments, a file's record, its signed URL and the content behind it, from the
JSON documents in ``fixtures/connectors/roboto`` (pages are served as recorded, chained by
``next_token``). Tests mutate it the way a platform does (a re-upload bumps a file's ``version``, a
deleted and uploaded file has a new ``file_id``) and turn on hostile knobs. Every request is logged.
"""

import copy
import json
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "connectors" / "roboto"
ORG, DATASET, TOKEN = "og_acme_robotics", "ds_shift_0914", "rbt-secret-token-never-printed"


def recorded(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def content_for(path: str, size: int, salt: int = 0) -> bytes:
    """Deterministic bytes of exactly ``size``: a file's content in the fixture."""
    seed = sum(path.encode()) + salt
    return bytes((seed + 7 * i) % 251 for i in range(size))


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes


@dataclass
class FakeRoboto:
    dataset: Any = field(default_factory=lambda: recorded("dataset.json"))
    file_pages: dict[str | None, Any] = field(default_factory=dict)
    event_pages: dict[str | None, Any] = field(default_factory=dict)
    comment_pages: dict[str | None, Any] = field(default_factory=dict)
    contents: dict[str, bytes] = field(default_factory=dict)  # file_id -> bytes
    requests: list[Request] = field(default_factory=list)
    # Hostile knobs.
    redirect: int | None = None  # answer every request with this 3xx
    signed_url: Callable[[str, str], str] | None = None  # (base, path) -> the URL to sign
    content_status: int | None = None  # answer content reads with this status
    content_range_shift: int = 0  # a 206 whose Content-Range starts this many bytes late
    ignore_range: bool = False  # answer content reads with 200 and the whole body
    raw_files: Callable[[str | None], bytes] | None = None  # replace a files page body
    raw_events: Callable[[str | None], bytes] | None = None  # replace an events page body
    drip: float | None = None  # send files pages one byte per this many seconds
    expire_signed_before: int = 0  # signed URLs numbered up to this have expired: content gets 403
    _signed: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.file_pages = {
            None: recorded("files_page_1.json"),
            "tok_files_2": recorded("files_page_2.json"),
        }
        self.event_pages = {
            None: recorded("events_page_1.json"),
            "tok_events_2": recorded("events_page_2.json"),
        }
        self.comment_pages = {None: recorded("comments.json")}
        for record in self.file_records():
            if record["fs_type"] == "file" and record["status"] == "available":
                self.contents[record["file_id"]] = content_for(
                    record["relative_path"], record["size"]
                )

    # --- The platform's side: what a test does to the data ---------------------------------------

    def file_records(self) -> Iterator[dict[str, Any]]:
        for page in self.file_pages.values():
            yield from page["data"]["items"]

    def record(self, path: str) -> dict[str, Any]:
        return next(r for r in self.file_records() if r["relative_path"] == path)

    def reupload(self, path: str, data: bytes) -> dict[str, Any]:
        """A new version of the file at ``path``: ``version`` + 1, new size and bytes."""
        record = self.record(path)
        record["version"] += 1
        record["size"] = len(data)
        record["modified"] = "2026-09-15T08:00:00.000000+00:00"
        self.contents[record["file_id"]] = data
        return record

    def replace_file(self, path: str, data: bytes, file_id: str) -> dict[str, Any]:
        """The file deleted and uploaded again: a new ``file_id`` and ``version`` 1."""
        record = self.record(path)
        del self.contents[record["file_id"]]
        record.update(file_id=file_id, version=1, size=len(data))
        self.contents[file_id] = data
        return record

    def content_requests(self) -> list[Request]:
        return [r for r in self.requests if r.path.startswith("/content/")]

    # --- Serving ---------------------------------------------------------------------------------

    @contextmanager
    def serve(self) -> Iterator[str]:
        fake = self

        class Handler(_Handler):
            roboto = fake

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class _Handler(BaseHTTPRequestHandler):
    roboto: FakeRoboto
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _log(self) -> tuple[str, dict[str, str], bytes]:
        parts = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.roboto.requests.append(
            Request(
                self.command,
                urllib.parse.unquote(parts.path),
                query,
                {k.lower(): v for k, v in self.headers.items()},
                body,
            )
        )
        return urllib.parse.unquote(parts.path), query, body

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, document: Any, *, raw: bytes | None = None, drip: float | None = None) -> None:
        body = raw if raw is not None else json.dumps(document).encode()
        if drip is None:
            self._send(200, body, {"Content-Type": "application/json"})
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            for index in range(len(body)):
                self.wfile.write(body[index : index + 1])
                self.wfile.flush()
                time.sleep(drip)
        except OSError:
            pass

    def do_GET(self) -> None:
        path, query, _ = self._log()
        self._route(path, query, b"")

    def do_POST(self) -> None:
        path, query, body = self._log()
        self._route(path, query, body)

    def _route(self, path: str, query: dict[str, str], body: bytes) -> None:
        fake = self.roboto
        if fake.redirect is not None:
            self._send(fake.redirect, b"", {"Location": "http://elsewhere.invalid/"})
            return
        if path.startswith("/content/"):
            self._content(path)
            return
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, b'{"message": "unauthorised"}')
            return
        if self.headers.get("X-Roboto-Resource-Owner-Id") != ORG:
            self._send(403, b'{"message": "wrong organisation"}')
            return
        parts = path.strip("/").split("/")
        if parts[:2] == ["v1", "datasets"] and len(parts) == 3 and self.command == "GET":
            self._json(fake.dataset)
        elif parts[:2] == ["v1", "datasets"] and parts[3:] == ["files", "query"]:
            token = json.loads(body or b"{}").get("page_token")
            if token != query.get("page_token"):
                self._send(400, b'{"message": "token mismatch"}')
            elif fake.raw_files is not None:
                self._json(None, raw=fake.raw_files(token), drip=fake.drip)
            else:
                self._paged(fake.file_pages, token, drip=fake.drip)
        elif parts[:2] == ["v1", "datasets"] and parts[3:] == ["events"]:
            token = query.get("page_token")
            if fake.raw_events is not None:
                self._json(None, raw=fake.raw_events(token))
            else:
                self._paged(fake.event_pages, token)
        elif parts[:3] == ["v1", "comments", "dataset"]:
            self._paged(fake.comment_pages, query.get("page_token"))
        elif parts[:3] == ["v1", "files", "record"]:
            record = next((r for r in fake.file_records() if r["file_id"] == parts[3]), None)
            if record is None:
                self._send(404, b'{"message": "not found"}')
            else:
                self._json({"data": record})
        elif parts[:2] == ["v1", "files"] and parts[3:] == ["signed-url"]:
            self._signed(parts[2])
        else:
            self._send(404, b'{"message": "no such route"}')

    def _paged(
        self, pages: dict[str | None, Any], token: str | None, *, drip: float | None = None
    ) -> None:
        if token not in pages:
            self._send(404, b'{"message": "no such page"}')
        else:
            self._json(copy.deepcopy(pages[token]), drip=drip)

    def _signed(self, file_id: str) -> None:
        fake = self.roboto
        record = next((r for r in fake.file_records() if r["file_id"] == file_id), None)
        if record is None:
            self._send(404, b'{"message": "not found"}')
            return
        fake._signed += 1
        port = int(self.headers["Host"].rpartition(":")[2])
        base = f"http://127.0.0.1:{port}"
        path = f"/content/{file_id}"
        url = fake.signed_url(base, path) if fake.signed_url else base + path
        # A signature as S3 writes it: percent-escapes, in an order that must be kept.
        sep = "&" if "?" in url else "?"
        credential = "AKID%2F20260914%2Fus-east-1%2Fs3%2Faws4_request"
        url += f"{sep}X-Amz-Credential={credential}&X-Amz-Signature=ab12&n={fake._signed}"
        self._json({"data": {"url": url}})

    def _content(self, path: str) -> None:
        fake = self.roboto
        file_id = path.rsplit("/", 1)[1]
        query = self.fake_query()
        if int(query.get("n", "0")) <= fake.expire_signed_before:
            self._send(403, b"expired")
            return
        if fake.content_status is not None:
            self._send(fake.content_status, b"")
            return
        data = fake.contents.get(file_id)
        if data is None:
            self._send(404, b"")
            return
        spec = self.headers.get("Range")
        if spec is None or fake.ignore_range:
            self._send(200, data)
            return
        first, _, last = spec.removeprefix("bytes=").partition("-")
        start, end = int(first), int(last)
        shifted = start + fake.content_range_shift
        body = data[start : end + 1]
        self._send(
            206,
            body,
            {"Content-Range": f"bytes {shifted}-{shifted + len(body) - 1}/{len(data)}"},
        )

    def fake_query(self) -> dict[str, str]:
        return self.roboto.requests[-1].query
