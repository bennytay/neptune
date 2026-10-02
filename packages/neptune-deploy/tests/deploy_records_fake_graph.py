"""An in-process Microsoft Graph drive: the delta feed and the redirecting ``/content`` call
(ADR 0008 §9). Like the other record fakes it serves recorded shapes (``fixtures/records``) over
real HTTP, and the redirect target is a pre-authenticated path on the same loopback server that
refuses any request that carries an ``Authorization`` header."""

import hashlib
import json
from typing import Any

from deploy_records_fake import Backend, Reply, Request, fixture, reply_json


class GraphBackend(Backend):
    auth_value = "Bearer onedrive-token-never-printed"

    def __init__(self) -> None:
        data = fixture("onedrive_items.json")
        self.drive: str = data["drive"]
        self.items: dict[str, dict[str, Any]] = {i["id"]: i for i in data["items"]}
        for item in self.items.values():
            if "content" in item:
                item["content"] = item["content"].encode()
        self.shortcut = data["shortcut"]
        self.log: list[str] = []  # item ids in the order they changed: a delta token indexes it
        self.history: list[dict[str, Any]] = []  # each change as it was stated when it happened
        self.verbatim = False  # a delta that states every change, not the last one per item
        self.link_param = "$skiptoken"  # what a nextLink carries; OneDrive Personal uses "token"
        self.stale: dict[str, dict[str, Any]] = {}  # an older statement to emit before the item
        self.redirect_query: str | None = None  # a raw query for the pre-authenticated URL
        self.redirect_to: str | None = None  # a Location to answer with, instead of the own host
        self.hide_hashes = False
        self.downloads: list[Request] = []

    # --- Edits between syncs -------------------------------------------------------------------

    def edit(self, item_id: str, content: bytes) -> None:
        item = self.items[item_id]
        number = int(item["ctag"].rsplit(",", 1)[1].rstrip('}"')) + 1
        head = item["ctag"].rsplit(",", 1)[0]
        item["content"] = content
        item["ctag"] = f'{head},{number}"'
        self._logged(item_id)

    def rename(self, item_id: str, name: str) -> None:
        """A rename moves the eTag but not the cTag: the bytes are the same."""
        item = self.items[item_id]
        item["name"] = name
        item["etag"] = item["etag"].replace("},", "},9")
        self._logged(item_id)

    def add(self, item_id: str, name: str, content: bytes) -> None:
        self.items[item_id] = {
            "id": item_id, "name": name, "content": content,
            "ctag": f'"c:{{{item_id}}},1"', "etag": f'"{{{item_id}}},1"',
        }  # fmt: skip
        self._logged(item_id)

    def delete(self, item_id: str) -> None:
        self.items[item_id] = {"id": item_id, "deleted": {"state": "deleted"}}
        self._logged(item_id)

    # --- The wire shapes -----------------------------------------------------------------------

    def _logged(self, item_id: str) -> None:
        self.log.append(item_id)
        self.history.append(self._meta(self.items[item_id]))

    def _meta(self, item: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"id": item["id"]}
        if "deleted" in item:
            return {**out, "deleted": item["deleted"]}
        out["name"] = item["name"]
        if "folder" in item:
            return {**out, "folder": item["folder"]}
        content = item["content"]
        hashes: dict[str, str] = {"quickXorHash": "AAAAAAAAAAAAAAAAAAAAAAAAAAA="}
        if not self.hide_hashes:
            hashes["sha1Hash"] = hashlib.sha1(content, usedforsecurity=False).hexdigest().upper()
            hashes["sha256Hash"] = hashlib.sha256(content).hexdigest().upper()
        meta = {
            **out, "size": len(content), "eTag": item["etag"],
            "file": {"mimeType": "application/pdf", "hashes": hashes},
        }  # fmt: skip
        if item["ctag"] is not None:  # a file in a library that states no content tag
            meta["cTag"] = item["ctag"]
        return meta

    def handle(self, request: Request) -> Reply | None:
        base = f"/v1.0/drives/{self.drive}"
        if request.path == f"{base}/root/delta":
            return self._delta(request, base)
        prefix, suffix = f"{base}/items/", "/content"
        if request.path.startswith(prefix) and request.path.endswith(suffix):
            item_id = request.path[len(prefix) : -len(suffix)]
            item = self.items.get(item_id)
            if item is None or "content" not in item:
                return None
            query = self.redirect_query or "tempauth=a%2Bb%3D%3D&e=2026"
            where = self.redirect_to or f"http://{request.headers['host']}/dl/{item_id}?{query}"
            return Reply(302, b"", {"Location": where})
        if request.path.startswith("/dl/"):
            self.downloads.append(request)
            item = self.items.get(request.path[len("/dl/") :])
            return (
                Reply(200, item["content"], {"Content-Type": "application/octet-stream"})
                if item
                else None
            )
        return None

    def authorised(self, request: Request) -> bool:
        if request.path.startswith("/dl/"):  # pre-authenticated: a credential here is a leak
            return "authorization" not in request.headers and "tempauth" in request.query
        return super().authorised(request)

    def _flat(self, kind: str, start: int) -> list[dict[str, Any]]:
        if kind == "i" and self.verbatim:
            return list(self.history[start:])  # every statement, in order, as Graph may
        if kind == "i":  # what changed since log position ``start``
            return [self._meta(self.items[i]) for i in sorted({*self.log[start:]})]
        flat = [self._meta(i) for i in sorted(self.items.values(), key=lambda i: i["id"])]
        flat.append(dict(self.shortcut))
        for item_id, older in self.stale.items():  # an older statement comes first
            flat.insert(next(n for n, m in enumerate(flat) if m["id"] == item_id), older)
        return flat

    def _delta(self, request: Request, base: str) -> Reply:
        """Tokens: ``t<n>`` starts a delta from log position n; ``s<k>`` and ``i<n>.<k>`` continue
        a snapshot or a delta at entry k."""
        size = int(request.query["$top"])
        token = request.query.get("$skiptoken") or request.query.get("token")
        kind, start, index = "s", 0, 0
        if token is not None and token[0] == "t":
            kind, start = "i", int(token[1:])
        elif token is not None and token[0] == "i":
            kind = "i"
            start, index = (int(n) for n in token[1:].split("."))
        elif token is not None:
            index = int(token[1:])
        flat = self._flat(kind, start)
        body: dict[str, Any] = {"value": flat[index : index + size]}
        link = f"http://{request.headers['host']}{base}/root/delta"
        if index + size < len(flat):
            following = f"s{index + size}" if kind == "s" else f"i{start}.{index + size}"
            body["@odata.nextLink"] = f"{link}?{self.link_param}={following}"
        else:
            body["@odata.deltaLink"] = f"{link}?token=t{len(self.log)}"
        return reply_json(body)


