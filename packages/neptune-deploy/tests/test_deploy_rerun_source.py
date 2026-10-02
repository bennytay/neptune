"""The Rerun Hub connector: a catalog export and the ``.rrd`` objects it names (ADR 0009)."""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from deploy_object_store_fake import FakeStore
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.provenance import Provenance
from neptune.model.world import StructuredRecord, StructuredTable, structured_record_from_json
from neptune.store.workspace import LocalOnlyError, Workspace
from neptune_deploy.sources.object_store import ObjectEntry, ObjectReadError
from neptune_deploy.sources.object_store.config import ObjectStoreConfigError
from neptune_deploy.sources.rerun import (
    RerunExport,
    RerunSource,
    parse_storage_url,
    rerun_source,
)
from neptune_deploy.sources.rerun.export import parse_export, read_export

EXPORT = Path(__file__).parent / "fixtures" / "connectors" / "rerun" / "catalog_export.json"
CREDENTIALS = {"s3_access_key_id": "AKIDEXAMPLE", "s3_secret_access_key": "s3cr3t-never-printed"}
BASE_KEY = "episodes/amr07_aisle12.rrd"
RRD = {
    "episodes/amr07_aisle12.rrd": 260,  # sizes as the catalog states them
    "episodes/arm3_pick_0914.rrd": 200,
    "episodes/arm3_pick_0914.annotations.rrd": 140,  # the layers sum to the catalog's 340
    "episodes/legged01_slip.rrd": 180,
}


def content(key: str, size: int, salt: int = 0) -> bytes:
    """Deterministic bytes of ``size`` that start like an RRD stream."""
    seed = sum(key.encode()) + salt
    return (b"RRF2" + bytes((seed + 5 * i) % 251 for i in range(size)))[:size]


def store(*, versioned: bool = True) -> FakeStore:
    fake = FakeStore(bucket="fleet-rrd", versioned=versioned)
    for key, size in RRD.items():
        fake.put(key, content(key, size))
    return fake


