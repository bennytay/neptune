"""Linear: a team's issues (ADR 0008 §4).

Public API used (Linear, "GraphQL API"): one endpoint, ``POST https://api.linear.app/graphql``,
with the ``issues`` connection (``first``, ``after``, ``includeArchived``, ``orderBy``, ``filter``;
``nodes`` and ``pageInfo``). Linear has no REST form, so this is the one connector that sends
``POST``. It sends GraphQL *queries* only: the two documents below are constants of this module,
the variables ride in the JSON body, and ``Api.graphql`` refuses any document that is not a query
(ADR 0008 §6).

- An issue is ``issue/<uuid>`` (the identifier, ``OPS-12``, changes when an issue moves between
  teams; the id does not), token ``updated:<updatedAt>`` as written. Its snapshot is a CSV with a
  header and one row, in the columns of Linear's own CSV export (the shape the
  ``ticketing.linear-csv`` mapping preset reads): values as the API states them, a missing value a
  blank cell.
- Identity says whose issues they are: the workspace's URL key, from the URL. Every response states
  the workspace it answered for (``organization.urlKey``), and a response for another workspace
  stops the listing, so a credential for the wrong workspace never writes into this one's scope.
- Linear orders by ``updatedAt`` newest first, so a run that stopped part-way has seen the newest
  issues and not the oldest: only a run that read every page states a cursor (the highest
  ``updatedAt`` seen), and a run that stopped leaves the previous cursor. The next run re-reads
  that instant (``gte``) and the ledger's revision tokens discard what was seen already.
- An issue in the trash (``trashed``) is a deletion, as the system states it. An archived issue is
  not: archiving is a state Linear lists (``includeArchived``), and is not read as an absence.
- Linear's ``attachments`` are references to URLs elsewhere, not files, and the files a description
  embeds sit on another host behind the same credential. Neither is exported.
- Linear reports a rate limit as ``400`` with its state in ``X-RateLimit-*`` headers (the error body
  is never read), which ``RecordTransport`` maps to ``rate_limited``. It states ``Retry-After`` as
  an epoch time, a wall-clock reading this client does not use.
"""

import csv
import io
import re
from collections.abc import Generator, Mapping
from typing import Any, Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.transport import Endpoint
from neptune_deploy.sources.records import jsontext
from neptune_deploy.sources.records.config import (
    Options,
    RecordConfigError,
    cursor_payload,
    cursor_text,
    need,
)
from neptune_deploy.sources.records.http import Api, Auth, ResponseInvalid
from neptune_deploy.sources.records.model import Fetch, Item, Page, Rejected
from neptune_deploy.sources.records.systems._pages import array, obj, text
from neptune_deploy.sources.records.systems.spec import Plan, Spec

CONNECTOR_ID: Final = "deploy_linear"
MAX_PAGE_SIZE: Final = 250
DEFAULT_ENDPOINT: Final = "https://api.linear.app"
GRAPHQL_PATH: Final = "/graphql"
COLUMNS: Final = (
    "ID",
    "Team",
    "Title",
    "Status",
    "Priority",
    "Assignee",
    "Labels",
    "Created",
    "Description",
    "Parent issue",
    "Started",
    "Completed",
)
_WORKSPACE: Final = re.compile(r"[a-z0-9][a-z0-9\-]{0,62}")
_TEAM: Final = re.compile(r"[A-Z][A-Z0-9]{0,15}")
_ISSUE_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_IDENTIFIER: Final = re.compile(r"[A-Z][A-Z0-9]{0,15}-[0-9]{1,9}")
_INSTANT: Final = re.compile(
    r"[0-9]{4}-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]\.[0-9]{3}Z"
)
_AFTER: Final = re.compile(r"[\x21-\x7e]{1,512}")
ENV: Final = {"api_key": "NEPTUNE_LINEAR_API_KEY", "access_token": "NEPTUNE_LINEAR_ACCESS_TOKEN"}

_NODE: Final = """
    nodes {
      id identifier title description createdAt updatedAt startedAt completedAt trashed
      priorityLabel state { name } assignee { name } team { key }
      labels { nodes { name } } parent { identifier }
    }
    pageInfo { hasNextPage endCursor }
"""
SNAPSHOT_QUERY: Final = (
    "query Issues($team: String!, $first: Int!, $after: String) {\n"
    "  organization { urlKey }\n"
    "  issues(first: $first, after: $after, includeArchived: true, orderBy: updatedAt,\n"
    "         filter: { team: { key: { eq: $team } } }) {" + _NODE + "  }\n}\n"
)
CHANGES_QUERY: Final = (
    "query IssueChanges($team: String!, $first: Int!, $after: String,\n"
    "                    $since: DateTimeOrDuration) {\n"
    "  organization { urlKey }\n"
    "  issues(first: $first, after: $after, includeArchived: true, orderBy: updatedAt,\n"
    "         filter: { team: { key: { eq: $team } }, updatedAt: { gte: $since } }) {"
    + _NODE
    + "  }\n}\n"
)


def _endpoint(value: Any) -> str:
    if not isinstance(value, str):
        raise RecordConfigError("endpoint is a URL")
    try:
        Endpoint.parse(value)
    except ValueError as exc:
        raise RecordConfigError(str(exc)) from exc
    return value


def _plan(authority: str, path: str, options: Options) -> Plan:
    if not _WORKSPACE.fullmatch(authority) or not _TEAM.fullmatch(path):
        raise RecordConfigError("a Linear source is linear://<workspace url key>/<TEAM KEY>")
    declared = options.extra.get("endpoint")
    return Plan(Endpoint.parse(declared or DEFAULT_ENDPOINT), path, False, instance=authority)


