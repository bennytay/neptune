"""OneDrive and SharePoint document libraries: a drive's files, with the delta feed (ADR 0008 §4).

Public API used (Microsoft, "Microsoft Graph v1.0"): ``GET /drives/{drive-id}/root/delta`` (the
response's ``value`` of driveItems, ``@odata.nextLink`` and ``@odata.deltaLink``) and ``GET
/drives/{drive-id}/items/{item-id}/content``, which answers ``302`` to a short-lived,
pre-authenticated download URL. A SharePoint document library is a drive too, so ``onedrive://``
names either: a personal or business OneDrive, or a library by its Graph drive id.

- A file is ``item/<driveItem id>``, token ``ctag:<cTag>``: the content tag, which changes when the
  bytes do and not on a rename or share (the ``eTag`` also changes with metadata, so identical
  bytes would look like a new revision on every touch). If a file states no ``cTag``, its token is
  ``etag:<eTag>``. An edit in place is a new token at the same location, so the ledger chains it
  as a new revision and keeps the old one (root ADR 0009).
- One feed does both jobs. A snapshot pages ``root/delta`` from the start; its last page's
  ``@odata.deltaLink`` carries the ``token`` that an incremental run sends back. A driveItem with a
  ``deleted`` facet is a deletion. Folders, packages and shortcuts (``remoteItem``) are not files
  and are left out without a finding. Graph states an item more than once if it changed while
  paging, and the last statement is the item (``Item.later_wins``).
- A next page is built from the link's ``$skiptoken`` or ``token`` parameter only, never requested
  from the URL the response states.
- Bytes come from the pre-authenticated URL the ``/content`` call redirects to, which is on another
  host. It is followed once, to a host the operator allows (``download_hosts``; default Microsoft's
  own domains), with no credential and no further redirect (``http.Api.download_redirected``).
  A download is checked against the listed size and the file's own ``sha256Hash`` or ``sha1Hash``
  if it states one. ``quickXorHash`` is not checked: it is not a cryptographic hash, and the
  compiler fingerprints every body with sha256.
"""

import re
import urllib.parse
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
from neptune_deploy.sources.records.systems._pages import array, obj, text, whole
from neptune_deploy.sources.records.systems.spec import Plan, Spec

CONNECTOR_ID: Final = "deploy_onedrive"
MAX_PAGE_SIZE: Final = 200
DEFAULT_ENDPOINT: Final = "https://graph.microsoft.com"
DEFAULT_DOWNLOAD_HOSTS: Final = (
    "1drv.com",
    "microsoftpersonalcontent.com",
    "sharepoint.cn",
    "sharepoint.com",
    "sharepoint.de",
    "sharepoint.us",
)
_DRIVE: Final = re.compile(r"[A-Za-z0-9!_\-]{1,200}")
_ITEM: Final = re.compile(r"[A-Za-z0-9!_\-]{1,128}")
_TOKEN: Final = re.compile(r"[\x21-\x7e]{1,2048}")
_HASH: Final = {
    "sha1Hash": re.compile(r"[0-9A-Fa-f]{40}"),
    "sha256Hash": re.compile(r"[0-9A-Fa-f]{64}"),
}
_HOST: Final = re.compile(r"[a-z0-9][a-z0-9.\-]{0,252}")
ENV: Final = {"access_token": "NEPTUNE_ONEDRIVE_ACCESS_TOKEN"}


def _endpoint(value: Any) -> str:
    if not isinstance(value, str):
        raise RecordConfigError("endpoint is a URL")
    try:
        Endpoint.parse(value)
    except ValueError as exc:
        raise RecordConfigError(str(exc)) from exc
    return value


def _hosts(value: Any) -> tuple[str, ...]:
    """Domains (and their subdomains) a download may be redirected to. A bare top-level name
    would allow a whole registry, so a name has a dot, or is ``localhost``."""
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 50
        or not all(
            isinstance(v, str) and _HOST.fullmatch(v) and ("." in v or v == "localhost")
            for v in value
        )
    ):
        raise RecordConfigError("download_hosts is a non-empty list of domain names, at most 50")
    return tuple(sorted(set(value)))


def _plan(authority: str, path: str, options: Options) -> Plan:
    if path or not _DRIVE.fullmatch(authority):
        raise RecordConfigError("a OneDrive source is onedrive://<drive id>")
    declared = options.extra.get("endpoint")
    return Plan(Endpoint.parse(declared or DEFAULT_ENDPOINT), authority, declared is not None)


def _auth(found: Mapping[str, str], options: Options) -> Auth:
    need(found, "access_token")
    if " " in found["access_token"]:
        raise RecordConfigError("an access token has no spaces")
    return Auth.bearer(found["access_token"])