def online(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    return workspace


def export_file(tmp_path: Path, document: Any = None, *, raw: bytes | None = None) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "catalog.json"
    if raw is not None:
        path.write_bytes(raw)
    elif document is None:
        path.write_bytes(EXPORT.read_bytes())
    else:
        path.write_text(json.dumps(document))
    return str(path)


def document() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(EXPORT.read_text())
    return loaded


@contextmanager
def connect(
    fake: FakeStore,
    tmp_path: Path,
    export: Any = None,
    *,
    ledger: SourceLedger | None = None,
    **options: Any,
) -> Iterator[RerunSource]:
    with fake.serve() as endpoint:
        yield rerun_source(
            export_file(tmp_path, export),
            network=online(tmp_path),
            ledger=ledger,
            options={"storage": {"s3": {"endpoint": endpoint, "store": "hub-site"}}, **options},
            credentials=CREDENTIALS,
        )


def fingerprint(source: RerunSource, ledger: SourceLedger) -> None:
    for entry in source.walk():
        if isinstance(entry, ObjectEntry):
            with source.open(entry.location) as stream:
                ledger.observe(entry.location, digest_stream(stream, chunk_size=1024 * 1024))


def codes(source: RerunSource) -> list[str]:
    return sorted(finding.code for finding in source.findings())


# --- The objects a catalog names ----------------------------------------------------------------


def test_every_layer_the_catalog_names_is_an_object_at_the_store_revision(tmp_path: Path) -> None:
    fake = store()
    with connect(fake, tmp_path) as source:
        listing = source.listing()
        assert listing.complete and source.findings() == ()
        assert [entry.key for entry in listing.entries] == sorted(RRD)
        by_key = {entry.key: entry for entry in listing.entries}
        assert {key: entry.size for key, entry in by_key.items()} == RRD
        version = fake.latest(BASE_KEY.encode())
        assert version is not None
        # The same identity a deploy_s3 source over this bucket would give the object (ADR 0006).
        assert by_key[BASE_KEY].location == ExternalObjectRef(
            "deploy_s3", f"hub-site:fleet-rrd/{BASE_KEY}", f"version:{version.version_id}"
        )
        assert {loc.object_id for loc in source.objects_of(1)} == {
            f"hub-site:fleet-rrd/{key}" for key in RRD if "arm3" in key
        }
        assert len(source.objects_of(0)) == 1 and source.objects_of(99) == ()


def test_one_exact_key_listing_per_object_and_nothing_else_is_asked(tmp_path: Path) -> None:
    fake = store()
    with connect(fake, tmp_path) as source:
        source.listing()
        source.catalog()
    assert {r.method for r in fake.requests} == {"GET"}
    assert len(fake.requests) == len(RRD)  # no bucket-wide listing, no object bytes
    assert all("range" not in r.headers for r in fake.requests)
    prefixes = sorted(r.query["prefix"] for r in fake.requests)
    assert prefixes == sorted(RRD)


def test_bytes_are_read_by_range_from_the_object(tmp_path: Path) -> None:
    fake = store()
    with connect(fake, tmp_path) as source:
        entry = next(e for e in source.listing().entries if e.key == BASE_KEY)
        with source.open(entry.location) as stream:
            stream.seek(100)
            assert stream.read(50) == content(BASE_KEY, 260)[100:150]
        assert source.fetch(entry, 0, 4) == b"RRF2"
    assert source.findings() == ()
    reads = fake.object_requests()
    assert reads and all(r.headers["range"].startswith("bytes=") for r in reads)


def test_a_reader_checks_each_chunk_against_the_fingerprint(tmp_path: Path) -> None:
    fake = store(versioned=False)  # a read of a versioned bucket is pinned to its version
    with connect(fake, tmp_path) as source:
        entry = next(e for e in source.listing().entries if e.key.endswith("legged01_slip.rrd"))
        with source.open(entry.location) as stream:
            artifact = digest_stream(stream, chunk_size=64)
        key = "episodes/legged01_slip.rrd"
        assert source.reader(entry.location, artifact).read(0, 180) == content(key, 180)
        fake.put(key, content(key, 180, salt=3))  # same size, other bytes: a new version
        with pytest.raises(ObjectReadError) as raised:
            source.reader(entry.location, artifact).read(0, 10)
        assert raised.value.code == "object_changed"
    assert codes(source) == ["deploy_s3.object_changed"]


def test_an_object_replaced_under_the_same_path_is_a_new_revision_of_the_same_location(
    tmp_path: Path,
) -> None:
    fake, ledger = store(), SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger)
        before = {e.key: e.location for e in source.listing().entries}
    fake.put(BASE_KEY, content(BASE_KEY, 270, salt=9))
    with connect(fake, tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
        (changed,) = discovery.changed
        assert changed.key == BASE_KEY and changed.size == 270
        assert changed.location.object_id == before[BASE_KEY].object_id  # the same location ...
        assert changed.location.revision_token != before[BASE_KEY].revision_token  # ... a new token
        assert (discovery.new, discovery.gone) == ((), ())
        assert len(discovery.unchanged) == len(RRD) - 1
        assert [e.key for e in source.walk() if isinstance(e, ObjectEntry)] == [BASE_KEY]
        # The catalog still says 260 and the store 270: that disagreement is a finding, not a fix.
    assert codes(source) == ["deploy_rerun.catalog_size_differs"]
    (finding,) = source.findings()
    assert finding.details == {"catalog": 260, "row": 0, "store": 270}


def test_an_object_replaced_after_listing_fails_that_read_only(tmp_path: Path) -> None:
    fake = store(versioned=False)  # a read of a versioned bucket is pinned to its version
    with connect(fake, tmp_path) as source:
        listing = source.listing()
        fake.put(BASE_KEY, content(BASE_KEY, 260, salt=1))
        replaced = next(e for e in listing.entries if e.key == BASE_KEY)
        other = next(e for e in listing.entries if e.key.endswith("legged01_slip.rrd"))
        with pytest.raises(ObjectReadError) as raised:
            source.open(replaced.location).read(10)
        assert raised.value.code == "object_changed"
        assert source.open(other.location).read(4) == b"RRF2"
    assert codes(source) == ["deploy_s3.object_changed"]


def test_a_catalog_no_longer_naming_an_object_does_not_make_it_gone(tmp_path: Path) -> None:
    fake, ledger = store(), SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger)
    smaller = document()
    smaller["segments"] = smaller["segments"][:1]
    with connect(fake, tmp_path, smaller, ledger=ledger) as source:
        assert source.discover(ledger).gone == ()


