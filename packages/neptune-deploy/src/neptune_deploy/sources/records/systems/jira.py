"""Jira Cloud: a project's issues and their attachments (ADR 0008 §4).

Public API used (Atlassian, "The Jira Cloud platform REST API"): enhanced JQL search ``GET
/rest/api/{2,3}/search/jql`` (``jql``, ``fields``, ``maxResults``, ``nextPageToken``; the
response's ``issues``, ``nextPageToken``, ``isLast``) and ``GET
/rest/api/{2,3}/attachment/content/{id}`` with ``redirect=false`` (the contents inline, so no
redirect to another host is ever needed).

- An issue is ``issue/<numeric id>`` (the key can change when an issue moves; the id cannot). Its
  token is ``updated:<fields.updated>`` as written. Its snapshot is ``[{"key", "fields"}]``, the
  shape the ``ticketing.jira-json`` mapping preset reads.
- An attachment is ``issue/<id>/attachment/<attachment id>``, token ``created:<created>``, its
  parent the issue. It is downloaded by id from the declared site, never from the ``content`` URL
  the record states.
- The feed is ordered ``updated ASC``, so the highest ``updated`` seen is a resume cursor. JQL
  states times in the credential user's zone, to the minute: the cursor's clock reading is taken
  from the very ``updated`` string (which carries that zone's offset) and widened by one day, and
  the ledger's revision tokens discard what was seen already.
- Jira states no deletions: a deleted issue is found by absence from a complete snapshot.
"""

import re
from collections.abc import Generator, Mapping
from datetime import datetime, timedelta
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
from neptune_deploy.sources.records.http import Api, Auth, ResponseInvalid
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

CONNECTOR_ID: Final = "deploy_jira"
MAX_PAGE_SIZE: Final = 100
DEFAULT_FIELDS: Final = (
    "attachment",
    "created",
    "description",
    "environment",
    "issuetype",
    "labels",
    "priority",
    "status",
    "summary",
    "updated",
)
_PROJECT: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_FIELD: Final = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,63}")
_NUMERIC_ID: Final = re.compile(r"[0-9]{1,18}")
_CURSOR: Final = re.compile(
    r"[0-9]{4}-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9](\.[0-9]{1,9})?([+-][0-9][0-9]:?[0-9][0-9]|Z)"
)
ENV: Final = {
    "email": "NEPTUNE_JIRA_EMAIL",
    "api_token": "NEPTUNE_JIRA_API_TOKEN",
    "access_token": "NEPTUNE_JIRA_ACCESS_TOKEN",
}


def _api_version(value: Any) -> str:
    if value not in ("2", "3"):
        raise RecordConfigError('api_version is "2" (plain-text fields) or "3" (rich-text fields)')
    return str(value)


def _plan(authority: str, path: str, options: Options) -> Plan:
    if not _PROJECT.fullmatch(path):
        raise RecordConfigError("not a Jira project key")
    return Plan(endpoint_for(authority, options), path)


def atlassian_auth(found: Mapping[str, str], options: Options) -> Auth:
    if "access_token" in found:
        if "email" in found or "api_token" in found:
            raise RecordConfigError("declare an OAuth access token, or an email and API token")
        return Auth.bearer(found["access_token"])
    need(found, "email", "api_token")
    if ":" in found["email"]:
        raise RecordConfigError("an email has no colon")
    return Auth.basic(found["email"], found["api_token"])


def _instant(updated: str) -> datetime | None:
    try:
        return datetime.fromisoformat(updated)
    except ValueError:
        return None


