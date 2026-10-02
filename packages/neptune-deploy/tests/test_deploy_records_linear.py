"""The Linear connector against an in-process GraphQL endpoint: query-only POST, the CSV the
Linear preset reads, newest-first paging, and the workspace check (ADR 0008)."""

import csv
import io
import json
from pathlib import Path

import pytest

from deploy_records_fake import FakeServer, Reply, reply_json
from deploy_records_fake_graph import LinearBackend
from deploy_records_support import LINEAR_CREDENTIALS, codes, fingerprint, linear, online
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef
from neptune_deploy.sources.records import RecordConfigError, RecordSource, linear_source
from neptune_deploy.sources.records.systems.linear import LinearSystem

SCOPE = "@acme-robotics/OPS/"
FIRST = "issue/5b1f0c1e-62a4-4c7e-9a11-0d6c3b7a0001"


def rows(source: RecordSource, item_id: str) -> list[list[str]]:
    entry = next(e for e in source.listing().entries if e.id == item_id)
    with source.open(entry.location) as stream:
        return list(csv.reader(io.StringIO(stream.read().decode())))


def test_issues_are_sources_keyed_by_issue_id_and_updated_at(tmp_path: Path) -> None:
    with linear(FakeServer(LinearBackend()), tmp_path) as source:
        listing = source.listing()
        assert listing.complete and listing.mode == "snapshot"
        assert [e.name for e in listing.entries] == ["OPS-12.csv", "OPS-13.csv", "OPS-14.csv"]
        assert listing.entries[0].location == ExternalObjectRef(
            "deploy_linear", SCOPE + FIRST, "updated:2026-03-02T10:00:00.000Z"
        )
        assert listing.removed == ("issue/5b1f0c1e-62a4-4c7e-9a11-0d6c3b7a0004",)  # trashed
        assert codes(source) == []


def test_a_snapshot_is_one_row_in_the_columns_of_linears_own_export(tmp_path: Path) -> None:
    with linear(FakeServer(LinearBackend()), tmp_path) as source:
        header, row = rows(source, FIRST)
        assert header[:4] == ["ID", "Team", "Title", "Status"]
        assert dict(zip(header, row, strict=True)) == {
            "ID": "OPS-12", "Team": "OPS", "Title": "Replace gripper pad on cell 2 arm",
            "Status": "Done", "Priority": "High", "Assignee": "R. Okafor",
            "Labels": "intervention,arm", "Created": "2026-03-01T08:15:30.000Z",
            "Description": "Pad worn through after 40k cycles.", "Parent issue": "",
            "Started": "2026-03-01T09:00:00.000Z", "Completed": "2026-03-02T10:00:00.000Z",
        }  # fmt: skip
        _, second = rows(source, "issue/5b1f0c1e-62a4-4c7e-9a11-0d6c3b7a0002")
        assert second[5] == "" and second[9] == "OPS-9"  # no assignee: a blank, never a fact
        _, third = rows(source, "issue/5b1f0c1e-62a4-4c7e-9a11-0d6c3b7a0003")
        assert third[8] == 'Line one,\nline "two"'  # quoting round-trips
        assert source.ingest_options() == {"tabular": {"csv_header": "first_row"}}


def test_only_graphql_queries_are_sent_and_the_variables_ride_in_the_body(tmp_path: Path) -> None:
    backend = LinearBackend()
    server = FakeServer(backend)
    with linear(server, tmp_path) as source:
        source.listing()
    assert server.other_methods == []
    posts = [r for r in server.log if r.method == "POST"]
    assert posts and all(r.path == "/graphql" for r in posts)
    for post in posts:
        assert json.loads(post.body)["query"].lstrip().startswith("query ")
        assert post.headers["content-type"] == "application/json"
        assert post.headers["authorization"] == "lin_api_key-never-printed"
    assert not any(
        "mutation" in d or "OPS" in d for d in backend.documents
    )  # no value in a document


@pytest.mark.parametrize(
    "document",
    [
        'mutation { issueDelete(id: "x") { success } }',
        "{ viewer { id } }",
        "query A { x } mutation B { y }",
        "subscription { issueCreated { id } }",
        "  # query\nmutation { x }",
    ],
)
def test_a_document_that_is_not_a_query_is_never_sent(tmp_path: Path, document: str) -> None:
    server = FakeServer(LinearBackend())
    with linear(server, tmp_path) as source:
        api = source.system.api
        with pytest.raises(ValueError, match="queries only"):
            api.graphql("/graphql", document, {})
    assert server.log == []


