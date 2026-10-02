"""ServiceNow: one table's records and their attachments (ADR 0008 §4).

Public API used (ServiceNow, "REST API reference"): the Table API ``GET /api/now/table/{table}``
(``sysparm_query``, ``sysparm_fields``, ``sysparm_limit``, ``sysparm_offset``,
``sysparm_display_value=false``, ``sysparm_exclude_reference_link=true``; the response's
``result`` and the ``X-Total-Count`` header), the Attachment API ``GET /api/now/attachment``
(metadata) and ``GET /api/now/attachment/{sys_id}/file`` (bytes), and the ``sys_audit_delete``
table for deletions.

- A record is ``table/<table>/<sys_id>``, token ``mod_count:<sys_mod_count>@<sys_updated_on>`` as
  written. Its snapshot is a CSV with a header and one row, the columns being exactly the declared
  ``fields`` (the shape the ``ticketing.servicenow-csv`` mapping preset reads). Values are what
  the API states with ``display_value=false``: stored values, not labels.
- An attachment is ``table/<table>/<sys_id>/attachment/<attachment sys_id>``, its parent the
  record. Attachments are listed with one query per batch of records, so the parent's attachment
  list is authoritative for that page.
- The feed is ordered ``sys_updated_on, sys_id``. ``sys_updated_on`` is stored in UTC, and the
  cursor is the highest one seen; the next run re-reads that second (``>=``) and the ledger's
  revision tokens discard what was seen already. An incremental run also reads the table's
  deletions from ``sys_audit_delete`` (it needs read access to it: if denied, the run is
  incomplete and the cursor does not advance). An attachment removed from a record whose own row
  did not change is found by the next snapshot, not by the feed.
"""

import csv
import io
import re
from collections.abc import Generator, Mapping
from typing import Any, Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.records import jsontext
from neptune_deploy.sources.records.config import (
    Options,
    RecordConfigError,
    cursor_payload,
    cursor_text,
    endpoint_for,
    need,
)
from neptune_deploy.sources.records.http import Api, Auth, PaginationLoop
from neptune_deploy.sources.records.model import Fetch, Item, Page, Rejected, safe_name
from neptune_deploy.sources.records.systems._pages import (
    array,
    flag,
    names,
    obj,
    text,
    text_or_whole,
    whole,
)
from neptune_deploy.sources.records.systems.spec import Plan, Spec

CONNECTOR_ID: Final = "deploy_servicenow"
MAX_PAGE_SIZE: Final = 1000
MAX_INNER_PAGES: Final = 10_000
BATCH: Final = 40  # records per attachment query: ids ride in the query string
CHANGE_REQUEST_FIELDS: Final = (
    "approval",
    "approval_set",
    "assignment_group",
    "category",
    "cmdb_ci",
    "description",
    "end_date",
    "number",
    "short_description",
    "start_date",
    "type",
    "u_after",
    "u_before",
    "u_site",
)
_TABLE: Final = re.compile(r"[a-z][a-z0-9_]{0,79}")
_FIELD: Final = re.compile(r"[a-z][a-z0-9_.]{0,79}")
_SYS_ID: Final = re.compile(r"[0-9a-f]{32}")
_UPDATED: Final = re.compile(r"[0-9]{4}-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]")
_QUERY: Final = re.compile(r"[\x20-\x7e]{1,1000}")
ENV: Final = {
    "username": "NEPTUNE_SERVICENOW_USERNAME",
    "password": "NEPTUNE_SERVICENOW_PASSWORD",
    "access_token": "NEPTUNE_SERVICENOW_ACCESS_TOKEN",
}


def _filter(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not _QUERY.fullmatch(value)
        or "ORDERBY" in value.upper()
        or value.startswith("^")
        or value.endswith("^")
    ):
        raise RecordConfigError("filter is an encoded query of printable ASCII, with no ORDERBY")
    return value


def _plan(authority: str, path: str, options: Options) -> Plan:
    if not _TABLE.fullmatch(path):
        raise RecordConfigError("not a ServiceNow table name")
    return Plan(endpoint_for(authority, options), path)


def _auth(found: Mapping[str, str], options: Options) -> Auth:
    if "access_token" in found:
        if "username" in found or "password" in found:
            raise RecordConfigError("declare an OAuth access token, or a user name and password")
        return Auth.bearer(found["access_token"])
    need(found, "username", "password")
    if ":" in found["username"]:
        raise RecordConfigError("a user name has no colon")
    return Auth.basic(found["username"], found["password"])