def test_an_object_the_store_lacks_is_a_finding_and_the_rest_stand(tmp_path: Path) -> None:
    fake = store()
    fake.delete("episodes/legged01_slip.rrd")
    with connect(fake, tmp_path) as source:
        listing = source.listing()
    assert not listing.complete
    assert len(listing.entries) == len(RRD) - 1
    assert codes(source) == ["deploy_rerun.object_not_found"]


def test_the_stores_size_and_the_catalogs_are_compared_for_a_single_layer_only(
    tmp_path: Path,
) -> None:
    fake = store()
    fake.put("episodes/arm3_pick_0914.rrd", b"RRF2" * 60)  # two layers: the row's size is a total
    fake.put("episodes/legged01_slip.rrd", b"RRF2" * 10)  # one layer: it must agree
    with connect(fake, tmp_path) as source:
        source.listing()
    (finding,) = source.findings()
    assert finding.code == "deploy_rerun.catalog_size_differs"
    assert finding.details == {"catalog": 180, "row": 2, "store": 40}


def test_bad_rows_and_urls_are_findings_not_failures(tmp_path: Path) -> None:
    broken = document()
    broken["segments"] += [
        {"rerun_segment_id": "", "rerun_layer_names": ["base"], "rerun_storage_urls": ["s3://a/b"]},
        {"rerun_segment_id": "x", "rerun_layer_names": ["a", "b"], "rerun_storage_urls": ["s3://a/b"]},
        {"rerun_segment_id": "y", "rerun_layer_names": "base", "rerun_storage_urls": 7},
        {"rerun_segment_id": "z", "rerun_layer_names": ["a", "b", "c", "d"],
         "rerun_storage_urls": ["file:///etc/passwd", "https://example.com/x.rrd",
                                "s3://bucket-only", 5]},
    ]  # fmt: skip
    with connect(store(), tmp_path, broken) as source:
        listing = source.listing()
    assert [e.key for e in listing.entries] == sorted(RRD)
    assert codes(source).count("deploy_rerun.segment_invalid") == 3
    unsupported = [f for f in source.findings() if f.code == "deploy_rerun.storage_url_unsupported"]
    assert len(unsupported) == 4
    # A scheme is shown (bounded), a path or a host never is.
    shown = json.dumps([f.details for f in unsupported])
    assert "passwd" not in shown and "example.com" not in shown


def test_storage_urls_parse_per_provider_and_refuse_the_rest() -> None:
    plain = parse_storage_url("s3://b/k/x.rrd")
    assert plain is not None and plain[1:] == ("b", None, "k/x.rrd")
    s3 = parse_storage_url("s3://b/a%20b/x.rrd")
    assert s3 is not None and (s3[1], s3[3]) == ("b", "a%20b/x.rrd")  # verbatim, never decoded
    gcs = parse_storage_url("gs://b/x.rrd")
    assert gcs is not None and gcs[1:] == ("b", None, "x.rrd")
    azure = parse_storage_url("az://acct01/cont/x.rrd")
    assert azure is not None and azure[1:] == ("cont", "acct01", "x.rrd")
    for bad in ("s3://b", "s3://b/", "s3:///k", "https://b/k", "b/k", "", None, 5, "az://a/c"):
        assert parse_storage_url(bad) is None


