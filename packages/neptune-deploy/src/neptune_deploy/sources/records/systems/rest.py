"""A declared REST record API: a CMMS, EAM or other system described by a profile (ADR 0008 §4).

There is no vendor preset here: a vendor's wire format is a preset only once a live tenant has
validated it. A *profile* is the operator's declaration of one API, closed and checked, in JSON::

    {
      "schema": "neptune-deploy.record-profile/1",
      "id": "cmms-example",
      "records": {"path": "/api/v1/work-orders", "items": "/data", "id": "/id",
                  "revision": {"pointer": "/updatedAt", "kind": "updated"}, "name": "/number"},
      "query": {"status": "all"},
      "paging": {"style": "cursor", "limit_param": "limit", "cursor_param": "cursor",
                 "next": "/meta/next"},
      "since": {"param": "updatedAfter"},
      "snapshot": {"format": "csv", "columns": {"WO Number": "/number", "Asset ID": "/asset/id"}},
      "attachments": {"pointer": "/attachments", "id": "/id", "name": "/fileName", "size": "/size",
                      "revision": "/updatedAt",
                      "path": "/api/v1/work-orders/{record}/attachments/{attachment}/download"},
      "auth": {"header": "Session-Token"}
    }

- ``records.items`` points at the array in each page (``""`` for a body that is the array); ``id``
  and ``revision.pointer`` point into each record and must hold text or an integer. The token is
  ``<kind>:<value>`` (``kind`` is ``updated``, ``version`` or ``etag``), the value as written.
- ``paging.style`` is ``cursor`` (``next`` points at the next cursor, absent or null at the end),
  ``offset`` (optional ``total``), ``page`` (from 1) or ``none``. The page size is the declared
  ``page_size`` option, sent as ``limit_param``.
- ``since.param`` names a query parameter that is an *inclusive* lower bound on the revision value
  (``updated >= value``); the cursor is the highest revision value seen, compared as text, so it is
  valid only for ISO 8601 times in one zone or integers of one width. Without ``since`` the API has
  no change feed here and every run lists everything.
- ``snapshot.format`` is ``json`` (the record, in an array of one) or ``csv`` (a header and one row;
  ``columns`` maps each header to a pointer, so a CMMS's JSON gives the columns that a mapping
  file such as ``cmms.generic`` reads).
- ``attachments`` lists each record's attachments from the record itself; the download path is built
  from validated ids, never from a URL the record states.
- ``auth.header`` is the header an API key is sent in (the credential ``api_key``); without it the
  credential is a bearer ``access_token``.
"""

import csv
import io
import re
from collections.abc import Generator, Mapping
from typing import Any, Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.sigv4 import quote
from neptune_deploy.sources.records import jsontext
from neptune_deploy.sources.records.config import (
    Options,
    RecordConfigError,
    cursor_payload,
    cursor_text,
    endpoint_for,
    need,
)
from neptune_deploy.sources.records.http import Api, Auth, ResponseInvalid
from neptune_deploy.sources.records.model import Fetch, Item, Page, Rejected, safe_name
from neptune_deploy.sources.records.systems._pages import flag, text_or_whole, whole
from neptune_deploy.sources.records.systems.spec import Plan, Spec

CONNECTOR_ID: Final = "deploy_rest"
MAX_PAGE_SIZE: Final = 1000
PROFILE_SCHEMA: Final = "neptune-deploy.record-profile/1"
_PROFILE_ID: Final = re.compile(r"[a-z0-9][a-z0-9._\-]{0,62}")
_PATH: Final = re.compile(r"/[A-Za-z0-9._~\-/]{0,512}")
_TEMPLATE: Final = re.compile(
    r"/[A-Za-z0-9._~\-/]{0,400}(\{record\}|\{attachment\})[A-Za-z0-9._~\-/{}]{0,100}"
)
_PARAM: Final = re.compile(r"[A-Za-z][A-Za-z0-9_.\-\[\]]{0,63}")
_HEADER: Final = re.compile(r"[A-Za-z][A-Za-z0-9\-]{0,63}")
_RESERVED_HEADERS: Final = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "accept", "user-agent", "te"}
)
_CURSOR: Final = re.compile(r"[A-Za-z0-9_.~=+/:\-]{1,1024}")
_VALUE: Final = re.compile(r"[^\x00-\x1f\x7f]{1,512}")
_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:@=+\-]{0,255}")
ENV: Final = {"access_token": "NEPTUNE_REST_ACCESS_TOKEN", "api_key": "NEPTUNE_REST_API_KEY"}


