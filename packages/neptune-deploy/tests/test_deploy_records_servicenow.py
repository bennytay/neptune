"""The ServiceNow connector against an in-process Table API: snapshots, attachments, deletions."""

import csv
import io
from pathlib import Path

from deploy_records_fake import FakeServer, Reply, ServiceNowBackend
from deploy_records_support import codes, fingerprint, servicenow
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef
from neptune_deploy.sources.records import RecordSource

CHG1 = "table/change_request/1c741bd70b2322007518478d83673af3"
CHG2 = "table/change_request/2d852ce80c3433118629589e94784bf4"
CHG3 = "table/change_request/3e963df90d4544229730690fa5895cd5"
ATT = f"{CHG1}/attachment/aa1111111111111111111111111111aa"


def rows(source: RecordSource, item_id: str) -> list[list[str]]:
    entry = next(e for e in source.listing().entries if e.id == item_id)
    with source.open(entry.location) as stream:
        return list(csv.reader(io.StringIO(stream.read().decode("utf-8"))))


def test_records_and_attachments_are_sources_with_mod_count_tokens(tmp_path: Path) -> None:
    with servicenow(FakeServer(ServiceNowBackend()), tmp_path) as source:
        listing = source.listing()
        assert listing.complete
        assert [e.id for e in listing.entries] == [CHG1, ATT, CHG2, CHG3]
        assert listing.entries[0].location == ExternalObjectRef(
            "deploy_servicenow",
            f"@site-a/change_request/{CHG1}",
            "mod_count:2@2026-07-01 08:00:00",
        )
        assert source.findings() == ()
        [relation] = source.relations()
        assert relation.child == listing.entries[1].location
        assert relation.parent == listing.entries[0].location


def test_a_record_snapshot_is_a_one_row_csv_of_the_declared_columns(tmp_path: Path) -> None:
    with servicenow(FakeServer(ServiceNowBackend()), tmp_path) as source:
        header, row = rows(source, CHG1)
        assert header == sorted(header) and "number" in header and "cmdb_ci" in header
        record = dict(zip(header, row, strict=True))
        assert record["number"] == "CHG0030001"
        assert record["u_before"] == "ctrl 4.1.2"
        assert "sys_id" not in record and "sys_updated_on" not in record
        assert rows(source, CHG3)[1][header.index("approval_set")] == ""  # blank stays blank


def test_declared_columns_and_a_filter_are_what_is_requested(tmp_path: Path) -> None:
    server = FakeServer(ServiceNowBackend())
    with servicenow(
        server, tmp_path, fields=["number", "cmdb_ci"], filter="active=true^type=normal"
    ) as source:
        assert rows(source, CHG1)[0] == ["cmdb_ci", "number"]
        request = server.requests("/table/change_request")[0]
        assert request.query["sysparm_query"].startswith("active=true^type=normal^ORDERBY")
        assert request.query["sysparm_display_value"] == "false"
        assert request.query["sysparm_exclude_reference_link"] == "true"


def test_a_record_edited_between_syncs_is_a_new_revision_and_the_old_is_kept(
    tmp_path: Path,
) -> None:
    backend = ServiceNowBackend()
    ledger = SourceLedger()
    with servicenow(FakeServer(backend), tmp_path) as source:
        first = fingerprint(source, ledger, source.walk())
    backend.edit("CHG0030002", "2026-07-05 11:00:00", u_after="max_speed 0.8")
    with servicenow(FakeServer(backend), tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
        assert [e.id for e in discovery.changed] == [CHG2]
        assert [e.id for e, _ in discovery.unchanged] == [CHG1, ATT, CHG3]
        assert [e.location.revision_token for e in discovery.changed] == [
            "mod_count:2@2026-07-05 11:00:00"
        ]
        second = fingerprint(source, ledger, source.walk())
    assert list(second) == [CHG2]
    assert second[CHG2].content_id != first[CHG2].content_id
    chain = [r for r in ledger.revisions() if r.location.key[2].endswith(CHG2)]
    assert len(chain) == 2


def test_the_change_feed_reads_updates_and_deletions_and_advances_the_cursor(
    tmp_path: Path,
) -> None:
    backend = ServiceNowBackend()
    ledger = SourceLedger()
    server = FakeServer(backend)
    with servicenow(server, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
        cursor = source.cursor
    assert cursor == "deploy_servicenow/1:2026-07-03 10:00:00"
    backend.edit("CHG0030001", "2026-07-06 09:00:00", u_after="ctrl 4.2.1")
    backend.delete("CHG0030003", "2026-07-06 10:00:00")
    with servicenow(server, tmp_path, since=cursor, ledger=ledger) as source:
        listing = source.listing()
        assert listing.mode == "incremental" and listing.complete
        assert [e.id for e in listing.entries if "/attachment/" not in e.id] == [CHG1]
        assert listing.removed == (CHG3,)
        discovery = source.discover(ledger)
        assert [g.location.key[2].rsplit("/", 1)[1] for g in discovery.gone] == [
            "3e963df90d4544229730690fa5895cd5"
        ]
        assert source.cursor == "deploy_servicenow/1:2026-07-06 09:00:00"
        deletion = server.requests("sys_audit_delete")[-1]
        assert "tablename=change_request" in deletion.query["sysparm_query"]


def test_a_deleted_record_is_gone_from_a_complete_snapshot_too(tmp_path: Path) -> None:
    backend = ServiceNowBackend()
    ledger = SourceLedger()
    with servicenow(FakeServer(backend), tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    backend.delete("CHG0030002", "2026-07-07 10:00:00")
    with servicenow(FakeServer(backend), tmp_path) as source:
        assert [g.location.key[2].endswith(CHG2) for g in source.discover(ledger).gone] == [True]


def test_pages_of_any_size_give_the_same_listing(tmp_path: Path) -> None:
    seen = set()
    for size in (1, 2, 1000):
        with servicenow(FakeServer(ServiceNowBackend()), tmp_path, page_size=size) as source:
            seen.add(tuple(e.location for e in source.listing().entries))
    assert len(seen) == 1


def test_a_deletions_table_that_is_denied_leaves_the_run_incomplete_and_the_cursor_unmoved(
    tmp_path: Path,
) -> None:
    server = FakeServer(ServiceNowBackend())
    server.inject(lambda r: r.path.endswith("sys_audit_delete"), Reply(403, b"{}"))
    with servicenow(server, tmp_path, since="deploy_servicenow/1:2026-07-01 00:00:00") as source:
        assert not source.listing().complete
        assert source.cursor is None  # keep the previous cursor: nothing is skipped over
        assert "deploy_servicenow.access_denied" in codes(source)


def test_a_table_that_counts_more_rows_than_it_gives_is_partial(tmp_path: Path) -> None:
    server = FakeServer(ServiceNowBackend())
    backend_handle = server.backend.handle

    def lying(request):  # type: ignore[no-untyped-def]
        reply = backend_handle(request)
        if reply is not None and request.path.endswith("change_request"):
            reply.headers["X-Total-Count"] = "9"
        return reply

    server.backend.handle = lying  # type: ignore[method-assign]
    with servicenow(server, tmp_path) as source:
        assert not source.listing().complete
        assert "deploy_servicenow.listing_partial" in codes(source)