# --- The catalog as stated records --------------------------------------------------------------


def _table(source: RerunSource, name: str) -> tuple[StructuredTable, list[StructuredRecord]]:
    catalog = source.catalog()
    (table,) = [t for t in catalog.tables if getattr(t.name, "value", None) == name]
    return table, [row for row in catalog.rows if row.table == table.id]


def _cells(table: StructuredTable, row: StructuredRecord) -> dict[str, Any]:
    assert isinstance(table.header, Known)
    return dict(zip(table.header.value, row.cells, strict=True))


def test_entity_paths_are_stated_by_the_catalog_and_nothing_reads_the_rrd(tmp_path: Path) -> None:
    fake = store()
    with connect(fake, tmp_path) as source:
        table, rows = _table(source, "rerun schema")
    paths = [_cells(table, row)["entity_path"].value for row in rows]
    assert sorted(paths) == [
        "/amr07/odometry",
        "/arm_cell3/joint_states/shoulder",
        "/legged01/lidar",
    ]  # one entity path per morphology: the catalog's word, kept verbatim
    amr = next(
        _cells(table, row)
        for row in rows
        if _cells(table, row)["entity_path"].value == paths[paths.index("/amr07/odometry")]
    )
    assert amr["archetype"].value == "rerun.archetypes.Transform3D"
    assert amr["is_static"].value is False  # a boolean keeps its type
    assert fake.requests == []  # building the catalog sent nothing


def test_segments_keep_every_column_as_stated_and_interpret_none(tmp_path: Path) -> None:
    with connect(store(), tmp_path) as source:
        table, rows = _table(source, "rerun segments")
    assert len(rows) == 3
    by_id = {_cells(table, row)["rerun_segment_id"].value: _cells(table, row) for row in rows}
    arm = by_id["arm3_pick_0914"]
    assert json.loads(arm["rerun_storage_urls"].value) == [
        "s3://fleet-rrd/episodes/arm3_pick_0914.rrd",
        "s3://fleet-rrd/episodes/arm3_pick_0914.annotations.rrd",
    ]
    assert arm["rerun_last_updated_at"].value == "2026-09-14T09:05:00Z"  # text, not a time
    assert arm["property:RecordingInfo:name"].value == "arm cell 3 pick and place"
    assert arm["rerun_size_bytes"].value == 340


def test_timelines_are_clocks_whose_parts_are_unknown_until_declared(tmp_path: Path) -> None:
    with connect(store(), tmp_path) as source:
        domains = {d.field: d for d in source.catalog().domains}
    assert set(domains) == {"log_time", "frame"}
    for domain in domains.values():
        assert domain.scope == ("ds-warehouse-0914",)
        assert domain.provenance.assertion_kind is AssertionKind.STATED
        assert all(
            isinstance(state, Unknown)
            for state in (
                domain.epoch,
                domain.timescale,
                domain.resolution,
                domain.role,
                domain.declared_monotonic,
            )
        )  # "kind": "timestamp" in the catalog is not an epoch


def test_a_declared_timeline_clock_is_used_for_that_timeline_only(tmp_path: Path) -> None:
    declared = {"log_time": {"epoch": "unix", "timescale": "posix", "resolution": "1/1000000000"}}
    with connect(store(), tmp_path, timeline_clocks=declared) as source:
        domains = {d.field: d for d in source.catalog().domains}
        transform = source.transform
    log = domains["log_time"]
    assert [log.epoch.value, str(log.timescale.value)] == ["unix", "posix"]  # type: ignore[union-attr]
    assert log.resolution.value.denominator == 10**9  # type: ignore[union-attr]
    assert isinstance(domains["frame"].epoch, Unknown)
    with connect(store(), tmp_path / "other") as plain:
        assert plain.transform != transform  # the declaration is part of what produced the records


