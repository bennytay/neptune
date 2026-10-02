"""Ordered change feeds, throttling, relation provenance and the raw redirect query (ADR 0008):
the cases a PR review found, each with an exact expectation."""

from pathlib import Path

import pytest

from deploy_records_fake import (
    DriveBackend,
    FakeServer,
    JiraBackend,
    Reply,
    RestBackend,
    ServiceNowBackend,
    reply_json,
)
from deploy_records_fake_graph import GraphBackend
from deploy_records_support import codes, gdrive, jira, onedrive, rest, servicenow
from neptune.identity.revisions import SourceLedger
from neptune_deploy.sources.records import Relation

DRIVE_FILE = "file/file_sop_dock"
GRAPH_ITEM = "item/01SOPDOCK00000001"


# --- A deletion and an update of one id in one feed: the last statement wins -----------------


@pytest.mark.parametrize("size", [1, 2, 1000])  # a size of 1 puts them across a page boundary
def test_drive_an_edit_then_a_deletion_is_a_deletion(tmp_path: Path, size: int) -> None:
    backend = DriveBackend()
    server = FakeServer(backend)
    backend.edit("file_sop_dock", b"%PDF-1.4 SOP dock charging v8")
    backend.remove("file_sop_dock")
    with gdrive(server, tmp_path, since="deploy_gdrive/1:0", page_size=size) as source:
        listing = source.listing()
        assert DRIVE_FILE in listing.removed
        assert DRIVE_FILE not in [e.id for e in listing.entries]
        assert codes(source) == []


@pytest.mark.parametrize("size", [1, 2, 1000])
def test_drive_a_deletion_then_a_restore_is_live(tmp_path: Path, size: int) -> None:
    backend = DriveBackend()
    server = FakeServer(backend)
    backend.remove("file_sop_dock")
    backend.add("file_sop_dock", "SOP dock charging.pdf", b"%PDF-1.4 SOP restored")
    with gdrive(server, tmp_path, since="deploy_gdrive/1:0", page_size=size) as source:
        listing = source.listing()
        assert DRIVE_FILE not in listing.removed
        entry = next(e for e in listing.entries if e.id == DRIVE_FILE)
        with source.open(entry.location) as stream:
            assert stream.read() == b"%PDF-1.4 SOP restored"


