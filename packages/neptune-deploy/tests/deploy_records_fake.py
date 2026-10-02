"""In-process record systems speaking the Jira, ServiceNow, Drive, Confluence and a declared REST
wire format (ADR 0008 §9).

CI has no network and no tenants, so the connectors' tests run against these: a real HTTP server on
a loopback port whose backend serves the documented response shapes from recorded fixtures
(``fixtures/records/*.json``) and can be edited between syncs (an issue updated, a file revised, a
record deleted). Knobs make it hostile: injected replies (429, 5xx, redirects, truncated or
compressed bodies, any bytes), a request log, and a count of methods other than GET.
"""

import base64
import copy
import hashlib
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

FIXTURES = Path(__file__).parent / "fixtures" / "records"
JSON = {"Content-Type": "application/json;charset=UTF-8"}


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes = b""


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=lambda: dict(JSON))
    declared_length: int | None = None  # a Content-Length other than the body's: truncation
    trickle: float = 0.0  # seconds between the body's bytes: a server that never finishes


def reply_json(value: Any, status: int = 200, headers: dict[str, str] | None = None) -> Reply:
    return Reply(status, json.dumps(value).encode(), {**JSON, **(headers or {})})


class Backend:
    """A record system's server side. ``handle`` returns a reply, or ``None`` for 404."""

    auth_header = "Authorization"
    auth_value = "Bearer token-never-printed"
    accepts_post = False  # a GraphQL system takes POST, and nothing else

    def handle(self, request: Request) -> Reply | None:  # pragma: no cover - overridden
        raise NotImplementedError

    def authorised(self, request: Request) -> bool:
        return request.headers.get(self.auth_header.lower()) == self.auth_value


@dataclass
class Injection:
    match: Callable[[Request], bool]
    reply: Reply
    times: int


