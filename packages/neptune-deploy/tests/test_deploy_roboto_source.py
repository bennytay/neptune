"""The Roboto connector against recorded-shape API fixtures (ADR 0009)."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from deploy_roboto_fake import DATASET, ORG, TOKEN, FakeRoboto, content_for
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.provenance import Provenance
from neptune.model.world import StructuredRecord, StructuredTable, structured_record_from_json
from neptune.store.workspace import LocalOnlyError, Workspace
from neptune_deploy.sources.object_store import ObjectEntry, ObjectReadError
from neptune_deploy.sources.roboto import RobotoSource, roboto_source

CREDENTIALS = {"roboto_api_token": TOKEN}


def online(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    return workspace


@contextmanager
def connect(
    fake: FakeRoboto,
    tmp_path: Path,
    prefix: str = "",
    *,
    ledger: SourceLedger | None = None,
    **options: Any,
) -> Iterator[RobotoSource]:
    with fake.serve() as endpoint:
        yield roboto_source(
            f"roboto://{ORG}/{DATASET}/{prefix}",
            network=online(tmp_path),
            ledger=ledger,
            options={"endpoint": endpoint, **options},
            credentials=CREDENTIALS,
        )


def fingerprint(source: RobotoSource, ledger: SourceLedger) -> None:
    for entry in source.walk():
        if isinstance(entry, ObjectEntry):
            with source.open(entry.location) as stream:
                ledger.observe(entry.location, digest_stream(stream, chunk_size=1024 * 1024))


def codes(source: RobotoSource) -> list[str]:
    return sorted(finding.code for finding in source.findings())


# --- Identity and listing -----------------------------------------------------------------------


def test_files_are_listed_by_path_with_a_revision_token_naming_file_and_version() -> None:
    fake = FakeRoboto()
    with fake.serve() as endpoint:
        source = roboto_source(
            f"roboto://{ORG}/{DATASET}/",
            network=_Open(),
            options={"endpoint": endpoint},
            credentials=CREDENTIALS,
        )
        listing = source.listing()
    assert listing.complete and source.findings() == ()
    assert [entry.key for entry in listing.entries] == [
        "amr07/calibration.yaml",
        "amr07/run_0914_am.mcap",
        "arm_cell3/pick_place_0914.mcap",
        "legged01/patrol_0914.bag",
    ]  # no directory record, no deleted file, no reserved (not yet uploaded) one
    by_key = {entry.key: entry for entry in listing.entries}
    assert by_key["amr07/run_0914_am.mcap"].location == ExternalObjectRef(
        "deploy_roboto", f"{ORG}/{DATASET}/amr07/run_0914_am.mcap", "version:fl_amr07_am:2"
    )
    assert by_key["legged01/patrol_0914.bag"].size == 180
    assert by_key["amr07/run_0914_am.mcap"].name == "run_0914_am.mcap"


def test_a_prefix_lists_only_the_files_under_it(tmp_path: Path) -> None:
    with connect(FakeRoboto(), tmp_path, "amr07/") as source:
        assert [e.key for e in source.listing().entries] == [
            "amr07/calibration.yaml",
            "amr07/run_0914_am.mcap",
        ]
        assert source.findings() == ()


def test_the_listing_is_deterministic_across_runs_and_page_boundaries(tmp_path: Path) -> None:
    first, second = FakeRoboto(), FakeRoboto()
    # The same files in one page instead of two: the listing does not depend on paging.
    merged = (
        second.file_pages[None]["data"]["items"] + second.file_pages["tok_files_2"]["data"]["items"]
    )
    second.file_pages = {None: {"data": {"items": merged, "next_token": None}}}
    with connect(first, tmp_path / "a") as one, connect(second, tmp_path / "b") as two:
        assert one.listing() == two.listing()
        assert one.transform == two.transform
        assert one.catalog() == two.catalog()
        assert [d.data for d in one.catalog().documents] == [
            d.data for d in two.catalog().documents
        ]


# --- Reads --------------------------------------------------------------------------------------


def test_bytes_are_read_by_range_with_the_token_sent_to_the_api_only(tmp_path: Path) -> None:
    fake = FakeRoboto()
    with connect(fake, tmp_path) as source:
        entry = next(e for e in source.listing().entries if e.key.endswith("run_0914_am.mcap"))
        with source.open(entry.location) as stream:
            stream.seek(100)
            assert stream.read(50) == content_for("amr07/run_0914_am.mcap", 300)[100:150]
        assert source.findings() == ()
    api = [r for r in fake.requests if not r.path.startswith("/content/")]
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in api)
    content = fake.content_requests()
    assert content and all("authorization" not in r.headers for r in content)
    assert all(r.headers["range"].startswith("bytes=") for r in content)
    # The signature reaches the content host exactly as signed, in order.
    assert content[0].query["X-Amz-Credential"] == "AKID/20260914/us-east-1/s3/aws4_request"
    # Reading checked the record's version before asking for a signed URL.
    assert [r.path for r in fake.requests if "/record/" in r.path] == [
        "/v1/files/record/fl_amr07_am"
    ]


def test_the_only_post_is_the_dataset_files_query(tmp_path: Path) -> None:
    fake = FakeRoboto()
    with connect(fake, tmp_path) as source:
        source.listing()
        source.catalog()
        entry = source.listing().entries[0]
        source.open(entry.location).read(10)
    posts = {r.path for r in fake.requests if r.method == "POST"}
    assert posts == {f"/v1/datasets/{DATASET}/files/query"}
    assert {r.method for r in fake.requests} == {"GET", "POST"}


def test_a_reader_checks_each_chunk_against_the_fingerprint(tmp_path: Path) -> None:
    fake = FakeRoboto()
    with connect(fake, tmp_path) as source:
        entry = next(e for e in source.listing().entries if e.key.endswith(".bag"))
        with source.open(entry.location) as stream:
            artifact = digest_stream(stream, chunk_size=64)
        reader = source.reader(entry.location, artifact)
        assert reader.read(0, 180) == content_for("legged01/patrol_0914.bag", 180)
        fake.reupload("legged01/patrol_0914.bag", b"x" * 180)  # same size, other bytes, new version
        fresh = source.reader(entry.location, artifact)
        with pytest.raises(ObjectReadError) as raised:
            fresh.read(0, 10)
        assert raised.value.code == "object_changed"


# --- Revisions ----------------------------------------------------------------------------------


def test_a_reupload_is_a_new_revision_of_the_same_location(tmp_path: Path) -> None:
    fake = FakeRoboto()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger)
        before = {e.key: e.location for e in source.listing().entries}
    fake.reupload("amr07/run_0914_am.mcap", content_for("run-v3", 310))
    with connect(fake, tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
        assert [e.key for e in discovery.changed] == ["amr07/run_0914_am.mcap"]
        assert (discovery.new, discovery.gone) == ((), ())
        assert len(discovery.unchanged) == 3
        (changed,) = discovery.changed
        assert changed.location.key == before["amr07/run_0914_am.mcap"].key
        assert changed.location.revision_token == "version:fl_amr07_am:3"
        assert [e.key for e in source.walk() if isinstance(e, ObjectEntry)] == [changed.key]
        fingerprint(source, ledger)
    assert len(ledger.revisions()) == 5


def test_a_file_deleted_and_uploaded_again_is_never_unchanged(tmp_path: Path) -> None:
    fake = FakeRoboto()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger)
    # Version 1 of a new file id at the old path: the version number alone would collide.
    fake.replace_file("amr07/calibration.yaml", b"k: 2\n" * 10, "fl_amr07_cal2")
    with connect(fake, tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
    assert [e.key for e in discovery.changed] == ["amr07/calibration.yaml"]
    assert discovery.changed[0].location.revision_token == "version:fl_amr07_cal2:1"


def test_an_object_replaced_after_listing_fails_that_read_only(tmp_path: Path) -> None:
    fake = FakeRoboto()
    with connect(fake, tmp_path) as source:
        listing = source.listing()
        fake.reupload("amr07/run_0914_am.mcap", b"y" * 300)
        replaced = next(e for e in listing.entries if e.key == "amr07/run_0914_am.mcap")
        other = next(e for e in listing.entries if e.key == "amr07/calibration.yaml")
        with pytest.raises(ObjectReadError) as raised:
            source.open(replaced.location).read(10)
        assert raised.value.code == "object_changed"
        assert source.open(other.location).read(5) == content_for("amr07/calibration.yaml", 120)[:5]
    assert codes(source) == ["deploy_roboto.object_changed"]


def test_an_expired_signed_url_is_asked_for_again_once(tmp_path: Path) -> None:
    fake = FakeRoboto()
    with connect(fake, tmp_path) as source:
        entry = next(e for e in source.listing().entries if e.key.endswith("calibration.yaml"))
        stream = source.open(entry.location)
        assert stream.read(4) == content_for("amr07/calibration.yaml", 120)[:4]
        fake.expire_signed_before = 1  # the first URL has expired
        stream.seek(60)
        assert stream.read(4) == content_for("amr07/calibration.yaml", 120)[60:64]
    assert source.findings() == ()


# --- Catalog metadata as stated records ---------------------------------------------------------


def _table(source: RobotoSource, name: str) -> tuple[StructuredTable, list[StructuredRecord]]:
    catalog = source.catalog()
    (table,) = [t for t in catalog.tables if t.name == Known(name, t.provenance)] or [
        t for t in catalog.tables if getattr(t.name, "value", None) == name
    ]
    return table, [row for row in catalog.rows if row.table == table.id]


def _cells(table: StructuredTable, row: StructuredRecord) -> dict[str, Any]:
    assert isinstance(table.header, Known)
    return dict(zip(table.header.value, row.cells, strict=True))


def test_events_are_stated_records_with_time_ranges_on_named_clocks(tmp_path: Path) -> None:
    fake = FakeRoboto()
    with connect(
        fake,
        tmp_path,
        event_clock={"epoch": "unix", "timescale": "posix", "resolution": "1/1000000000"},
    ) as source:
        catalog = source.catalog()
        table, rows = _table(source, "roboto events")
    assert [_cells(table, row)["event_id"].value for row in rows] == [
        "ev_amr07_stop",
        "ev_arm3_grasp",
        "ev_legged_slip",
    ]
    stop = _cells(table, rows[0])
    assert stop["name"].value == "protective_stop"
    assert stop["start_time"].value == 1_757_836_805_000_000_000
    assert stop["end_time"].value == 1_757_836_809_500_000_000
    assert isinstance(stop["description"], Known)
    assert json.loads(stop["metadata"].value) == {"rule": "ISO3691-4 5.2", "zone": "aisle-12"}
    assert json.loads(stop["tags"].value) == ["safety", "amr"]
    grasp = _cells(table, rows[1])
    assert grasp["start_time"].value == grasp["end_time"].value  # a discrete event
    assert isinstance(grasp["description"], Unknown)  # null is not "no description"
    # The clock a time is on is a record: one per time field, named by the field and the dataset.
    domains = {d.field: d for d in catalog.domains}
    assert set(domains) == {"start_time", "end_time"}
    assert stop["@clock:start_time"].value == domains["start_time"].id
    assert stop["@clock:end_time"].value == domains["end_time"].id
    start = domains["start_time"]
    assert start.scope == (DATASET,)
    assert [start.epoch.value, str(start.timescale.value)] == ["unix", "posix"]  # type: ignore[union-attr]
    assert start.resolution.value.denominator == 10**9  # type: ignore[union-attr]
    assert isinstance(start.role, Unknown) and isinstance(start.declared_monotonic, Unknown)


def test_a_clock_nobody_declared_is_unknown_not_unix(tmp_path: Path) -> None:
    with connect(FakeRoboto(), tmp_path) as source:
        domains = source.catalog().domains
    assert domains
    for domain in domains:
        assert all(
            isinstance(state, Unknown)
            for state in (domain.epoch, domain.timescale, domain.resolution, domain.role)
        )


def test_every_record_is_stated_and_cites_a_catalog_document(tmp_path: Path) -> None:
    with connect(FakeRoboto(), tmp_path) as source:
        catalog = source.catalog()
    documents = {document.content_id: document for document in catalog.documents}
    assert {d.ref.object_id for d in catalog.documents} == {
        f"{ORG}/{DATASET}:{part}" for part in ("dataset", "files", "events", "comments")
    }
    assert all(d.ref.revision_token.startswith("records:") for d in catalog.documents)
    assert all(d.ref.connector_id == "deploy_roboto" for d in catalog.documents)
    for row in catalog.rows:
        assert row.provenance.assertion_kind is AssertionKind.STATED
        assert row.provenance.transform == source.transform.id
        assert row.provenance.evidence.source in documents
        for cell in row.cells:
            assert isinstance(cell, Known | Unknown)
            assert isinstance(cell.provenance, Provenance)
            assert cell.provenance.assertion_kind is AssertionKind.STATED
    for domain in catalog.domains:
        assert domain.provenance.evidence.source in documents
    # Each record round-trips through its strict reader.
    for row in catalog.rows:
        assert structured_record_from_json(row.to_json()) == row


def test_the_files_table_carries_roboto_versions_and_metadata_as_stated(tmp_path: Path) -> None:
    with connect(FakeRoboto(), tmp_path, "amr07/") as source:
        table, rows = _table(source, "roboto files")
    assert len(rows) == 2  # only the files under the prefix, and only those the source listed
    by_path = {_cells(table, row)["relative_path"].value: _cells(table, row) for row in rows}
    run = by_path["amr07/run_0914_am.mcap"]
    assert (run["version"].value, run["file_id"].value, run["size"].value) == (
        2,
        "fl_amr07_am",
        300,
    )
    assert run["ingestion_status"].value == "ingested"


def test_a_failed_events_read_is_a_finding_and_the_rest_of_the_catalog_stands(
    tmp_path: Path,
) -> None:
    fake = FakeRoboto()
    fake.event_pages = {}  # every events page is a 404
    with connect(fake, tmp_path) as source:
        catalog = source.catalog()
        assert {d.ref.object_id.rpartition(":")[2] for d in catalog.documents} == {
            "dataset",
            "files",
            "comments",
        }
    (finding,) = [f for f in source.findings() if f.code == "deploy_roboto.catalog_failed"]
    assert finding.details["part"] == "events" and finding.details["status"] == 404


def test_events_and_comments_can_be_switched_off(tmp_path: Path) -> None:
    fake = FakeRoboto()
    with connect(fake, tmp_path, events=False, comments=False) as source:
        source.catalog()
    assert not any("events" in r.path or "comments" in r.path for r in fake.requests)


def test_record_limit_stops_with_a_finding_that_says_so(tmp_path: Path) -> None:
    with connect(FakeRoboto(), tmp_path, max_records=2) as source:
        _, rows = _table(source, "roboto events")
    assert len(rows) == 2
    (finding,) = [f for f in source.findings() if f.code == "deploy_roboto.catalog_limit"]
    assert finding.details["cause"] == "record_limit" and finding.details["part"] == "events"


# --- The network boundary -----------------------------------------------------------------------


def test_a_local_only_workspace_refuses_the_connector(tmp_path: Path) -> None:
    fake = FakeRoboto()
    with fake.serve() as endpoint, pytest.raises(LocalOnlyError):
        roboto_source(
            f"roboto://{ORG}/{DATASET}/",
            network=Workspace(tmp_path / "home"),  # network off, the default
            options={"endpoint": endpoint},
            credentials=CREDENTIALS,
        )
    assert fake.requests == []


def test_switching_to_local_only_refuses_the_next_request(tmp_path: Path) -> None:
    fake = FakeRoboto()
    workspace = online(tmp_path)
    with fake.serve() as endpoint:
        source = roboto_source(
            f"roboto://{ORG}/{DATASET}/",
            network=workspace,
            options={"endpoint": endpoint},
            credentials=CREDENTIALS,
        )
        workspace.allow_network(False)
        with pytest.raises(LocalOnlyError):
            source.listing()
    assert fake.requests == []


class _Open:
    def require_network(self, purpose: str) -> None:
        return None
