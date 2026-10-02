"""Google Drive: a drive's binary files, with the Drive change feed (ADR 0008 §4).

Public API used (Google, "Google Drive API v3"): ``files.list`` (``q``, ``pageSize``,
``pageToken``, ``fields``, ``corpora``, ``driveId``, ``supportsAllDrives``; the response's
``files``, ``nextPageToken``, ``incompleteSearch``), ``changes.getStartPageToken``,
``changes.list`` (``pageToken``, ``includeRemoved``; ``changes``, ``nextPageToken``,
``newStartPageToken``) and ``files.get`` with ``alt=media``.

- A file is ``file/<file id>``, token ``version:<version>``: Drive's monotonically increasing
  version number, which "reflects every change made to the file on the server". An edit in place
  is a new token at the same location, so the ledger chains it as a new revision and keeps the old
  one; a version bump over identical bytes (a rename, a share) is no new revision (root ADR 0009).
- Only files with bytes are exported: those with a ``size`` and an ``md5Checksum``. A
  Google-native document has neither until it is exported, so it is a ``type_unsupported``
  finding, not an export of unknown length. A download is checked against the listed size and the
  file's own MD5.
- A snapshot reads ``files.list`` and carries the start page token taken *before* it, so nothing
  changed during the listing is missed. An incremental run reads ``changes.list`` from the cursor:
  a change with ``removed`` or a trashed file is a deletion.
"""

import re
from collections.abc import Generator, Mapping
from typing import Any, Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.transport import Endpoint
from neptune_deploy.sources.records.config import (
    Options,
    RecordConfigError,
    cursor_payload,
    cursor_text,
    need,
)
from neptune_deploy.sources.records.http import Api, Auth, ResponseInvalid
from neptune_deploy.sources.records.model import Fetch, Item, Page, Rejected, safe_name
from neptune_deploy.sources.records.systems._pages import (
    array,
    names,
    obj,
    text,
    text_or_whole,
    whole,
)
from neptune_deploy.sources.records.systems.spec import Plan, Spec

CONNECTOR_ID: Final = "deploy_gdrive"
MAX_PAGE_SIZE: Final = 1000
DEFAULT_ENDPOINT: Final = "https://www.googleapis.com"
MY_DRIVE: Final = "my-drive"
FOLDER: Final = "application/vnd.google-apps.folder"
FILE_FIELDS: Final = "id,name,mimeType,size,md5Checksum,version,trashed"
_DRIVE: Final = re.compile(r"[A-Za-z0-9_\-]{1,128}")
_FILE_ID: Final = re.compile(r"[A-Za-z0-9_\-]{1,128}")
_TOKEN: Final = re.compile(r"[A-Za-z0-9_.~\-]{1,512}")
_MD5: Final = re.compile(r"[0-9a-f]{32}")
_MIME: Final = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+\-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+\-]{0,126}")
ENV: Final = {"access_token": "NEPTUNE_GDRIVE_ACCESS_TOKEN"}


def _endpoint(value: Any) -> str:
    if not isinstance(value, str):
        raise RecordConfigError("endpoint is a URL")
    try:
        Endpoint.parse(value)
    except ValueError as exc:
        raise RecordConfigError(str(exc)) from exc
    return value


def _plan(authority: str, path: str, options: Options) -> Plan:
    if path or not _DRIVE.fullmatch(authority):
        raise RecordConfigError("a Drive source is gdrive://<shared drive id> or gdrive://my-drive")
    if authority == MY_DRIVE and options.instance is None:
        raise RecordConfigError(
            "my-drive is one user's drive, not a name others share: declare an instance name,"
            " which is part of every object's identity"
        )
    declared = options.extra.get("endpoint")
    return Plan(Endpoint.parse(declared or DEFAULT_ENDPOINT), authority, declared is not None)


def _auth(found: Mapping[str, str], options: Options) -> Auth:
    need(found, "access_token")
    if " " in found["access_token"]:
        raise RecordConfigError("an access token has no spaces")
    return Auth.bearer(found["access_token"])