def test_every_record_is_stated_and_cites_a_catalog_document(tmp_path: Path) -> None:
    with connect(store(), tmp_path) as source:
        catalog = source.catalog()
    documents = {document.content_id: document for document in catalog.documents}
    assert {d.ref.object_id for d in catalog.documents} == {
        f"hub-acme:ds-warehouse-0914/{part}"
        for part in ("dataset", "segments", "schema", "indexes")
    }
    assert all(d.ref.connector_id == "deploy_rerun" for d in catalog.documents)
    assert all(d.ref.revision_token.startswith("records:") for d in catalog.documents)
    for row in catalog.rows:
        assert row.provenance.assertion_kind is AssertionKind.STATED
        assert row.provenance.transform == source.transform.id
        assert row.provenance.evidence.source in documents
        for cell in row.cells:
            assert isinstance(cell, Known | Unknown)
            assert isinstance(cell.provenance, Provenance)
            assert cell.provenance.assertion_kind is AssertionKind.STATED
        assert structured_record_from_json(row.to_json()) == row
    for domain in catalog.domains:
        assert domain.provenance.evidence.source in documents


def test_the_catalog_does_not_depend_on_the_order_of_the_export(tmp_path: Path) -> None:
    shuffled = document()
    for part in ("segments", "schema", "indexes"):
        shuffled[part] = list(reversed(shuffled[part]))
    with connect(store(), tmp_path / "a") as one, connect(store(), tmp_path / "b", shuffled) as two:
        assert one.catalog() == two.catalog()
        assert [d.data for d in one.catalog().documents] == [
            d.data for d in two.catalog().documents
        ]
        assert one.listing() == two.listing()
        assert one.transform == two.transform
        assert one.findings() == two.findings()


def test_a_value_that_cannot_be_stored_is_unknown_with_a_finding(tmp_path: Path) -> None:
    odd = document()
    odd["segments"][0]["property:RecordingInfo:name"] = "bad \ud800 name"
    with connect(store(), tmp_path, odd) as source:
        table, rows = _table(source, "rerun segments")
    cell = next(
        _cells(table, row)["property:RecordingInfo:name"]
        for row in rows
        if _cells(table, row)["rerun_segment_id"].value == "amr07_aisle12"
    )
    assert isinstance(cell, Unknown)
    assert codes(source) == ["deploy_rerun.value_unrepresentable"]


def test_no_credential_endpoint_or_path_reaches_a_finding_or_the_transform(tmp_path: Path) -> None:
    fake = store()
    fake.delete("episodes/legged01_slip.rrd")
    with connect(fake, tmp_path) as source:
        source.listing()
        text = repr(source.transform) + "".join(repr(f) for f in source.findings())
        text += json.dumps(source.transform.config, default=str)
    for secret in ("s3cr3t", "AKIDEXAMPLE", "127.0.0.1", str(tmp_path)):
        assert secret not in text


# --- Hostile stores ------------------------------------------------------------------------------


def test_a_redirect_is_refused_and_never_followed(tmp_path: Path) -> None:
    fake = store()
    fake.redirect = 302
    with connect(fake, tmp_path) as source:
        listing = source.listing()
    assert listing.entries == ()
    assert all(r.headers.get("host", "").startswith("127.0.0.1") for r in fake.requests)
    assert "deploy_rerun.object_not_found" in codes(source)


def test_a_store_that_ignores_ranges_is_a_finding_on_read(tmp_path: Path) -> None:
    fake = store()
    with connect(fake, tmp_path) as source:
        entry = next(e for e in source.listing().entries if e.key == BASE_KEY)
        fake.ignore_range = True
        with pytest.raises(ObjectReadError):
            source.fetch(entry, 10, 20)
    assert [c for c in codes(source) if c.startswith("deploy_s3.")]


# --- The network boundary ------------------------------------------------------------------------


def test_a_local_only_workspace_refuses_the_connector(tmp_path: Path) -> None:
    fake = store()
    with fake.serve() as endpoint, pytest.raises(LocalOnlyError):
        rerun_source(
            export_file(tmp_path),
            network=Workspace(tmp_path / "home"),  # network off, the default
            options={"storage": {"s3": {"endpoint": endpoint, "store": "hub-site"}}},
            credentials=CREDENTIALS,
        )
    assert fake.requests == []


