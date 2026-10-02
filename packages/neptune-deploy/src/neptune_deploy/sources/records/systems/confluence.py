"""Confluence Cloud: a space's current pages as storage-format documents (ADR 0008 §4).

Public API used (Atlassian, "Confluence Cloud REST API v2"): ``GET /wiki/api/v2/pages`` with
``space-id``, ``status=current``, ``body-format=storage``, ``sort=id``, ``limit`` and ``cursor``; the
response's ``results`` (``id``, ``title``, ``version.number``, ``body.storage.value``) and
``_links.next``.

- A page is ``page/<id>``, token ``version:<version.number>``. Its bytes are the page's storage-format
  body, as written (XHTML with Confluence's ``ac:`` elements), named ``<title>.xhtml``. A page's
  title and version are identity and hints, not bytes.
- ``_links.next`` is read only for its ``cursor`` parameter: the next request is built here, never
  taken from a URL the response states.
- There is no change feed: Confluence Cloud v2 states neither a changes cursor nor deletions, so every
  run lists the space, and a page absent from a complete listing is gone.
- Attachments are not exported. Their download is a redirect to another host, which a read-only
  connector never follows (ADR 0006 §6), and the v2 API has no inline form of it.
"""

import re
import urllib.parse
from collections.abc import Generator
from typing import Any, Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.records.config import Options, RecordConfigError, endpoint_for
from neptune_deploy.sources.records.http import Api, ResponseInvalid
from neptune_deploy.sources.records.model import Fetch, Item, Page, Rejected, safe_name
from neptune_deploy.sources.records.systems._pages import array, obj, text, text_or_whole, whole
from neptune_deploy.sources.records.systems.jira import atlassian_auth
from neptune_deploy.sources.records.systems.spec import Plan, Spec

CONNECTOR_ID: Final = "deploy_confluence"
MAX_PAGE_SIZE: Final = 250
_SPACE: Final = re.compile(r"[0-9]{1,18}")
_PAGE_ID: Final = re.compile(r"[0-9]{1,18}")
_CURSOR: Final = re.compile(r"[A-Za-z0-9_.~=+/:\-]{1,1024}")
ENV: Final = {
    "email": "NEPTUNE_CONFLUENCE_EMAIL",
    "api_token": "NEPTUNE_CONFLUENCE_API_TOKEN",
    "access_token": "NEPTUNE_CONFLUENCE_ACCESS_TOKEN",
}


def _plan(authority: str, path: str, options: Options) -> Plan:
    if not _SPACE.fullmatch(path):
        raise RecordConfigError(f"not a Confluence space id (numeric): {path!r}")
    return Plan(endpoint_for(authority, options), path)


class ConfluenceSystem:
    def __init__(self, api: Api, space: str, page_size: int) -> None:
        self.api = api
        self.space = space
        self.page_size = page_size

    def pages(self, since: str | None) -> Generator[Page, None, None]:
        cursor: str | None = None
        while True:
            query = [
                ("space-id", self.space),
                ("status", "current"),
                ("body-format", "storage"),
                ("sort", "id"),
                ("limit", str(self.page_size)),
            ]
            if cursor is not None:
                query.append(("cursor", cursor))
            document, _ = self.api.json("/wiki/api/v2/pages", query)
            root = obj(document)
            cursor = self._next(root)
            items: list[Item] = []
            rejected: list[Rejected] = []
            for entry in array(root.get("results")):
                self._page(entry, items, rejected)
            yield Page(tuple(items), cursor=cursor, rejected=tuple(rejected))
            if cursor is None:
                return

    @staticmethod
    def _next(root: dict[str, Any]) -> str | None:
        links = root.get("_links")
        following = text(links.get("next")) if isinstance(links, dict) else None
        if following is None:
            return None
        found = urllib.parse.parse_qs(urllib.parse.urlsplit(following).query).get("cursor", [])
        if len(found) != 1 or not _CURSOR.fullmatch(found[0]):
            raise ResponseInvalid("a next link without a usable cursor")
        return found[0]

    def _page(self, entry: Any, items: list[Item], rejected: list[Rejected]) -> None:
        record = entry if isinstance(entry, dict) else {}
        raw = text_or_whole(record.get("id"))
        item_id = f"page/{raw or ''}"
        if raw is None or not _PAGE_ID.fullmatch(raw):
            rejected.append(Rejected(item_id, "id_invalid"))
            return
        version = record.get("version")
        number = whole(version.get("number")) if isinstance(version, dict) else None
        body = record.get("body")
        storage = body.get("storage") if isinstance(body, dict) else None
        stored = storage if isinstance(storage, dict) else {}
        value = text(stored.get("value"))
        if number is None or value is None or stored.get("representation") != "storage":
            rejected.append(Rejected(item_id, "record_invalid"))
            return
        try:
            data = value.encode("utf-8")
        except UnicodeEncodeError:
            rejected.append(Rejected(item_id, "record_unrepresentable"))
            return
        items.append(
            Item(
                item_id,
                f"version:{number}",
                safe_name(f"{text(record.get('title')) or raw}.xhtml", f"{raw}.xhtml"),
                len(data),
                body=data,
            )
        )

    def download(self, fetch: Fetch) -> bytes:
        raise NotImplementedError("pages are held from their listing; none has a download")


def _build(api: Api, what: str, options: Options) -> tuple[ConfluenceSystem, dict[str, JsonValue]]:
    if options.since is not None:
        raise RecordConfigError("Confluence has no change feed: every run lists the space")
    system = ConfluenceSystem(api, what, options.page_size or MAX_PAGE_SIZE)
    return system, {"space": what, "system": "confluence"}


SPEC: Final = Spec(
    connector_id=CONNECTOR_ID,
    scheme="confluence",
    max_page_size=MAX_PAGE_SIZE,
    extras={},
    env=ENV,
    since_shape=_CURSOR,
    plan=_plan,
    auth=atlassian_auth,
    build=_build,
)