class LinearBackend(Backend):
    """The Linear GraphQL endpoint: one ``POST /graphql``, the ``issues`` connection newest first,
    and the workspace the answer is for."""

    auth_header = "Authorization"
    auth_value = "lin_api_key-never-printed"  # a personal key is sent bare
    accepts_post = True

    def __init__(self) -> None:
        data = fixture("linear_issues.json")
        self.workspace: str = data["workspace"]
        self.issues: list[dict[str, Any]] = data["issues"]
        self.documents: list[str] = []

    def edit(self, identifier: str, updated: str, **fields: Any) -> None:
        for issue in self.issues:
            if issue["identifier"] == identifier:
                issue.update(fields, updatedAt=updated)

    def trash(self, identifier: str, updated: str) -> None:
        self.edit(identifier, updated, trashed=True)

    def handle(self, request: Request) -> Reply | None:
        if request.method != "POST" or request.path != "/graphql":
            return None
        asked = json.loads(request.body)
        self.documents.append(asked["query"])
        variables = asked["variables"]
        rows = [i for i in self.issues if i["team"]["key"] == variables["team"]]
        if "since" in variables:
            rows = [i for i in rows if i["updatedAt"] >= variables["since"]]
        rows.sort(key=lambda i: (i["updatedAt"], i["id"]), reverse=True)  # newest first
        start = int(str(variables.get("after") or "cursor:0").split(":")[1])
        size = variables["first"]
        more = start + size < len(rows)
        return reply_json(
            {
                "data": {
                    "organization": {"urlKey": self.workspace},
                    "issues": {
                        "nodes": rows[start : start + size],
                        "pageInfo": {
                            "hasNextPage": more,
                            "endCursor": f"cursor:{start + size}" if more else None,
                        },
                    },
                }
            }
        )
