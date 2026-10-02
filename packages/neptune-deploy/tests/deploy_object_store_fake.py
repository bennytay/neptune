"""An in-process object store speaking the S3, GCS JSON and Azure Blob wire formats (ADR 0006 §8).

CI has no cloud and no network, so the connector's tests run against this: a real HTTP server on
a loopback port, holding one bucket (or container) with version history, and serving the subset
of each API the connector uses, as each provider documents it. Knobs make it hostile: redirects,
truncated bodies, ignored ranges, document types, pagination loops, shuffled and resized pages,
and injected entries. Every request is logged, so a test can assert what was (and was not) fetched.

The connector's S3 path is also run against a real S3-compatible server when one is available
(``test_deploy_object_store_live.py``); this fake is what CI relies on.
"""

import hashlib
import json
import random
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from xml.sax.saxutils import escape

S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"


@dataclass
class Version:
    data: bytes
    version_id: str
    generation: int
    deleted: bool = False

    @property
    def etag(self) -> str:
        return hashlib.md5(self.data, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class Entry:
    """One listing entry as the server will send it (tests may inject or rewrite these)."""

    key: bytes
    version: Version
    latest: bool


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]


@dataclass
class FakeStore:
    """One bucket on one provider: ``s3``, ``gcs`` or ``azure``."""

    provider: str = "s3"
    bucket: str = "fleet-logs"
    account: str = "devstoreaccount1"  # Azure only: the endpoint's base path, as Azurite has it
    versioned: bool = True
    history: dict[bytes, list[Version]] = field(default_factory=dict)
    requests: list[Request] = field(default_factory=list)
    # Hostile knobs.
    redirect: int | None = None  # answer every request with this 3xx status
    truncate_after: int | None = None  # object bodies: send this many bytes, then close
    ignore_range: bool = False  # answer a ranged GET with 200 and the whole object
    doctype: bool = False  # listings carry a DOCTYPE declaring an entity
    loop: bool = False  # every truncated page names the first page's cursor again
    shuffle: random.Random | None = None  # random page sizes, entries shuffled within a page
    rewrite: Callable[[int, list[Entry]], list[Entry]] | None = None  # (page number, entries)
    sas_required: bool = False  # Azure: refuse a request without a SAS signature
    _counter: int = 0
    _pages_served: int = 0
    _cache: tuple[object, list[Entry]] = (None, [])
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --- Writing (the test's, never the connector's) -------------------------------------------

    def put(self, key: bytes | str, data: bytes) -> Version:
        raw = key.encode("utf-8") if isinstance(key, str) else key
        with self._lock:
            self._counter += 1
            versions = self.history.setdefault(raw, [])
            if not self.versioned:
                versions.clear()
            version_id = f"v{self._counter:06d}" if self.versioned else "null"
            version = Version(data, version_id, 1_700_000_000_000_000 + self._counter)
            versions.append(version)
            return version

    def delete(self, key: bytes | str) -> None:
        raw = key.encode("utf-8") if isinstance(key, str) else key
        with self._lock:
            if self.versioned:
                self._counter += 1
                marker = Version(b"", f"v{self._counter:06d}", 0, deleted=True)
                self.history[raw].append(marker)
            else:
                del self.history[raw]

    def bulk(self, items: dict[bytes, bytes]) -> None:
        """Many objects at once, cheaply (the scale test)."""
        with self._lock:
            for raw, data in items.items():
                self._counter += 1
                version = Version(data, f"v{self._counter:06d}", self._counter)
                self.history.setdefault(raw, []).append(version)

    # --- Reading ---------------------------------------------------------------------------------

    def latest(self, key: bytes) -> Version | None:
        versions = self.history.get(key)
        if not versions or versions[-1].deleted:
            return None
        return versions[-1]

    def entries(self, prefix: bytes, *, all_versions: bool) -> list[Entry]:
        """Every listing entry under ``prefix``, in key order; kept until the next write."""
        cache_key = (prefix, all_versions, self._counter)
        if self._cache[0] == cache_key:
            return self._cache[1]
        found: list[Entry] = []
        for key in sorted(k for k in self.history if k.startswith(prefix)):
            versions = self.history[key]
            if all_versions:
                found += [
                    Entry(key, version, index == len(versions) - 1)
                    for index, version in reversed(list(enumerate(versions)))
                ]
            elif not versions[-1].deleted:
                found.append(Entry(key, versions[-1], True))
        self._cache = (cache_key, found)
        return found

    def object_requests(self) -> list[Request]:
        """Requests that read object bytes (not listings)."""
        return [r for r in self.requests if "range" in r.headers or "alt" in r.query]

    @contextmanager
    def serve(self) -> Iterator[str]:
        """Run the server; yield its endpoint URL (``http://127.0.0.1:<port>[/<account>]``)."""
        store = self

        class Handler(_Handler):
            fake = store

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            yield f"{base}/{self.account}" if self.provider == "azure" else base
        finally:
            server.shutdown()
            server.server_close()

    def page(self, entries: list[Entry], start: int, size: int) -> tuple[list[Entry], int | None]:
        """The page from ``start`` and the next start; random sizes and order when shuffled."""
        with self._lock:
            number = self._pages_served
            self._pages_served += 1
        if self.shuffle is not None:
            size = self.shuffle.randint(1, max(1, size))
        chosen = entries[start : start + size]
        end = start + len(chosen)
        if self.shuffle is not None:
            chosen = chosen[:]
            self.shuffle.shuffle(chosen)
        if self.rewrite is not None:
            chosen = self.rewrite(number, chosen)
        return chosen, (end if end < len(entries) else None)