def _closed(value: Any, name: str, required: set[str], optional: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecordConfigError(f"profile {name} is an object")
    keys = set(value)
    if not required <= keys or keys - required - optional:
        raise RecordConfigError(
            f"profile {name} has {sorted(keys)}; it needs {sorted(required)}"
            f" and may add {sorted(optional)}"
        )
    return value


def _pointer(value: Any, name: str) -> str:
    if not isinstance(value, str) or not (value == "" or value.startswith("/")) or len(value) > 256:
        raise RecordConfigError(f"profile {name} is a JSON pointer")
    return value


def _word(value: Any, name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RecordConfigError(f"profile {name} is not in the allowed form")
    return value


class Profile:
    """A checked profile. Unknown keys, pointers that are not pointers, and paths that are not paths
    are refused before any request."""

    def __init__(self, raw: Any) -> None:
        top = _closed(
            raw,
            "",
            {"schema", "id", "records", "paging", "snapshot"},
            {"query", "since", "attachments", "auth"},
        )
        if top["schema"] != PROFILE_SCHEMA:
            raise RecordConfigError(f"profile schema is {PROFILE_SCHEMA}")
        self.id = _word(top["id"], "id", _PROFILE_ID)
        records = _closed(top["records"], "records", {"path", "items", "id", "revision"}, {"name"})
        self.path = _word(records["path"], "records.path", _PATH)
        self.items = _pointer(records["items"], "records.items")
        self.record_id = _pointer(records["id"], "records.id")
        revision = _closed(records["revision"], "records.revision", {"pointer", "kind"}, set())
        self.revision = _pointer(revision["pointer"], "records.revision.pointer")
        if revision["kind"] not in ("updated", "version", "etag"):
            raise RecordConfigError("records.revision.kind is updated, version or etag")
        self.kind: str = revision["kind"]
        name = records.get("name")
        self.name = _pointer(name, "records.name") if name is not None else None
        query = top.get("query", {})
        if not isinstance(query, dict) or not all(
            _PARAM.fullmatch(k) and isinstance(v, str) and _VALUE.fullmatch(v)
            for k, v in query.items()
        ):
            raise RecordConfigError("profile query is an object of text parameters")
        self.query = tuple(sorted(query.items()))
        self.paging = self._paging(top["paging"])
        since = top.get("since")
        self.since = (
            _word(_closed(since, "since", {"param"}, set())["param"], "since.param", _PARAM)
            if since is not None
            else None
        )
        self.snapshot = self._snapshot(top["snapshot"])
        self.attachments = self._attachments(top.get("attachments"))
        auth = top.get("auth")
        self.header = None
        if auth is not None:
            header = _word(
                _closed(auth, "auth", {"header"}, set())["header"], "auth.header", _HEADER
            )
            if header.lower() in _RESERVED_HEADERS:
                raise RecordConfigError("auth.header is a header the client sets itself")
            self.header = header
        self.raw: dict[str, JsonValue] = _plain(top)

    @staticmethod
    def _paging(raw: Any) -> dict[str, str]:
        style = raw.get("style") if isinstance(raw, dict) else None
        shapes = {
            "cursor": ({"style", "limit_param", "cursor_param", "next"}, set()),
            "offset": ({"style", "limit_param", "offset_param"}, {"total"}),
            "page": ({"style", "limit_param", "page_param"}, set()),
            "none": ({"style"}, set()),
        }
        if style not in shapes:
            raise RecordConfigError("paging.style is cursor, offset, page or none")
        required, optional = shapes[style]
        paging = _closed(raw, "paging", required, optional)
        out = {"style": style}
        for key, value in paging.items():
            if key != "style":
                out[key] = (
                    _pointer(value, f"paging.{key}")
                    if key in ("next", "total")
                    else _word(value, f"paging.{key}", _PARAM)
                )
        return out

    @staticmethod
    def _snapshot(raw: Any) -> dict[str, Any]:
        fmt = raw.get("format") if isinstance(raw, dict) else None
        if fmt == "json":
            return {"format": "json", **_closed(raw, "snapshot", {"format"}, set())}
        shaped = _closed(raw, "snapshot", {"format", "columns"}, set())
        columns = shaped["columns"]
        if (
            fmt != "csv"
            or not isinstance(columns, dict)
            or not columns
            or len(columns) > 200
            or not all(isinstance(k, str) and k and k.isprintable() for k in columns)
        ):
            raise RecordConfigError("snapshot.format is json, or csv with a columns object")
        return {
            "format": "csv",
            "columns": {k: _pointer(v, "snapshot.columns") for k, v in columns.items()},
        }

    @staticmethod
    def _attachments(raw: Any) -> dict[str, str] | None:
        if raw is None:
            return None
        spec = _closed(
            raw, "attachments", {"pointer", "id", "name", "size", "revision", "path"}, set()
        )
        out = {
            k: _pointer(spec[k], f"attachments.{k}")
            for k in ("pointer", "id", "name", "size", "revision")
        }
        out["path"] = _word(spec["path"], "attachments.path", _TEMPLATE)
        return out


def _plain(value: Any) -> Any:
    """The profile as JSON values, for the transform's config: a changed profile is new lineage."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _profile_option(value: Any) -> Profile:
    if isinstance(value, Profile):
        return value
    return Profile(_from_json(value))


def _from_json(value: Any) -> Any:
    """A declared profile is plain JSON values; refuse anything else."""
    if isinstance(value, bool) or value is None or isinstance(value, str | int | float):
        return value
    if isinstance(value, list):
        return [_from_json(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _from_json(v) for k, v in value.items()}
    raise RecordConfigError("a profile is JSON")


def _plan(authority: str, path: str, options: Options) -> Plan:
    if path:
        raise RecordConfigError("a REST source is rest://<host>; the profile names the path")
    profile = options.extra.get("profile")
    if profile is None:
        raise RecordConfigError("profile: declare the API this source reads")
    return Plan(endpoint_for(authority, options), profile.id)


def _auth(found: Mapping[str, str], options: Options) -> Auth:
    profile: Profile = options.extra["profile"]
    if profile.header is not None:
        need(found, "api_key")
        if "access_token" in found:
            raise RecordConfigError("the profile sends an API key; declare no access token")
        return Auth(profile.header, found["api_key"])
    need(found, "access_token")
    if "api_key" in found:
        raise RecordConfigError("the profile has no auth.header; declare no API key")
    if " " in found["access_token"]:
        raise RecordConfigError("an access token has no spaces")
    return Auth.bearer(found["access_token"])


class RestSystem:
    def __init__(self, api: Api, profile: Profile, page_size: int, attachments: bool) -> None:
        self.api = api
        self.profile = profile
        self.page_size = page_size
        self.attachments = attachments and profile.attachments is not None
        self.declared_options: dict[str, dict[str, JsonValue]] = (
            {"tabular": {"csv_header": "first_row"}} if profile.snapshot["format"] == "csv" else {}
        )

    def pages(self, since: str | None) -> Generator[Page, None, None]:
        profile = self.profile
        bound = cursor_payload(CONNECTOR_ID, since, _VALUE)
        if bound is not None and profile.since is None:
            raise RecordConfigError("the profile declares no since parameter")
        style = profile.paging["style"]
        high = bound
        cursor: str | None = None
        number = 0  # offset or page number
        while True:
            query = list(profile.query)
            if bound is not None and profile.since is not None:
                query.append((profile.since, bound))
            if style != "none":
                query.append((profile.paging["limit_param"], str(self.page_size)))
            if style == "cursor" and cursor is not None:
                query.append((profile.paging["cursor_param"], cursor))
            if style == "offset":
                query.append((profile.paging["offset_param"], str(number)))
            if style == "page":
                query.append((profile.paging["page_param"], str(number + 1)))
            document, _ = self.api.json(profile.path, query)
            rows = jsontext.pointer(document, profile.items)
            if not isinstance(rows, list):
                raise ResponseInvalid("the items pointer does not name an array")
            items: list[Item] = []
            rejected: list[Rejected] = []
            for row in rows:
                value = self._record(row, items, rejected)
                if value is not None and (high is None or value > high):
                    high = value
            following: str | None = None
            more = False
            if style == "cursor":
                raw = jsontext.pointer(document, profile.paging["next"])
                following = (
                    text_or_whole(raw) if raw is not jsontext.MISSING and raw is not None else None
                )
                if raw not in (None, jsontext.MISSING) and (
                    following is None or not _CURSOR.fullmatch(following)
                ):
                    raise ResponseInvalid("the next cursor is not usable")
                more = following is not None
                cursor = following
            elif style in ("offset", "page"):
                number += len(rows) if style == "offset" else 1
                total_pointer = profile.paging.get("total")
                total = (
                    whole(jsontext.pointer(document, total_pointer))
                    if total_pointer is not None
                    else None
                )
                more = bool(rows) and (total is None or number < total)
                if style == "offset" and total is not None and not rows and number < total:
                    yield Page(partial=True)
                    return
                following = str(number) if more else None
            yield Page(
                tuple(items),
                cursor=following,
                resume=cursor_text(CONNECTOR_ID, high)
                if (not more and high is not None and profile.since is not None)
                else None,
                rejected=tuple(rejected),
            )
            if not more:
                return

    def _text(self, row: Any, pointer: str | None) -> str | None:
        if pointer is None:
            return None
        return text_or_whole(jsontext.pointer(row, pointer))

    def _record(self, row: Any, items: list[Item], rejected: list[Rejected]) -> str | None:
        profile = self.profile
        raw = self._text(row, profile.record_id)
        item_id = f"record/{raw or ''}"
        if raw is None or not _ID.fullmatch(raw):
            rejected.append(Rejected(item_id, "id_invalid"))
            return None
        value = self._text(row, profile.revision)
        if value is None or not _VALUE.fullmatch(value):
            rejected.append(Rejected(item_id, "record_invalid"))
            return None
        try:
            body = self._body(row)
        except (jsontext.JsonTextError, UnicodeEncodeError):
            rejected.append(Rejected(item_id, "record_unrepresentable"))
            return None
        extension = "csv" if profile.snapshot["format"] == "csv" else "json"
        stem = self._text(row, profile.name) or raw
        listed = (
            jsontext.pointer(row, profile.attachments["pointer"])
            if self.attachments and profile.attachments is not None
            else None
        )
        items.append(
            Item(
                item_id,
                f"{profile.kind}:{value}",
                safe_name(f"{stem}.{extension}", f"{raw}.{extension}"),
                len(body),
                body=body,
                children=f"{item_id}/attachment/" if isinstance(listed, list) else None,
            )
        )
        for attachment in listed if isinstance(listed, list) else ():
            made = self._attachment(item_id, raw, attachment)
            if isinstance(made, Rejected):
                rejected.append(made)
            else:
                items.append(made)
        return value

    def _body(self, row: Any) -> bytes:
        snapshot = self.profile.snapshot
        if snapshot["format"] == "json":
            return jsontext.dumps([row])
        columns: dict[str, str] = snapshot["columns"]
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(list(columns))
        writer.writerow([jsontext.cell(jsontext.pointer(row, ptr)) for ptr in columns.values()])
        return out.getvalue().encode("utf-8")

    def _attachment(self, parent: str, record_id: str, attachment: Any) -> Item | Rejected:
        spec = self.profile.attachments
        assert spec is not None
        raw = self._text(attachment, spec["id"])
        item_id = f"{parent}/attachment/{raw or ''}"
        if raw is None or not _ID.fullmatch(raw):
            return Rejected(item_id, "id_invalid")
        value = self._text(attachment, spec["revision"])
        if value is None or not _VALUE.fullmatch(value):
            return Rejected(item_id, "record_invalid")
        size = whole(jsontext.pointer(attachment, spec["size"]))
        if size is None:
            return Rejected(item_id, "size_invalid")
        path = (
            spec["path"].replace("{record}", quote(record_id)).replace("{attachment}", quote(raw))
        )
        return Item(
            item_id,
            f"{self.profile.kind}:{value}",
            safe_name(self._text(attachment, spec["name"]), raw),
            size,
            fetch=Fetch(path, (), size),
            parent=parent,
        )

    def download(self, fetch: Fetch) -> bytes:
        return self.api.download(fetch.path, fetch.query, fetch.size)


def _build(api: Api, what: str, options: Options) -> tuple[RestSystem, dict[str, JsonValue]]:
    profile: Profile = options.extra["profile"]
    attachments = bool(options.extra.get("attachments", True))
    system = RestSystem(api, profile, options.page_size or MAX_PAGE_SIZE, attachments)
    return system, {"attachments": attachments, "profile": profile.raw, "system": "rest"}


SPEC: Final = Spec(
    connector_id=CONNECTOR_ID,
    scheme="rest",
    max_page_size=MAX_PAGE_SIZE,
    extras={"profile": _profile_option, "attachments": flag},
    env=ENV,
    since_shape=_VALUE,
    plan=_plan,
    auth=_auth,
    build=_build,
)