class FakeServer:
    """Serves one backend over HTTP on a loopback port, logging every request."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.log: list[Request] = []
        self.other_methods: list[str] = []
        self.injections: list[Injection] = []

    def inject(self, match: Callable[[Request], bool], reply: Reply, times: int = 1) -> None:
        self.injections.append(Injection(match, reply, times))

    def requests(self, fragment: str = "") -> list[Request]:
        return [r for r in self.log if fragment in r.path]

    def _respond(self, request: Request) -> Reply:
        self.log.append(request)
        for injection in self.injections:
            if injection.times > 0 and injection.match(request):
                injection.times -= 1
                return injection.reply
        if not self.backend.authorised(request):
            return reply_json({"error": "unauthorised"}, 401)
        return self.backend.handle(request) or reply_json({"error": "not found"}, 404)

    @contextmanager
    def serve(self) -> Iterator[str]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                parts = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
                headers = {k.lower(): v for k, v in self.headers.items()}
                reply = server._respond(Request("GET", parts.path, query, headers))
                self.send_response(reply.status)
                length = len(reply.body) if reply.declared_length is None else reply.declared_length
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(length))
                self.end_headers()
                if reply.trickle:
                    try:
                        for i in range(len(reply.body)):
                            self.wfile.write(reply.body[i : i + 1])
                            self.wfile.flush()
                            time.sleep(reply.trickle)
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                self.wfile.write(reply.body)
                if reply.declared_length is not None:
                    self.close_connection = True

            def do_POST(self) -> None:
                if not server.backend.accepts_post:
                    self._other()
                    return
                parts = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                headers = {k.lower(): v for k, v in self.headers.items()}
                reply = server._respond(Request("POST", parts.path, {}, headers, body))
                self.send_response(reply.status)
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                self.wfile.write(reply.body)

            def _other(self) -> None:
                server.other_methods.append(self.command)
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_PUT = do_DELETE = do_PATCH = do_HEAD = _other

            def log_message(self, *args: Any) -> None:
                pass

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                pass  # a client that hangs up mid-reply is the point of several tests

        httpd = Quiet(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield f"127.0.0.1:{httpd.server_address[1]}"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()


# --- Jira ----------------------------------------------------------------------------------------


class JiraBackend(Backend):
    """``/rest/api/2/search/jql`` and ``/rest/api/2/attachment/content/{id}``."""

    auth_value = "Basic " + base64.b64encode(b"ops@example.com:jira-secret").decode()

    def __init__(self) -> None:
        data = fixture("jira_search_ops.json")
        self.issues: list[dict[str, Any]] = data["issues"]
        self.bytes: dict[str, bytes] = {k: v.encode() for k, v in data["attachment_bytes"].items()}
        self.project = "OPS"

    def edit(self, key: str, updated: str, **fields: Any) -> None:
        for issue in self.issues:
            if issue["key"] == key:
                issue["fields"].update(fields)
                issue["fields"]["updated"] = updated

    def delete(self, key: str) -> None:
        self.issues = [i for i in self.issues if i["key"] != key]

    def _with_sizes(self, issue: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(issue)
        for attachment in out["fields"].get("attachment") or []:
            attachment["size"] = len(self.bytes.get(attachment["id"], b""))
        return out

    def handle(self, request: Request) -> Reply | None:
        if request.path == "/rest/api/2/search/jql":
            return self._search(request)
        prefix = "/rest/api/2/attachment/content/"
        if request.path.startswith(prefix):
            if request.query.get("redirect") != "false":
                return Reply(303, headers={"Location": "https://api.media.example/file"})
            data = self.bytes.get(request.path[len(prefix) :])
            return (
                Reply(200, data, {"Content-Type": "application/octet-stream"})
                if data is not None
                else None
            )
        return None

    def _search(self, request: Request) -> Reply:
        jql = request.query["jql"]
        issues = [i for i in self.issues if f'project = "{self.project}"' in jql]
        if 'updated >= "' in jql:
            bound = jql.split('updated >= "')[1].split('"')[0]
            issues = [i for i in issues if i["fields"]["updated"][:16].replace("T", " ") >= bound]
        issues.sort(key=lambda i: (i["fields"]["updated"], i["key"]))
        start = int(request.query.get("nextPageToken") or 0)
        size = int(request.query["maxResults"])
        wanted = request.query["fields"].split(",")
        page = issues[start : start + size]
        body: dict[str, Any] = {
            "issues": [
                {
                    "id": i["id"],
                    "key": i["key"],
                    "fields": {
                        k: v for k, v in self._with_sizes(i)["fields"].items() if k in wanted
                    },
                }
                for i in page
            ]
        }
        if start + size < len(issues):
            body["nextPageToken"] = str(start + size)
        else:
            body["isLast"] = True
        return reply_json(body)


# --- ServiceNow ----------------------------------------------------------------------------------


class ServiceNowBackend(Backend):
    """``/api/now/table/change_request``, ``/api/now/attachment`` and ``sys_audit_delete``."""

    auth_value = "Basic " + base64.b64encode(b"integration:sn-secret").decode()

    def __init__(self) -> None:
        data = fixture("servicenow_change_requests.json")
        self.rows: list[dict[str, str]] = data["rows"]
        self.attachments: list[dict[str, str]] = data["attachments"]
        self.bytes: dict[str, bytes] = {k: v.encode() for k, v in data["attachment_bytes"].items()}
        self.deleted: list[dict[str, str]] = []

    def edit(self, number: str, updated: str, **fields: str) -> None:
        for row in self.rows:
            if row["number"] == number:
                row.update(fields)
                row["sys_updated_on"] = updated
                row["sys_mod_count"] = str(int(row["sys_mod_count"]) + 1)

    def delete(self, number: str, when: str) -> None:
        for row in self.rows:
            if row["number"] == number:
                self.deleted.append(
                    {
                        "documentkey": row["sys_id"],
                        "tablename": "change_request",
                        "sys_created_on": when,
                    }
                )
        self.rows = [r for r in self.rows if r["number"] != number]

    def handle(self, request: Request) -> Reply | None:
        if request.path == "/api/now/table/change_request":
            return self._table(request, self._ordered(request, self.rows))
        if request.path == "/api/now/table/sys_audit_delete":
            bound = request.query["sysparm_query"].split("sys_created_on>=")[1].split("^")[0]
            rows = [d for d in self.deleted if d["sys_created_on"] >= bound]
            return self._table(request, rows)
        if request.path == "/api/now/attachment":
            query = request.query["sysparm_query"]
            ids = query.split("table_sys_idIN")[1].split("^")[0].split(",")
            rows = [
                {**a, "size_bytes": str(len(self.bytes[a["sys_id"]]))}
                for a in self.attachments
                if a["table_sys_id"] in ids
            ]
            if "^ORDERBYsys_id" in query:  # a paged query is ordered, or pages skip and repeat
                rows.sort(key=lambda a: a["sys_id"])
            return self._table(request, rows)
        prefix = "/api/now/attachment/"
        if request.path.startswith(prefix) and request.path.endswith("/file"):
            data = self.bytes.get(request.path[len(prefix) : -len("/file")])
            return (
                Reply(200, data, {"Content-Type": "application/octet-stream"})
                if data is not None
                else None
            )
        return None

    def _ordered(self, request: Request, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        query = request.query["sysparm_query"]
        for part in query.split("^"):
            if part.startswith("sys_updated_on>="):
                rows = [
                    r for r in rows if r["sys_updated_on"] >= part.removeprefix("sys_updated_on>=")
                ]
        return sorted(rows, key=lambda r: (r["sys_updated_on"], r["sys_id"]))

    def _table(self, request: Request, rows: list[dict[str, str]]) -> Reply:
        offset, limit = int(request.query["sysparm_offset"]), int(request.query["sysparm_limit"])
        wanted = request.query["sysparm_fields"].split(",")
        page = [{k: v for k, v in r.items() if k in wanted} for r in rows[offset : offset + limit]]
        return reply_json({"result": page}, headers={"X-Total-Count": str(len(rows))})


# --- Google Drive --------------------------------------------------------------------------------


def md5_of(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


class DriveBackend(Backend):
    """``files.list``, ``changes.getStartPageToken``/``list`` and ``files.get?alt=media``."""

    auth_value = "Bearer drive-token-never-printed"

    def __init__(self) -> None:
        data = fixture("drive_files.json")
        self.files: dict[str, dict[str, Any]] = {f["id"]: f for f in data["files"]}
        for f in self.files.values():
            f["content"] = f["content"].encode()
        self.native = data["native"]
        self.folders = data["folders"]
        self.changes: list[dict[str, Any]] = []  # the change log; a page token is an index into it

    def edit(self, file_id: str, content: bytes) -> None:
        f = self.files[file_id]
        f["content"] = content
        f["version"] = str(int(f["version"]) + 1)
        self.changes.append({"fileId": file_id, "removed": False})

    def touch(self, file_id: str) -> None:
        """A version bump with the same bytes (a share, a rename): Drive's version still moves."""
        f = self.files[file_id]
        f["version"] = str(int(f["version"]) + 1)
        self.changes.append({"fileId": file_id, "removed": False})

    def add(self, file_id: str, name: str, content: bytes, mime: str = "application/pdf") -> None:
        self.files[file_id] = {
            "id": file_id, "name": name, "mimeType": mime, "content": content,
            "version": "1", "trashed": False,
        }  # fmt: skip
        self.changes.append({"fileId": file_id, "removed": False})

    def trash(self, file_id: str) -> None:
        self.files[file_id]["trashed"] = True
        self.changes.append({"fileId": file_id, "removed": False})

    def remove(self, file_id: str) -> None:
        del self.files[file_id]
        self.changes.append({"fileId": file_id, "removed": True})

    def _meta(self, f: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in f.items() if k != "content"}
        if "content" in f:
            out["size"] = str(len(f["content"]))
            out["md5Checksum"] = md5_of(f["content"])
        return out

    def handle(self, request: Request) -> Reply | None:
        if request.path == "/drive/v3/changes/startPageToken":
            return reply_json({"startPageToken": str(len(self.changes))})
        if request.path == "/drive/v3/files":
            live = [self._meta(f) for f in self.files.values() if not f["trashed"]]
            live += [self._meta(f) for f in self.native]
            return self._paged(request, sorted(live, key=lambda f: f["id"]))
        if request.path == "/drive/v3/changes":
            return self._changes(request)
        prefix = "/drive/v3/files/"
        if request.path.startswith(prefix) and request.query.get("alt") == "media":
            f = self.files.get(request.path[len(prefix) :])
            return Reply(200, f["content"], {"Content-Type": "application/pdf"}) if f else None
        return None

    def _paged(self, request: Request, files: list[dict[str, Any]]) -> Reply:
        start = int(request.query.get("pageToken") or 0)
        size = int(request.query["pageSize"])
        body: dict[str, Any] = {"files": files[start : start + size]}
        if start + size < len(files):
            body["nextPageToken"] = str(start + size)
        return reply_json(body)

    def _changes(self, request: Request) -> Reply:
        start, size = int(request.query["pageToken"]), int(request.query["pageSize"])
        out = []
        for change in self.changes[start : start + size]:
            f = self.files.get(change["fileId"])
            entry: dict[str, Any] = {"fileId": change["fileId"], "removed": change["removed"]}
            if f is not None and not change["removed"]:
                entry["file"] = self._meta(f)
            out.append(entry)
        body: dict[str, Any] = {"changes": out}
        if start + size < len(self.changes):
            body["nextPageToken"] = str(start + size)
        else:
            body["newStartPageToken"] = str(len(self.changes))
        return reply_json(body)