class _Handler(BaseHTTPRequestHandler):
    fake: FakeStore
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _record(self) -> tuple[str, dict[str, str]]:
        path, _, query = self.path.partition("?")
        params = dict(
            urllib.parse.parse_qsl(query, keep_blank_values=True, errors="surrogateescape")
        )
        headers = {k.lower(): v for k, v in self.headers.items()}
        self.fake.requests.append(Request(self.command, path, params, headers))
        return path, params

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _refuse(self) -> None:
        self._record()
        self._send(405, b"read-only fake: only GET is served")

    do_PUT = do_POST = do_DELETE = do_HEAD = do_PATCH = _refuse

    def do_GET(self) -> None:
        path, params = self._record()
        fake = self.fake
        if fake.redirect is not None:
            self._send(fake.redirect, b"", {"Location": "http://203.0.113.9/elsewhere"})
            return
        if fake.provider == "s3":
            self._s3(path, params)
        elif fake.provider == "gcs":
            self._gcs(path, params)
        else:
            self._azure(path, params)

    # --- Shared --------------------------------------------------------------------------------

    def _xml(self, body: str) -> None:
        text = '<?xml version="1.0" encoding="UTF-8"?>\n'
        if self.fake.doctype:
            text += '<!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">]>\n'
        self._send(200, (text + body).encode("utf-8"), {"Content-Type": "application/xml"})

    def _object(self, version: Version | None, match: str | None) -> None:
        if version is None:
            self._send(404, b"<Error><Code>NoSuchKey</Code></Error>")
            return
        if match is not None and match.strip('"') != version.etag:
            self._send(412, b"<Error><Code>PreconditionFailed</Code></Error>")
            return
        data = version.data
        status, headers = 200, {"ETag": f'"{version.etag}"'}
        requested = self.headers.get("Range")
        if requested and not self.fake.ignore_range:
            first, _, last = requested.removeprefix("bytes=").partition("-")
            start, end = int(first), min(int(last), len(data) - 1)
            if start >= len(data):
                self._send(416, b"")
                return
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            data = data[start : end + 1]
        if self.fake.truncate_after is not None:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data[: self.fake.truncate_after])
            self.wfile.flush()
            self.close_connection = True
            return
        self._send(status, data, headers)

    # --- S3 ------------------------------------------------------------------------------------

    def _s3(self, path: str, params: dict[str, str]) -> None:
        fake = self.fake
        bucket, _, rest = path.lstrip("/").partition("/")
        if bucket != fake.bucket:
            self._send(404, b"<Error><Code>NoSuchBucket</Code></Error>")
            return
        if not rest and "versions" in params:
            self._s3_versions(params)
        elif not rest and params.get("list-type") == "2":
            self._s3_objects(params)
        elif rest:
            key = urllib.parse.unquote_to_bytes(rest)
            versions = fake.history.get(key, [])
            if "versionId" in params:
                found = [v for v in versions if v.version_id == params["versionId"]]
                version = found[0] if found and not found[0].deleted else None
            else:
                version = fake.latest(key)
            self._object(version, self.headers.get("If-Match"))
        else:
            self._send(400, b"<Error><Code>InvalidRequest</Code></Error>")

    def _s3_key(self, key: bytes, encoded: bool) -> str:
        if encoded:
            return urllib.parse.quote_plus(key, safe="/")
        return escape(key.decode("utf-8"))

    def _s3_versions(self, params: dict[str, str]) -> None:
        fake = self.fake
        encoded = params.get("encoding-type") == "url"
        prefix = params.get("prefix", "").encode("utf-8", "surrogateescape")
        entries = fake.entries(prefix, all_versions=True)
        start = 0
        if "key-marker" in params:
            marker = params["key-marker"].encode("utf-8", "surrogateescape")
            version_marker = params.get("version-id-marker", "")
            start = next(
                (
                    index + 1
                    for index, entry in enumerate(entries)
                    if entry.key == marker and entry.version.version_id == version_marker
                ),
                len(entries),
            )
            if fake.loop:
                start = 0
        chosen, after = fake.page(entries, start, int(params.get("max-keys", "1000")))
        parts = [f'<ListVersionsResult xmlns="{S3_NS}"><Name>{fake.bucket}</Name>']
        if encoded:
            parts.append("<EncodingType>url</EncodingType>")
        for entry in chosen:
            version, key = entry.version, self._s3_key(entry.key, encoded)
            latest = "true" if entry.latest else "false"
            if version.deleted:
                parts.append(
                    f"<DeleteMarker><Key>{key}</Key><VersionId>{version.version_id}</VersionId>"
                    f"<IsLatest>{latest}</IsLatest></DeleteMarker>"
                )
            else:
                parts.append(
                    f"<Version><Key>{key}</Key><VersionId>{version.version_id}</VersionId>"
                    f"<IsLatest>{latest}</IsLatest><ETag>&quot;{version.etag}&quot;</ETag>"
                    f"<Size>{len(version.data)}</Size></Version>"
                )
        if after is not None:
            last = entries[after - 1]
            parts.append(
                f"<IsTruncated>true</IsTruncated>"
                f"<NextKeyMarker>{self._s3_key(last.key, encoded)}</NextKeyMarker>"
                f"<NextVersionIdMarker>{last.version.version_id}</NextVersionIdMarker>"
            )
        else:
            parts.append("<IsTruncated>false</IsTruncated>")
        parts.append("</ListVersionsResult>")
        self._xml("".join(parts))

    def _s3_objects(self, params: dict[str, str]) -> None:
        fake = self.fake
        encoded = params.get("encoding-type") == "url"
        prefix = params.get("prefix", "").encode("utf-8", "surrogateescape")
        entries = fake.entries(prefix, all_versions=False)
        start = int(params["continuation-token"]) if "continuation-token" in params else 0
        if fake.loop and "continuation-token" in params:
            start = 0
        chosen, after = fake.page(entries, start, int(params.get("max-keys", "1000")))
        parts = [f'<ListBucketResult xmlns="{S3_NS}"><Name>{fake.bucket}</Name>']
        if encoded:
            parts.append("<EncodingType>url</EncodingType>")
        for entry in chosen:
            parts.append(
                f"<Contents><Key>{self._s3_key(entry.key, encoded)}</Key>"
                f"<ETag>&quot;{entry.version.etag}&quot;</ETag>"
                f"<Size>{len(entry.version.data)}</Size></Contents>"
            )
        if after is not None:
            parts.append(
                f"<IsTruncated>true</IsTruncated><NextContinuationToken>{after}"
                "</NextContinuationToken>"
            )
        else:
            parts.append("<IsTruncated>false</IsTruncated>")
        parts.append("</ListBucketResult>")
        self._xml("".join(parts))

    # --- GCS -----------------------------------------------------------------------------------

    def _gcs(self, path: str, params: dict[str, str]) -> None:
        fake = self.fake
        base = f"/storage/v1/b/{fake.bucket}/o"
        if path == base:
            prefix = params.get("prefix", "").encode("utf-8", "surrogateescape")
            entries = fake.entries(prefix, all_versions=False)
            start = int(params.get("pageToken", "0"))
            if fake.loop and "pageToken" in params:
                start = 0
            chosen, after = fake.page(entries, start, int(params.get("maxResults", "1000")))
            items = [
                {
                    "kind": "storage#object",
                    "name": entry.key.decode("utf-8", "surrogateescape"),
                    "bucket": fake.bucket,
                    "generation": str(entry.version.generation),
                    "size": str(len(entry.version.data)),
                    "etag": entry.version.etag,
                }
                for entry in chosen
            ]
            document: dict[str, Any] = {"kind": "storage#objects", "items": items}
            if after is not None:
                document["nextPageToken"] = str(after)
            body = json.dumps(document).encode("utf-8")
            self._send(200, body, {"Content-Type": "application/json"})
        elif path.startswith(base + "/") and params.get("alt") == "media":
            key = urllib.parse.unquote_to_bytes(path[len(base) + 1 :])
            wanted = params.get("generation")
            version = fake.latest(key)
            if version is not None and wanted is not None and str(version.generation) != wanted:
                version = None
            self._object(version, None)
        else:
            self._send(404, b'{"error": {"code": 404}}')

    # --- Azure ---------------------------------------------------------------------------------

    def _azure(self, path: str, params: dict[str, str]) -> None:
        fake = self.fake
        if fake.sas_required and "sig" not in params:
            self._send(403, b"<Error><Code>AuthenticationFailed</Code></Error>")
            return
        account, _, rest = path.lstrip("/").partition("/")
        container, _, blob = rest.partition("/")
        if account != fake.account or container != fake.bucket:
            self._send(404, b"<Error><Code>ContainerNotFound</Code></Error>")
            return
        if not blob and params.get("comp") == "list":
            prefix = params.get("prefix", "").encode("utf-8", "surrogateescape")
            entries = fake.entries(prefix, all_versions=False)
            start = int(params["marker"]) if params.get("marker") else 0
            if fake.loop and params.get("marker"):
                start = 0
            chosen, after = fake.page(entries, start, int(params.get("maxresults", "5000")))
            parts = ['<EnumerationResults ServiceEndpoint="x" ContainerName="c"><Blobs>']
            for entry in chosen:
                try:
                    name = f"<Name>{escape(entry.key.decode('utf-8'))}</Name>"
                except UnicodeDecodeError:
                    name = f'<Name Encoded="true">{urllib.parse.quote(entry.key)}</Name>'
                version_xml = (
                    f"<VersionId>{entry.version.version_id}</VersionId>" if fake.versioned else ""
                )
                parts.append(
                    f"<Blob>{name}{version_xml}<Properties><Etag>0x{entry.version.etag[:16].upper()}"
                    f"</Etag><Content-Length>{len(entry.version.data)}</Content-Length>"
                    "</Properties></Blob>"
                )
            parts.append("</Blobs>")
            parts.append(f"<NextMarker>{after}</NextMarker>" if after else "<NextMarker />")
            parts.append("</EnumerationResults>")
            self._xml("".join(parts))
        elif blob:
            key = urllib.parse.unquote_to_bytes(blob)
            if "versionid" in params:
                found = [
                    v for v in fake.history.get(key, []) if v.version_id == params["versionid"]
                ]
                version = found[0] if found and not found[0].deleted else None
            else:
                version = fake.latest(key)
            match = self.headers.get("If-Match")
            if match is not None and version is not None:
                expected = f"0x{version.etag[:16].upper()}"
                match = version.etag if match.strip('"') == expected else "mismatch"
            self._object(version, match)
        else:
            self._send(400, b"<Error><Code>InvalidQueryParameterValue</Code></Error>")