class ServiceNowSystem:
    def __init__(
        self,
        api: Api,
        table: str,
        columns: tuple[str, ...],
        flt: str | None,
        attachments: bool,
        page_size: int,
    ) -> None:
        self.api = api
        self.declared_options: dict[str, dict[str, JsonValue]] = {
            "tabular": {"csv_header": "first_row"}
        }
        self.table = table
        self.columns = columns
        self.filter = flt
        self.attachments = attachments
        self.page_size = page_size

    def _table_page(
        self, path: str, query: str, fields: str, offset: int, limit: int
    ) -> tuple[list[Any], int | None]:
        document, headers = self.api.json(
            path,
            [
                ("sysparm_query", query),
                ("sysparm_fields", fields),
                ("sysparm_limit", str(limit)),
                ("sysparm_offset", str(offset)),
                ("sysparm_display_value", "false"),
                ("sysparm_exclude_reference_link", "true"),
            ],
        )
        return array(obj(document).get("result")), whole(headers.get("x-total-count"))

    def pages(self, since: str | None) -> Generator[Page, None, None]:
        cursor = cursor_payload(CONNECTOR_ID, since, _UPDATED)
        parts = [self.filter, f"sys_updated_on>={cursor}" if cursor else None]
        query = "^".join(
            part for part in (*parts, "ORDERBYsys_updated_on", "ORDERBYsys_id") if part
        )
        fields = ",".join(sorted({*self.columns, "sys_id", "sys_mod_count", "sys_updated_on"}))
        high = cursor
        offset = 0
        while True:
            rows, total = self._table_page(
                f"/api/now/table/{self.table}", query, fields, offset, self.page_size
            )
            if not rows:
                if total is not None and offset < total:
                    yield Page(partial=True)  # it counted more rows than it gave
                break
            offset += len(rows)
            items: list[Item] = []
            rejected: list[Rejected] = []
            for row in rows:
                updated = self._row(row, items, rejected)
                if updated is not None and (high is None or updated > high):
                    high = updated
            if self.attachments:
                self._attachments(items, rejected)
            more = total is None or offset < total
            # A snapshot's rows arrive oldest first, so the highest time so far is a resume point;
            # an incremental run is complete only after the deletions, which carry its cursor.
            yield Page(
                tuple(items),
                cursor=str(offset) if more else None,
                resume=cursor_text(CONNECTOR_ID, high) if cursor is None and high else None,
                rejected=tuple(rejected),
            )
            if not more:
                break
        if cursor is not None:
            yield self._deletions(cursor, high)

    def _row(self, row: Any, items: list[Item], rejected: list[Rejected]) -> str | None:
        record = row if isinstance(row, dict) else {}
        raw = text(record.get("sys_id"))
        item_id = f"table/{self.table}/{raw or ''}"
        if raw is None or not _SYS_ID.fullmatch(raw):
            rejected.append(Rejected(item_id, "id_invalid"))
            return None
        updated = text(record.get("sys_updated_on"))
        count = text_or_whole(record.get("sys_mod_count"))
        if updated is None or not _UPDATED.fullmatch(updated) or count is None:
            rejected.append(Rejected(item_id, "record_invalid"))
            return None
        try:
            out = io.StringIO()
            writer = csv.writer(out, lineterminator="\n")
            writer.writerow(self.columns)
            writer.writerow([jsontext.cell(record.get(name)) for name in self.columns])
            body = out.getvalue().encode("utf-8")
        except (UnicodeEncodeError, jsontext.JsonTextError):
            rejected.append(Rejected(item_id, "record_unrepresentable"))
            return None
        items.append(
            Item(
                item_id,
                f"mod_count:{count}@{updated}",
                f"{raw}.csv",
                len(body),
                body=body,
                children=f"{item_id}/attachment/" if self.attachments else None,
                later_wins=True,  # ordered by sys_updated_on: a row seen twice is its later state
            )
        )
        return updated

    def _attachments(self, items: list[Item], rejected: list[Rejected]) -> None:
        parents = [item for item in items if item.parent is None]
        for start in range(0, len(parents), BATCH):
            batch = {item.id.rsplit("/", 1)[1]: item.id for item in parents[start : start + BATCH]}
            query = f"table_name={self.table}^table_sys_idIN{','.join(sorted(batch))}^ORDERBYsys_id"
            for row in self._rows(
                "/api/now/attachment",
                query,
                "sys_id,file_name,size_bytes,table_sys_id,sys_mod_count,sys_updated_on",
                1000,
            ):
                made = self._attachment(row, batch)
                if isinstance(made, Rejected):
                    rejected.append(made)
                else:
                    items.append(made)

    def _rows(self, path: str, query: str, fields: str, limit: int) -> Generator[Any, None, None]:
        """Every row of a query, by offset: stops on an empty page, and refuses a system that
        returns a page it already returned (one that ignores the offset) or never ends."""
        offset = 0
        previous: list[Any] | None = None
        for _ in range(MAX_INNER_PAGES):
            rows, _total = self._table_page(path, query, fields, offset, limit)
            if not rows:
                return
            if rows == previous:
                raise PaginationLoop("a page repeats the one before it")
            previous = rows
            offset += len(rows)
            yield from rows
        raise PaginationLoop("a query has too many pages")

    def _attachment(self, row: Any, batch: Mapping[str, str]) -> Item | Rejected:
        record = row if isinstance(row, dict) else {}
        raw = text(record.get("sys_id"))
        parent_key = text(record.get("table_sys_id"))
        parent = batch.get(parent_key or "")
        item_id = (
            f"{parent or 'table/' + self.table + '/' + (parent_key or '')}/attachment/{raw or ''}"
        )
        if raw is None or not _SYS_ID.fullmatch(raw) or parent is None:
            return Rejected(item_id, "id_invalid")
        updated = text(record.get("sys_updated_on"))
        count = text_or_whole(record.get("sys_mod_count"))
        if updated is None or not _UPDATED.fullmatch(updated) or count is None:
            return Rejected(item_id, "record_invalid")
        size = whole(record.get("size_bytes"))
        if size is None:
            return Rejected(item_id, "size_invalid")
        return Item(
            item_id,
            f"mod_count:{count}@{updated}",
            safe_name(record.get("file_name"), raw),
            size,
            fetch=Fetch(f"/api/now/attachment/{raw}/file", (), size),
            parent=parent,
            locator="/table_sys_id",  # the attachment's own record states whose it is
        )

    def _deletions(self, cursor: str, high: str | None) -> Page:
        removed: list[str] = []
        rejected: list[Rejected] = []
        query = f"tablename={self.table}^sys_created_on>={cursor}^ORDERBYsys_created_on"
        for row in self._rows(
            "/api/now/table/sys_audit_delete", query, "documentkey", self.page_size
        ):
            key = text(row.get("documentkey")) if isinstance(row, dict) else None
            item_id = f"table/{self.table}/{key or ''}"
            if key is not None and _SYS_ID.fullmatch(key):
                removed.append(item_id)
            else:
                rejected.append(Rejected(item_id, "id_invalid"))
        return Page(
            removed=tuple(removed),
            rejected=tuple(rejected),
            resume=cursor_text(CONNECTOR_ID, high) if high else None,
        )

    def download(self, fetch: Fetch) -> bytes:
        return self.api.download(fetch.path, fetch.query, fetch.size)


def _build(api: Api, what: str, options: Options) -> tuple[ServiceNowSystem, dict[str, JsonValue]]:
    columns = options.extra.get("fields")
    if columns is None:
        if what != "change_request":
            raise RecordConfigError("fields: declare the columns of this table to export")
        columns = CHANGE_REQUEST_FIELDS
    attachments = bool(options.extra.get("attachments", True))
    flt = options.extra.get("filter")
    system = ServiceNowSystem(
        api, what, tuple(columns), flt, attachments, options.page_size or MAX_PAGE_SIZE
    )
    config: dict[str, JsonValue] = {
        "attachments": attachments,
        "fields": list(columns),
        "system": "servicenow",
        "table": what,
    }
    if flt is not None:
        config["filter"] = flt
    return system, config


SPEC: Final = Spec(
    connector_id=CONNECTOR_ID,
    scheme="servicenow",
    max_page_size=MAX_PAGE_SIZE,
    extras={"fields": names(_FIELD, "fields"), "filter": _filter, "attachments": flag},
    env=ENV,
    since_shape=_UPDATED,
    plan=_plan,
    auth=_auth,
    build=_build,
)