def _auth(found: Mapping[str, str], options: Options) -> Auth:
    if "api_key" in found and "access_token" in found:
        raise RecordConfigError("declare a Linear API key, or an OAuth access token")
    if "access_token" in found:
        return Auth.bearer(found["access_token"])
    need(found, "api_key")
    return Auth("Authorization", found["api_key"])  # a personal key is sent bare, never as Bearer


class LinearSystem:
    def __init__(self, api: Api, workspace: str, team: str, page_size: int) -> None:
        self.api = api
        self.declared_options: dict[str, dict[str, JsonValue]] = {
            "tabular": {"csv_header": "first_row"}
        }
        self.workspace = workspace
        self.team = team
        self.page_size = page_size

    def pages(self, since: str | None) -> Generator[Page, None, None]:
        payload = cursor_payload(CONNECTOR_ID, since, _INSTANT)
        after: str | None = None
        high: str | None = None
        while True:
            variables: dict[str, Any] = {"team": self.team, "first": self.page_size, "after": after}
            if payload is not None:
                variables["since"] = payload
            document, _ = self.api.graphql(
                GRAPHQL_PATH, SNAPSHOT_QUERY if payload is None else CHANGES_QUERY, variables
            )
            root = obj(document)
            if root.get("errors"):
                raise ResponseInvalid("the answer states errors")
            data = obj(root.get("data"))
            if text(obj(data.get("organization")).get("urlKey")) != self.workspace:
                raise ResponseInvalid("the answer is for another workspace")
            issues = obj(data.get("issues"))
            info = obj(issues.get("pageInfo"))
            end = text(info.get("endCursor"))
            more = info.get("hasNextPage") is True
            if more and (end is None or not _AFTER.fullmatch(end)):
                raise ResponseInvalid("a page says more follows and names no usable cursor")
            items: list[Item] = []
            rejected: list[Rejected] = []
            removed: list[str] = []
            for node in array(issues.get("nodes")):
                updated = self._issue(node, items, rejected, removed)
                if updated is not None and (high is None or updated > high):
                    high = updated
            yield Page(
                tuple(items),
                cursor=end if more else None,
                resume=cursor_text(CONNECTOR_ID, high) if not more and high is not None else None,
                removed=tuple(removed),
                rejected=tuple(rejected),
            )
            if not more:
                return
            after = end

    def _issue(
        self, node: Any, items: list[Item], rejected: list[Rejected], removed: list[str]
    ) -> str | None:
        """Append what ``node`` gives; return its ``updatedAt`` as a cursor candidate."""
        record = node if isinstance(node, dict) else {}
        raw = text(record.get("id"))
        item_id = f"issue/{raw or ''}"
        if raw is None or not _ISSUE_ID.fullmatch(raw):
            rejected.append(Rejected(item_id, "id_invalid"))
            return None
        updated = text(record.get("updatedAt"))
        identifier = text(record.get("identifier"))
        if (
            updated is None
            or not _INSTANT.fullmatch(updated)
            or identifier is None
            or not _IDENTIFIER.fullmatch(identifier)
        ):
            rejected.append(Rejected(item_id, "record_invalid"))
            return None
        if record.get("trashed") is True:
            removed.append(item_id)
            return updated
        try:
            row = self._row(record, identifier)
            out = io.StringIO()
            writer = csv.writer(out, lineterminator="\n")
            writer.writerow(COLUMNS)
            writer.writerow(row)
            body = out.getvalue().encode("utf-8")
        except (UnicodeEncodeError, jsontext.JsonTextError):
            rejected.append(Rejected(item_id, "record_unrepresentable"))
            return None
        items.append(Item(item_id, f"updated:{updated}", f"{identifier}.csv", len(body), body=body))
        return updated

    @staticmethod
    def _row(record: dict[str, Any], identifier: str) -> list[str]:
        def inner(name: str, key: str) -> str:
            value = record.get(name)
            return jsontext.cell(value.get(key)) if isinstance(value, dict) else ""

        labels = record.get("labels")
        nodes = labels.get("nodes") if isinstance(labels, dict) else None
        names = (
            [jsontext.cell(n.get("name")) for n in nodes if isinstance(n, dict)]
            if isinstance(nodes, list)
            else []
        )
        return [
            identifier,
            inner("team", "key"),
            jsontext.cell(record.get("title")),
            inner("state", "name"),
            jsontext.cell(record.get("priorityLabel")),
            inner("assignee", "name"),
            ",".join(names),
            jsontext.cell(record.get("createdAt")),
            jsontext.cell(record.get("description")),
            inner("parent", "identifier"),
            jsontext.cell(record.get("startedAt")),
            jsontext.cell(record.get("completedAt")),
        ]

    def download(self, fetch: Fetch) -> bytes:
        raise NotImplementedError("issues are held from their listing; none has a download")


def _build(api: Api, what: str, options: Options) -> tuple[LinearSystem, dict[str, JsonValue]]:
    workspace = options.named
    assert workspace is not None  # _plan names it
    system = LinearSystem(api, workspace, what, options.page_size or MAX_PAGE_SIZE)
    return system, {"system": "linear", "team": what, "workspace": workspace}


SPEC: Final = Spec(
    connector_id=CONNECTOR_ID,
    scheme="linear",
    max_page_size=MAX_PAGE_SIZE,
    extras={"endpoint": _endpoint},
    env=ENV,
    since_shape=_INSTANT,
    plan=_plan,
    auth=_auth,
    build=_build,
)