# --- Confluence ----------------------------------------------------------------------------------


class ConfluenceBackend(Backend):
    """``/wiki/api/v2/pages`` with ``body-format=storage`` and cursor paging."""

    auth_value = "Basic " + base64.b64encode(b"wiki@example.com:wiki-secret").decode()

    def __init__(self) -> None:
        self.pages: list[dict[str, Any]] = fixture("confluence_pages.json")["pages"]

    def edit(self, page_id: str, storage: str) -> None:
        for page in self.pages:
            if page["id"] == page_id:
                page["storage"] = storage
                page["version"] = {"number": page["version"]["number"] + 1}

    def handle(self, request: Request) -> Reply | None:
        if request.path != "/wiki/api/v2/pages" or request.query.get("body-format") != "storage":
            return None
        pages = sorted(
            (p for p in self.pages if p["spaceId"] == request.query["space-id"]),
            key=lambda p: int(p["id"]),
        )
        start = int(request.query.get("cursor") or 0)
        size = int(request.query["limit"])
        results = [
            {
                "id": p["id"],
                "status": p["status"],
                "title": p["title"],
                "spaceId": p["spaceId"],
                "version": p["version"],
                "body": {"storage": {"representation": "storage", "value": p["storage"]}},
            }
            for p in pages[start : start + size]
        ]
        body: dict[str, Any] = {"results": results, "_links": {}}
        if start + size < len(pages):
            space = request.query["space-id"]
            body["_links"]["next"] = (
                f"/wiki/api/v2/pages?space-id={space}&limit={size}&cursor={start + size}"
            )
        return reply_json(body)