def test_switching_to_local_only_refuses_the_next_request(tmp_path: Path) -> None:
    fake, workspace = store(), online(tmp_path)
    with fake.serve() as endpoint:
        source = rerun_source(
            export_file(tmp_path),
            network=workspace,
            options={"storage": {"s3": {"endpoint": endpoint, "store": "hub-site"}}},
            credentials=CREDENTIALS,
        )
        workspace.allow_network(False)
        with pytest.raises(LocalOnlyError):
            source.listing()
    assert fake.requests == []


def test_building_a_source_reads_the_export_and_sends_nothing(tmp_path: Path) -> None:
    fake = store()
    with connect(fake, tmp_path):
        pass
    assert fake.requests == []


def test_the_catalog_alone_needs_no_store_request(tmp_path: Path) -> None:
    fake = store()
    with connect(fake, tmp_path) as source:
        source.catalog()
    assert fake.requests == []  # entity paths and timelines are the export's, read locally


# --- The export file -----------------------------------------------------------------------------


def _refused(raw: bytes, match: str) -> None:
    with pytest.raises(ObjectStoreConfigError, match=match):
        parse_export(raw)


def test_the_export_is_strict_json_with_a_known_envelope() -> None:
    good = EXPORT.read_bytes()
    export = parse_export(good)
    assert isinstance(export, RerunExport)
    assert (export.catalog, export.dataset_id) == ("hub-acme", "ds-warehouse-0914")
    assert (len(export.segments), len(export.schema), len(export.indexes)) == (3, 3, 2)
    _refused(b"\xff\xfe", "strict JSON")
    _refused(b"[]", "not a JSON object")
    _refused(b'{"format": "neptune.rerun_catalog_export", "version": 1, "version": 1}', "strict")
    _refused(b'{"a": NaN}', "strict JSON")
    _refused(b"[" * 200 + b"]" * 200, "strict JSON")
    _refused(b'{"format": "other", "version": 1}', "not neptune.rerun_catalog_export")
    _refused(b'{"format": "neptune.rerun_catalog_export", "version": 2}', "version 1")


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"extra": 1}, "unknown members"),
        ({"catalog": "Hub/Acme"}, "catalog names this Hub"),
        ({"catalog": 5}, "catalog names this Hub"),
        ({"catalog": ""}, "catalog names this Hub"),
        ({"dataset": {"name": "x"}}, "dataset is an object"),
        ({"dataset": {"id": "a/b"}}, "dataset is an object"),
        ({"dataset": {"id": "a:b"}}, "dataset is an object"),
        ({"dataset": "ds"}, "dataset is an object"),
        ({"segments": {}}, "segments is a list of objects"),
        ({"segments": [1]}, "segments is a list of objects"),
        ({"schema": [[]]}, "schema is a list of objects"),
        ({"indexes": "log_time"}, "indexes is a list of objects"),
    ],
)
def test_a_malformed_envelope_is_refused_naming_what_is_wrong(
    patch: dict[str, Any], match: str
) -> None:
    broken = document() | patch
    with pytest.raises(ObjectStoreConfigError, match=match):
        parse_export(json.dumps(broken).encode())


def test_the_export_file_is_a_regular_file_the_operator_named(tmp_path: Path) -> None:
    with pytest.raises(ObjectStoreConfigError, match="cannot be opened"):
        read_export(str(tmp_path / "missing.json"))
    with pytest.raises(ObjectStoreConfigError, match="not a regular file"):
        read_export(str(tmp_path))  # a directory opens for reading but is no export
    real = export_file(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ObjectStoreConfigError, match="cannot be opened"):
        read_export(str(link))
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(ObjectStoreConfigError):
        read_export(str(fifo))
    assert read_export(real) == EXPORT.read_bytes()