def test_a_workspace_that_is_another_stops_the_listing(tmp_path: Path) -> None:
    backend = LinearBackend()
    backend.workspace = "other-corp"
    with linear(FakeServer(backend), tmp_path) as source:
        assert source.listing().entries == () and source.listing().complete is False
        assert codes(source) == ["deploy_linear.response_invalid"]


def test_a_graphql_error_in_a_200_is_a_failed_listing(tmp_path: Path) -> None:
    server = FakeServer(LinearBackend())
    server.inject(lambda r: True, reply_json({"errors": [{"message": "boom"}], "data": None}))
    with linear(server, tmp_path) as source:
        assert source.listing().complete is False
        assert codes(source) == ["deploy_linear.response_invalid"]
        assert all("boom" not in f.message for f in source.findings())


def test_a_rate_limit_answered_as_400_is_a_finding_and_no_cursor_is_stated(tmp_path: Path) -> None:
    server = FakeServer(LinearBackend())
    limited = Reply(400, b'{"errors":[{"extensions":{"code":"RATELIMITED"}}]}')
    limited.headers["X-RateLimit-Requests-Remaining"] = "0"
    server.inject(lambda r: True, limited)
    with linear(server, tmp_path) as source:
        assert source.listing().complete is False and source.cursor is None
        assert codes(source) == ["deploy_linear.rate_limited"]


def test_a_400_that_is_not_a_rate_limit_is_a_failed_listing(tmp_path: Path) -> None:
    server = FakeServer(LinearBackend())
    server.inject(lambda r: True, Reply(400, b"{}"))
    with linear(server, tmp_path) as source:
        source.listing()
        assert codes(source) == ["deploy_linear.listing_failed"]


def test_pages_of_any_size_give_one_listing_and_the_cursor_is_the_newest_instant(
    tmp_path: Path,
) -> None:
    seen = []
    for size in (1, 2, 250):
        with linear(FakeServer(LinearBackend()), tmp_path, page_size=size) as source:
            seen.append((source.listing().entries, source.listing().removed, source.cursor))
    assert seen[0] == seen[1] == seen[2]
    assert seen[0][2] == "deploy_linear/1:2026-03-06T09:00:00.000Z"


def test_a_run_that_stopped_part_way_states_no_cursor(tmp_path: Path) -> None:
    """Newest first: the first pages are the newest, so a cursor taken from them would skip the
    oldest issues that were never read."""
    server = FakeServer(LinearBackend())
    server.inject(lambda r: b'"cursor:1"' in r.body, Reply(500, b"{}"), times=5)
    with linear(server, tmp_path, page_size=1) as source:
        assert source.listing().complete is False
        assert len(source.listing().entries) == 1
        assert source.cursor is None