# --- A declared REST CMMS ------------------------------------------------------------------------


class RestBackend(Backend):
    """The profile ``cmms_profile.json``: work orders with cursor paging and ``updatedAfter``."""

    auth_header = "Session-Token"
    auth_value = "cmms-session-token-never-printed"

    def __init__(self) -> None:
        data = fixture("cmms_work_orders.json")
        self.orders: list[dict[str, Any]] = data["work_orders"]
        self.bytes = {k: v.encode() for k, v in data["attachment_bytes"].items()}

    def edit(self, order_id: int, updated: str, **fields: Any) -> None:
        for order in self.orders:
            if order["id"] == order_id:
                order.update(fields)
                order["updatedAt"] = updated

    def delete(self, order_id: int) -> None:
        self.orders = [o for o in self.orders if o["id"] != order_id]

    def handle(self, request: Request) -> Reply | None:
        if request.path == "/api/v1/work-orders":
            orders = sorted(self.orders, key=lambda o: (o["updatedAt"], o["id"]))
            if "updatedAfter" in request.query:
                orders = [o for o in orders if o["updatedAt"] >= request.query["updatedAfter"]]
            start = int(request.query.get("cursor") or 0)
            size = int(request.query["limit"])
            page = copy.deepcopy(orders[start : start + size])
            for order in page:
                for attachment in order["attachments"]:
                    attachment["size"] = len(self.bytes[str(attachment["id"])])
            meta = {"next": str(start + size) if start + size < len(orders) else None}
            return reply_json({"data": page, "meta": meta})
        parts = request.path.split("/")
        if len(parts) == 8 and parts[3] == "work-orders" and parts[5] == "attachments":
            data = self.bytes.get(parts[6])
            return (
                Reply(200, data, {"Content-Type": "application/octet-stream"})
                if data is not None
                else None
            )
        return None