class JiraSystem:
    def __init__(
        self,
        api: Api,
        project: str,
        fields: tuple[str, ...],
        version: str,
        attachments: bool,
        page_size: int,
    ) -> None:
        self.api = api
        self.declared_options: dict[str, dict[str, JsonValue]] = {}
        self.page_size = page_size
        self.project = project
        self.fields = fields
        self.base = f"/rest/api/{version}"
        self.attachments = attachments

    def _jql(self, since: str | None) -> str:
        jql = f'project = "{self.project}"'
        if since is not None:
            moment = _instant(since)
            if moment is None:
                raise RecordConfigError("since is not a time")
            clock = (moment.replace(tzinfo=None) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
            jql += f' AND updated >= "{clock}"'
        return jql + " ORDER BY updated ASC, key ASC"

    def pages(self, since: str | None) -> Generator[Page, None, None]:
        payload = cursor_payload(CONNECTOR_ID, since, _CURSOR)
        jql = self._jql(payload)
        high: tuple[datetime, str] | None = None
        if payload is not None and (moment := _instant(payload)) is not None:
            high = (moment, payload)
        token: str | None = None
        while True:
            query = [
                ("jql", jql),
                ("maxResults", str(self.page_size)),
                ("fields", ",".join(self.fields)),
            ]
            if token is not None:
                query.append(("nextPageToken", token))
            document, _ = self.api.json(f"{self.base}/search/jql", query)
            root = obj(document)
            issues = array(root.get("issues"))
            token = text(root.get("nextPageToken")) or None
            if token is None and root.get("isLast") is False:
                raise ResponseInvalid("a page says it is not the last and names no next page")
            items: list[Item] = []
            rejected: list[Rejected] = []
            for issue in issues:
                seen = self._issue(issue, items, rejected)
                if seen is not None and (high is None or seen[0] > high[0]):
                    high = seen
            yield Page(
                tuple(items),
                cursor=token,
                resume=cursor_text(CONNECTOR_ID, high[1]) if high is not None else None,
                rejected=tuple(rejected),
            )
            if token is None:
                return

    def _issue(
        self, issue: Any, items: list[Item], rejected: list[Rejected]
    ) -> tuple[datetime, str] | None:
        """Append what ``issue`` gives; return its ``updated`` as a cursor candidate."""
        record = issue if isinstance(issue, dict) else {}
        raw = text_or_whole(record.get("id"))
        if raw is None or not _NUMERIC_ID.fullmatch(raw):
            rejected.append(Rejected(f"issue/{raw or ''}", "id_invalid"))
            return None
        item_id = f"issue/{raw}"
        fields = record.get("fields")
        key = text(record.get("key"))
        updated = text(fields.get("updated")) if isinstance(fields, dict) else None
        if not isinstance(fields, dict) or not key or not updated or not updated.isprintable():
            rejected.append(Rejected(item_id, "record_invalid"))
            return None
        try:
            body = jsontext.dumps([{"fields": fields, "key": key}])
        except jsontext.JsonTextError:
            rejected.append(Rejected(item_id, "record_unrepresentable"))
            return None
        listed = fields.get("attachment") if self.attachments else None
        items.append(
            Item(
                item_id,
                f"updated:{updated}",
                safe_name(f"{key}.json", f"{raw}.json"),
                len(body),
                body=body,
                children=f"{item_id}/attachment/" if isinstance(listed, list) else None,
            )
        )
        for attachment in listed if isinstance(listed, list) else ():
            made = self._attachment(item_id, attachment)
            if isinstance(made, Rejected):
                rejected.append(made)
            else:
                items.append(made)
        moment = _instant(updated)
        return (moment, updated) if moment is not None else None

    def _attachment(self, parent: str, attachment: Any) -> Item | Rejected:
        record = attachment if isinstance(attachment, dict) else {}
        raw = text_or_whole(record.get("id"))
        item_id = f"{parent}/attachment/{raw or ''}"
        created = text(record.get("created"))
        size = whole(record.get("size"))
        if raw is None or not _NUMERIC_ID.fullmatch(raw):
            return Rejected(item_id, "id_invalid")
        if not created or not created.isprintable():
            return Rejected(item_id, "record_invalid")
        if size is None:
            return Rejected(item_id, "size_invalid")
        return Item(
            item_id,
            f"created:{created}",
            safe_name(record.get("filename"), raw),
            size,
            fetch=Fetch(f"{self.base}/attachment/content/{raw}", (("redirect", "false"),), size),
            parent=parent,
        )

    def download(self, fetch: Fetch) -> bytes:
        return self.api.download(fetch.path, fetch.query, fetch.size)


def _build(api: Api, what: str, options: Options) -> tuple[JiraSystem, dict[str, JsonValue]]:
    attachments = bool(options.extra.get("attachments", True))
    payload = cursor_payload(CONNECTOR_ID, options.since, _CURSOR)
    if payload is not None and _instant(payload) is None:
        raise RecordConfigError("since is not a time")
    fields = {*(options.extra.get("fields") or DEFAULT_FIELDS), "updated"}
    fields = fields | {"attachment"} if attachments else fields - {"attachment"}
    version = str(options.extra.get("api_version", "2"))
    ordered = tuple(sorted(fields))
    system = JiraSystem(
        api, what, ordered, version, attachments, options.page_size or MAX_PAGE_SIZE
    )
    config: dict[str, JsonValue] = {
        "api_version": version,
        "attachments": attachments,
        "fields": list(ordered),
        "project": what,
        "system": "jira",
    }
    return system, config


SPEC: Final = Spec(
    connector_id=CONNECTOR_ID,
    scheme="jira",
    max_page_size=MAX_PAGE_SIZE,
    extras={
        "fields": names(_FIELD, "fields"),
        "api_version": _api_version,
        "attachments": flag,
    },
    env=ENV,
    since_shape=_CURSOR,
    plan=_plan,
    auth=atlassian_auth,
    build=_build,
)