def test_drive_a_deleted_file_is_gone_in_the_ledger_even_if_edited_in_the_same_page(
    tmp_path: Path,
) -> None:
    backend = DriveBackend()
    server = FakeServer(backend)
    ledger = SourceLedger()
    from deploy_records_support import fingerprint

    with gdrive(server, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
        cursor = source.cursor
    backend.edit("file_sop_dock", b"%PDF-1.4 SOP dock charging v8")
    backend.remove("file_sop_dock")
    with gdrive(server, tmp_path, since=cursor, ledger=ledger) as source:
        assert [g.location.key[2].rsplit("/", 1)[1] for g in source.discover(ledger).gone] == [
            "file_sop_dock"
        ]


@pytest.mark.parametrize("size", [1, 2, 200])
def test_graph_an_edit_then_a_deletion_is_a_deletion(tmp_path: Path, size: int) -> None:
    backend = GraphBackend()
    backend.verbatim = True
    server = FakeServer(backend)
    backend.edit("01SOPDOCK00000001", b"%PDF-1.4 SOP dock charging v8")
    backend.delete("01SOPDOCK00000001")
    with onedrive(server, tmp_path, since="deploy_onedrive/1:t0", page_size=size) as source:
        listing = source.listing()
        assert GRAPH_ITEM in listing.removed
        assert GRAPH_ITEM not in [e.id for e in listing.entries]


@pytest.mark.parametrize("size", [1, 2, 200])
def test_graph_a_deletion_then_a_restore_is_live(tmp_path: Path, size: int) -> None:
    backend = GraphBackend()
    backend.verbatim = True
    server = FakeServer(backend)
    backend.delete("01SOPDOCK00000001")
    backend.add("01SOPDOCK00000001", "SOP restored.pdf", b"%PDF-1.4 SOP restored")
    with onedrive(server, tmp_path, since="deploy_onedrive/1:t0", page_size=size) as source:
        listing = source.listing()
        assert GRAPH_ITEM not in listing.removed
        entry = next(e for e in listing.entries if e.id == GRAPH_ITEM)
        with source.open(entry.location) as stream:
            assert stream.read() == b"%PDF-1.4 SOP restored"


# --- Throttling ------------------------------------------------------------------------------


def quota(reason: str) -> Reply:
    body = {"error": {"code": 403, "errors": [{"reason": reason, "domain": "usageLimits"}]}}
    reply = reply_json(body, 403)
    reply.headers["Retry-After"] = "30"
    return reply


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded"])
def test_drive_a_403_that_states_a_quota_reason_is_rate_limited(
    tmp_path: Path, reason: str
) -> None:
    server = FakeServer(DriveBackend())
    server.inject(lambda r: r.path == "/drive/v3/files", quota(reason))
    with gdrive(server, tmp_path) as source:
        assert source.listing().complete is False
        assert codes(source) == ["deploy_gdrive.rate_limited"]
        (finding,) = source.findings()
        assert finding.details["retry_after"] == 30 and finding.details["status"] == 403


@pytest.mark.parametrize(
    "reply",
    [
        quota("forbidden"),
        quota("insufficientFilePermissions"),
        Reply(403, b"not json"),
        Reply(403, b""),
        reply_json({"error": {"errors": "rateLimitExceeded"}}, 403),
        reply_json({"error": "rateLimitExceeded"}, 403),
        Reply(403, b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}' + b" " * 70_000),
    ],
)
def test_drive_any_other_403_stays_access_denied(tmp_path: Path, reply: Reply) -> None:
    server = FakeServer(DriveBackend())
    server.inject(lambda r: r.path == "/drive/v3/files", reply)
    with gdrive(server, tmp_path) as source:
        source.listing()
        assert codes(source) == ["deploy_gdrive.access_denied"]


def test_a_403_reason_is_only_a_quota_for_drive_shaped_bodies_and_never_leaks(
    tmp_path: Path,
) -> None:
    server = FakeServer(JiraBackend())
    server.inject(lambda r: "/search/jql" in r.path, Reply(403, b'{"errorMessages":["no"]}'))
    with jira(server, tmp_path) as source:
        source.listing()
        assert codes(source) == ["deploy_jira.access_denied"]
        assert all("errorMessages" not in f.message for f in source.findings())


@pytest.mark.parametrize("status", [429, 503, 509])
def test_graph_throttling_is_rate_limited_with_a_bounded_retry_after(
    tmp_path: Path, status: int
) -> None:
    server = FakeServer(GraphBackend())
    limited = Reply(status, b"{}")
    limited.headers["Retry-After"] = "17"
    server.inject(lambda r: "/root/delta" in r.path, limited)
    with onedrive(server, tmp_path) as source:
        assert source.listing().complete is False
        (finding,) = source.findings()
        assert finding.code == "deploy_onedrive.rate_limited"
        assert finding.details["retry_after"] == 17 and finding.details["status"] == status


@pytest.mark.parametrize("retry_after", ["Wed, 21 Oct 2026 07:28:00 GMT", "-5", "99999999999", "x"])
def test_graph_a_retry_after_that_is_not_a_bounded_integer_is_not_used(
    tmp_path: Path, retry_after: str
) -> None:
    server = FakeServer(GraphBackend())
    limited = Reply(503, b"{}")
    limited.headers["Retry-After"] = retry_after
    server.inject(lambda r: "/root/delta" in r.path, limited)
    with onedrive(server, tmp_path) as source:
        source.listing()
        (finding,) = source.findings()
        assert finding.code == "deploy_onedrive.rate_limited"
        assert "retry_after" not in finding.details


def test_graph_a_throttled_download_fails_that_read_only(tmp_path: Path) -> None:
    from neptune_deploy.sources.records import ObjectReadError

    backend = GraphBackend()
    server = FakeServer(backend)
    with onedrive(server, tmp_path) as source:
        entry = source.listing().entries[0]
        server.inject(lambda r: r.path.startswith("/dl/"), Reply(503, b"{}"))
        with pytest.raises(ObjectReadError):
            source.open(entry.location)
        assert "deploy_onedrive.rate_limited" in codes(source)


def test_a_503_is_only_throttling_where_the_system_says_so(tmp_path: Path) -> None:
    server = FakeServer(JiraBackend())
    server.inject(lambda r: "/search/jql" in r.path, Reply(503, b"{}"))
    with jira(server, tmp_path) as source:
        source.listing()
        assert codes(source) == ["deploy_jira.listing_failed"]


# --- Relation provenance ---------------------------------------------------------------------


def test_a_relation_is_stated_and_cites_where_the_system_stated_it(tmp_path: Path) -> None:
    with jira(FakeServer(JiraBackend()), tmp_path) as source:
        relations = source.relations()
        assert len(relations) == 3
        assert all(isinstance(r, Relation) and r.assertion_kind == "stated" for r in relations)
        assert [r.locator for r in relations] == [
            "/fields/attachment/0",
            "/fields/attachment/1",
            "/fields/attachment/2",
        ]
        # the locator points at the attachment in the parent's own snapshot
        import json

        parent = next(e for e in source.listing().entries if e.id == "issue/10001")
        record = json.loads(source.open(parent.location).read())[0]
        for relation in relations:
            number = int(relation.locator.rsplit("/", 1)[1])  # type: ignore[union-attr]
            assert (
                str(record["fields"]["attachment"][number]["id"])
                == (relation.child.object_id.rsplit("/", 1)[1])
            )


def test_servicenow_and_rest_relations_cite_their_records(tmp_path: Path) -> None:
    with servicenow(FakeServer(ServiceNowBackend()), tmp_path) as source:
        relations = source.relations()
        assert relations and {r.locator for r in relations} == {"/table_sys_id"}
        assert {r.assertion_kind for r in relations} == {"stated"}
    with rest(FakeServer(RestBackend()), tmp_path) as source:
        relations = source.relations()
        assert relations and {r.locator for r in relations} == {"/attachments/0"}


# --- The redirect query is the system's bytes ------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ["tempauth=a+b%2Fc==&e=2026", "tempauth=a%20b", "tempauth=%2B%2b+&x", "tempauth=ab&flag"],
)
def test_graph_the_redirect_query_reaches_the_host_exactly_as_written(
    tmp_path: Path, query: str
) -> None:
    backend = GraphBackend()
    backend.redirect_query = query
    server = FakeServer(backend)
    with onedrive(server, tmp_path) as source:
        entry = source.listing().entries[0]
        source.open(entry.location).close()
    assert [r.target.partition("?")[2] for r in backend.downloads] == [query]


@pytest.mark.parametrize("query", ["tempauth=a b", "tempauth=é", "t=a\x00b", "t=<x>", "t=a\\b"])
def test_graph_a_redirect_query_that_is_not_plain_is_refused(tmp_path: Path, query: str) -> None:
    from neptune_deploy.sources.object_store.transport import RedirectRefused
    from neptune_deploy.sources.records.http import pre_authenticated

    with pytest.raises(RedirectRefused):
        pre_authenticated(f"http://127.0.0.1:9/dl/x?{query}", ["127.0.0.1"], 302)