def _link_token(root: dict[str, Any], name: str) -> tuple[str, str] | None:
    """``(parameter, value)`` of the continuation in ``root[name]``, or ``None`` if absent."""
    link = text(root.get(name))
    if link is None:
        return None
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
    found = [(key, values) for key, values in query.items() if key in ("$skiptoken", "token")]
    if len(found) != 1 or len(found[0][1]) != 1 or not _TOKEN.fullmatch(found[0][1][0]):
        raise ResponseInvalid("a link without a usable token")
    return found[0][0], found[0][1][0]


class OneDriveSystem:
    def __init__(self, api: Api, drive: str, hosts: tuple[str, ...], page_size: int) -> None:
        self.api = api
        self.declared_options: dict[str, dict[str, JsonValue]] = {}
        self.drive = drive
        self.hosts = hosts
        self.page_size = page_size

    def pages(self, since: str | None) -> Generator[Page, None, None]:
        token = cursor_payload(CONNECTOR_ID, since, _TOKEN)
        step: tuple[str, str] | None = ("token", token) if token is not None else None
        while True:
            query = [("$top", str(self.page_size))]
            if step is not None:
                query.append(step)
            document, _ = self.api.json(f"/v1.0/drives/{self.drive}/root/delta", query)
            root = obj(document)
            following = _link_token(root, "@odata.nextLink")
            final = _link_token(root, "@odata.deltaLink")
            if following is None and final is None:
                raise ResponseInvalid("a page names neither a next page nor a delta link")
            if following is not None and final is not None:
                raise ResponseInvalid("a page names both a next page and a delta link")
            items: list[Item] = []
            rejected: list[Rejected] = []
            removed: list[str] = []
            for entry in array(root.get("value")):
                self._entry(entry, items, rejected, removed)
            yield Page(
                tuple(items),
                cursor=following[1] if following is not None else None,
                resume=cursor_text(CONNECTOR_ID, final[1]) if final is not None else None,
                removed=tuple(removed),
                rejected=tuple(rejected),
            )
            if following is None:
                return
            step = following

    def _entry(
        self, entry: Any, items: list[Item], rejected: list[Rejected], removed: list[str]
    ) -> None:
        record = entry if isinstance(entry, dict) else {}
        raw = text(record.get("id"))
        item_id = f"item/{raw or ''}"
        if raw is None or not _ITEM.fullmatch(raw):
            rejected.append(Rejected(item_id, "id_invalid"))
            return
        if isinstance(record.get("deleted"), dict):
            removed.append(item_id)
            return
        facet = record.get("file")
        if not isinstance(facet, dict):
            return  # a folder, package or shortcut: not a document, and no finding
        ctag, etag = text(record.get("cTag")), text(record.get("eTag"))
        token = f"ctag:{ctag}" if ctag else f"etag:{etag}" if etag else None
        if token is None or not token.isprintable():
            rejected.append(Rejected(item_id, "record_invalid"))
            return
        size = whole(record.get("size"))
        if size is None:
            rejected.append(Rejected(item_id, "size_invalid"))
            return
        hashes = facet.get("hashes")
        stated = hashes if isinstance(hashes, dict) else {}
        digests: dict[str, str] = {}
        for graph_name, pattern in _HASH.items():
            value = text(stated.get(graph_name))
            if value is not None:
                if not pattern.fullmatch(value):
                    rejected.append(Rejected(item_id, "record_invalid"))
                    return
                digests[graph_name.removesuffix("Hash")] = value.lower()
        items.append(
            Item(
                item_id,
                token,
                safe_name(record.get("name"), raw),
                size,
                fetch=Fetch(
                    f"/v1.0/drives/{self.drive}/items/{raw}/content",
                    (),
                    size,
                    sha1=digests.get("sha1"),
                    sha256=digests.get("sha256"),
                    redirect=True,
                ),
                later_wins=True,
            )
        )

    def download(self, fetch: Fetch) -> bytes:
        return self.api.download_redirected(fetch.path, fetch.query, fetch.size, self.hosts)


def _build(api: Api, what: str, options: Options) -> tuple[OneDriveSystem, dict[str, JsonValue]]:
    hosts = tuple(options.extra.get("download_hosts") or DEFAULT_DOWNLOAD_HOSTS)
    system = OneDriveSystem(api, what, hosts, options.page_size or MAX_PAGE_SIZE)
    config: dict[str, JsonValue] = {
        "download_hosts": list(hosts),
        "drive": what,
        "system": "onedrive",
    }
    return system, config


SPEC: Final = Spec(
    connector_id=CONNECTOR_ID,
    scheme="onedrive",
    max_page_size=MAX_PAGE_SIZE,
    extras={"endpoint": _endpoint, "download_hosts": _hosts},
    env=ENV,
    since_shape=_TOKEN,
    plan=_plan,
    auth=_auth,
    build=_build,
)