def test_an_issue_edited_in_place_is_a_new_revision_and_the_old_one_is_kept(tmp_path: Path) -> None:
    backend = LinearBackend()
    server = FakeServer(backend)
    ledger = SourceLedger()
    with linear(server, tmp_path) as source:
        first = fingerprint(source, ledger, source.walk())
    backend.edit("OPS-14", "2026-03-07T11:00:00.000Z", title="Recalibrate thruster allocation v2")
    with linear(server, tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
        assert [e.name for e in discovery.changed] == ["OPS-14.csv"]
        second = fingerprint(source, ledger, source.walk())
    key = "issue/5b1f0c1e-62a4-4c7e-9a11-0d6c3b7a0003"
    assert second[key].content_id != first[key].content_id
    chain = sorted(
        (r for r in ledger.revisions() if r.location.key[2] == SCOPE + key),
        key=lambda r: len(r.supersedes),
    )
    assert len(chain) == 2 and chain[1].supersedes == (chain[0].id,)


def test_the_change_feed_reads_from_the_cursor_and_a_trashed_issue_is_gone(tmp_path: Path) -> None:
    backend = LinearBackend()
    server = FakeServer(backend)
    ledger = SourceLedger()
    with linear(server, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
        cursor = source.cursor
    backend.edit("OPS-13", "2026-03-08T00:00:00.000Z", title="Re-teach dock approach, AMR 7 (v2)")
    backend.trash("OPS-12", "2026-03-08T01:00:00.000Z")
    with linear(server, tmp_path, since=cursor, ledger=ledger) as source:
        listing = source.listing()
        assert listing.mode == "incremental" and listing.complete
        # the instant of the cursor is read again; OPS-14 is unchanged and the ledger discards it
        assert [e.name for e in listing.entries] == ["OPS-13.csv", "OPS-14.csv"]
        assert [e.name for e, _ in source.discover(ledger).unchanged] == ["OPS-14.csv"]
        assert listing.removed == (FIRST,)
        assert [g.location.key[2] for g in source.discover(ledger).gone] == [SCOPE + FIRST]
        assert source.cursor == "deploy_linear/1:2026-03-08T01:00:00.000Z"
    assert any(b"since" in r.body for r in server.log)


def test_unusable_issues_are_findings_and_the_rest_are_listed(tmp_path: Path) -> None:
    backend = LinearBackend()
    good = dict(backend.issues[0])
    backend.issues.extend(
        [
            {**good, "id": "not-a-uuid"},
            {**good, "id": "5b1f0c1e-62a4-4c7e-9a11-0d6c3b7a0099", "updatedAt": "yesterday"},
            {**good, "id": "5b1f0c1e-62a4-4c7e-9a11-0d6c3b7a0098", "identifier": "../x"},
            {**good, "id": "5b1f0c1e-62a4-4c7e-9a11-0d6c3b7a0097", "title": "\ud800 lone"},
        ]
    )
    with linear(FakeServer(backend), tmp_path) as source:  # the surrogate rides as a JSON escape
        assert len(source.listing().entries) == 3
        assert codes(source) == [
            "deploy_linear.id_invalid",
            "deploy_linear.record_invalid",
            "deploy_linear.record_unrepresentable",
        ]


def test_the_identity_is_the_workspace_so_a_shared_api_host_cannot_mix_two(tmp_path: Path) -> None:
    server = FakeServer(LinearBackend())
    with server.serve() as host:
        a = linear_source(
            "linear://acme-robotics/OPS", network=online(tmp_path),
            options={"endpoint": f"http://{host}"}, credentials=LINEAR_CREDENTIALS,
        )  # fmt: skip
        assert isinstance(a, RecordSource) and a.location.scope == SCOPE
        b = linear_source(
            "linear://other-corp/OPS", network=online(tmp_path),
            options={"endpoint": f"http://{host}", "instance": "other-site"},
            credentials=LINEAR_CREDENTIALS,
        )  # fmt: skip
        assert b.location.scope == "@other-site/OPS/"
        assert (
            isinstance(b.system, LinearSystem) and b.system.workspace == "other-corp"
        )  # verified even when identity is declared


@pytest.mark.parametrize(
    "url",
    [
        "linear://acme-robotics",
        "linear://acme-robotics/ops",
        "linear://acme-robotics/OPS/extra",
        "linear://Acme/OPS",
        "linear://acme_robotics/OPS",
        "linear://",
        "linear://acme@evil/OPS",
        "linear://acme-robotics/OPS?x=1",
    ],
)
def test_urls_that_are_not_a_workspace_and_a_team_key_are_refused(tmp_path: Path, url: str) -> None:
    with pytest.raises(RecordConfigError):
        linear_source(url, network=online(tmp_path), credentials=LINEAR_CREDENTIALS)


def test_credentials_are_a_key_or_a_token_never_both_never_none(tmp_path: Path) -> None:
    for creds in ({}, {"api_key": "a", "access_token": "b"}):
        with pytest.raises(RecordConfigError):
            linear_source("linear://acme/OPS", network=online(tmp_path), credentials=creds)
    token = linear_source(
        "linear://acme/OPS", network=online(tmp_path), credentials={"access_token": "tok"}
    )
    assert token.system.api._auth.value == "Bearer tok"
    from_env = linear_source(
        "linear://acme/OPS", network=online(tmp_path),
        environ={"NEPTUNE_LINEAR_API_KEY": "envkey", "LINEAR_API_KEY": "ambient"},
    )  # fmt: skip
    assert from_env.system.api._auth.value == "envkey"


def test_the_listing_is_deterministic_across_sources(tmp_path: Path) -> None:
    out = []
    for _ in range(2):
        with linear(FakeServer(LinearBackend()), tmp_path) as source:
            out.append((source.listing(), source.findings(), source.cursor, source.transform))
    assert out[0] == out[1]