def test_the_export_size_is_bounded_at_the_byte(tmp_path: Path) -> None:
    raw = EXPORT.read_bytes()
    path = export_file(tmp_path, raw=raw)
    assert read_export(path, limit=len(raw)) == raw
    with pytest.raises(ObjectStoreConfigError, match="larger than"):
        read_export(path, limit=len(raw) - 1)
    with pytest.raises(ObjectStoreConfigError, match="larger than"):
        rerun_source(path, network=online(tmp_path), options={"max_export_bytes": 1024})


def test_a_file_url_names_this_machines_files_only(tmp_path: Path) -> None:
    path = export_file(tmp_path)
    rerun_source(f"file://{path}", network=online(tmp_path), options={}, credentials=CREDENTIALS)
    for bad in ("file://elsewhere" + path, "", "a\x00b"):
        with pytest.raises(ObjectStoreConfigError):
            rerun_source(bad, network=online(tmp_path))


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"nope": 1}, "unknown options"),
        ({"storage": []}, "storage is an object"),
        ({"storage": {"ftp": {}}}, "storage is an object"),
        ({"storage": {"s3": "x"}}, "storage.s3"),
        ({"storage": {"s3": {"max_objects": 3}}}, "storage.s3"),
        ({"storage": {"s3": {"bogus": 3}}}, "unknown"),
        ({"timeline_clocks": []}, "timeline_clocks is an object"),
        ({"timeline_clocks": {"t": {"epoch": "unix", "x": 1}}}, "timeline_clocks.t"),
        ({"timeline_clocks": {"t": {"resolution": "0/5"}}}, "timeline_clocks.t"),
        ({"timeline_clocks": {"t": {"resolution": 5}}}, "timeline_clocks.t"),
        ({"max_objects": 0}, "max_objects"),
        ({"max_objects": True}, "max_objects"),
        ({"max_export_bytes": 10}, "max_export_bytes"),
    ],
)
def test_options_are_closed_and_checked(
    tmp_path: Path, options: dict[str, Any], match: str
) -> None:
    with pytest.raises(ObjectStoreConfigError, match=match):
        rerun_source(export_file(tmp_path), network=online(tmp_path), options=options)


# --- Limits and the inherited Source surface -----------------------------------------------------


def test_max_objects_stops_the_listing_with_a_finding_at_the_same_place_every_time(
    tmp_path: Path,
) -> None:
    runs = []
    for run in ("a", "b"):
        with connect(store(), tmp_path / run, max_objects=2) as source:
            listing = source.listing()
            runs.append((listing, source.findings()))
    assert runs[0] == runs[1]
    listing, findings = runs[0]
    assert not listing.complete and len(listing.entries) == 2
    assert [f.code for f in findings] == ["deploy_rerun.listing_limit"]
    assert [e.key for e in listing.entries] == sorted(RRD)[:2]  # in byte order of (bucket, key)


def test_every_inherited_source_method_runs_without_a_missing_attribute(tmp_path: Path) -> None:
    ledger = SourceLedger()
    with connect(store(), tmp_path, ledger=ledger) as source:
        walked = list(source.walk())
        assert [e.key for e in walked if isinstance(e, ObjectEntry)] == sorted(RRD)
        entry = next(e for e in walked if isinstance(e, ObjectEntry))
        assert source.entry(entry.location) == entry
        assert source.open(entry.location).read(4) == b"RRF2"
        with source.open(entry.location) as stream:
            artifact = digest_stream(stream, chunk_size=64)
        assert source.reader(entry.location, artifact).size == entry.size
        assert source.discover(ledger).complete
        assert source.findings() == ()
        with pytest.raises(ObjectReadError) as raised:
            source.entry(ExternalObjectRef("deploy_s3", "hub-site:fleet-rrd/none", "etag:0"))
        assert raised.value.code == "not_listed"
        with pytest.raises(TypeError):
            source.entry(ExternalObjectRef("deploy_gcs_other", "x", "y"))
        with pytest.raises(TypeError):
            source.entry("not a location")