class DriveSystem:
    def __init__(
        self, api: Api, drive: str, mime_types: frozenset[str] | None, page_size: int
    ) -> None:
        self.api = api
        self.declared_options: dict[str, dict[str, JsonValue]] = {}
        self.drive = drive
        self.mime_types = mime_types
        self.page_size = page_size

    @property
    def _scope(self) -> list[tuple[str, str]]:
        shared = [("driveId", self.drive)] if self.drive != MY_DRIVE else []
        return [("supportsAllDrives", "true"), ("includeItemsFromAllDrives", "true"), *shared]

    def pages(self, since: str | None) -> Generator[Page, None, None]:
        token = cursor_payload(CONNECTOR_ID, since, _TOKEN)
        if token is None:
            yield from self._snapshot()
        else:
            yield from self._changes(token)

    def _start_token(self) -> str:
        document, _ = self.api.json(
            "/drive/v3/changes/startPageToken",
            [
                ("supportsAllDrives", "true"),
                *([("driveId", self.drive)] if self.drive != MY_DRIVE else []),
            ],
        )
        start = text(obj(document).get("startPageToken"))
        if start is None or not _TOKEN.fullmatch(start):
            raise ResponseInvalid("no usable start page token")
        return start

    def _snapshot(self) -> Generator[Page, None, None]:
        start = self._start_token()
        page: str | None = None
        corpora = [("corpora", "drive")] if self.drive != MY_DRIVE else []
        while True:
            query = [
                ("q", f"trashed = false and mimeType != '{FOLDER}'"),
                ("pageSize", str(self.page_size)),
                ("fields", f"nextPageToken,incompleteSearch,files({FILE_FIELDS})"),
                *corpora,
                *self._scope,
            ]
            if page is not None:
                query.append(("pageToken", page))
            document, _ = self.api.json("/drive/v3/files", query)
            root = obj(document)
            page = text(root.get("nextPageToken")) or None
            items: list[Item] = []
            rejected: list[Rejected] = []
            for entry in array(root.get("files")):
                self._file(entry, items, rejected)
            yield Page(
                tuple(items),
                cursor=page,
                resume=cursor_text(CONNECTOR_ID, start) if page is None else None,
                rejected=tuple(rejected),
                partial=root.get("incompleteSearch") is True,
            )
            if page is None:
                return

    def _changes(self, token: str) -> Generator[Page, None, None]:
        while True:
            document, _ = self.api.json(
                "/drive/v3/changes",
                [
                    ("pageToken", token),
                    ("pageSize", str(self.page_size)),
                    ("includeRemoved", "true"),
                    (
                        "fields",
                        f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FILE_FIELDS}))",
                    ),
                    *self._scope,
                ],
            )
            root = obj(document)
            following = text(root.get("nextPageToken")) or None
            final = text(root.get("newStartPageToken"))
            if following is None and (final is None or not _TOKEN.fullmatch(final)):
                raise ResponseInvalid("the last page of changes names no new start token")
            if following is not None and not _TOKEN.fullmatch(following):
                raise ResponseInvalid("not a page token")
            events: list[Item | str] = []
            rejected: list[Rejected] = []
            for change in array(root.get("changes")):
                self._change(change, events, rejected)
            resume = following if following is not None else final
            yield Page(
                cursor=following,
                resume=cursor_text(CONNECTOR_ID, resume) if resume else None,
                rejected=tuple(rejected),
                events=tuple(events),  # an edit then a deletion, or the reverse: the last wins
            )
            if following is None:
                return
            token = following

    def _change(self, change: Any, events: list[Item | str], rejected: list[Rejected]) -> None:
        record = change if isinstance(change, dict) else {}
        file = record.get("file")
        file_id = text(record.get("fileId")) or (
            text(file.get("id")) if isinstance(file, dict) else None
        )
        if file_id is None or not _FILE_ID.fullmatch(file_id):
            rejected.append(Rejected(f"file/{file_id or ''}", "id_invalid"))
            return
        if record.get("removed") is True or (
            isinstance(file, dict) and file.get("trashed") is True
        ):
            events.append(f"file/{file_id}")
        elif isinstance(file, dict):
            updated: list[Item] = []
            self._file(file, updated, rejected, later_wins=True)  # the feed is in time order
            events.extend(updated)

    def _file(
        self, entry: Any, items: list[Item], rejected: list[Rejected], *, later_wins: bool = False
    ) -> None:
        record = entry if isinstance(entry, dict) else {}
        raw = text(record.get("id"))
        item_id = f"file/{raw or ''}"
        mime = text(record.get("mimeType"))
        if raw is None or not _FILE_ID.fullmatch(raw):
            rejected.append(Rejected(item_id, "id_invalid"))
            return
        if mime == FOLDER or (
            self.mime_types is not None and mime is not None and mime not in self.mime_types
        ):
            return  # not a document, or not one the operator asked for: no finding
        md5 = text(record.get("md5Checksum"))
        if mime is None or mime.startswith("application/vnd.google-apps.") or md5 is None:
            rejected.append(Rejected(item_id, "type_unsupported"))
            return
        version = text_or_whole(record.get("version"))
        if version is None or not _MD5.fullmatch(md5.lower()):
            rejected.append(Rejected(item_id, "record_invalid"))
            return
        size = whole(record.get("size"))
        if size is None:
            rejected.append(Rejected(item_id, "size_invalid"))
            return
        items.append(
            Item(
                item_id,
                f"version:{version}",
                safe_name(record.get("name"), raw),
                size,
                fetch=Fetch(
                    f"/drive/v3/files/{raw}",
                    (("alt", "media"), ("supportsAllDrives", "true")),
                    size,
                    md5.lower(),
                ),
                later_wins=later_wins,
            )
        )

    def download(self, fetch: Fetch) -> bytes:
        return self.api.download(fetch.path, fetch.query, fetch.size)


def _build(api: Api, what: str, options: Options) -> tuple[DriveSystem, dict[str, JsonValue]]:
    mime = options.extra.get("mime_types")
    system = DriveSystem(
        api, what, frozenset(mime) if mime is not None else None, options.page_size or MAX_PAGE_SIZE
    )
    config: dict[str, JsonValue] = {"drive": what, "system": "gdrive"}
    if mime is not None:
        config["mime_types"] = list(mime)
    return system, config


SPEC: Final = Spec(
    connector_id=CONNECTOR_ID,
    scheme="gdrive",
    max_page_size=MAX_PAGE_SIZE,
    extras={"endpoint": _endpoint, "mime_types": names(_MIME, "mime_types")},
    env=ENV,
    since_shape=_TOKEN,
    plan=_plan,
    auth=_auth,
    build=_build,
)
