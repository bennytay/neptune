"""The ingest package and its receipt (ADR 0022): deterministic, verifiable, and honest about
what was read, what was not, and what went wrong.

The four worked examples are packaged in tests/integration/test_example_packages.py.
"""

import io
import random
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from neptune.identity import canonical_json
from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import content_id, digest_stream
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    NotCovered,
    Unknown,
)
from neptune.model.package import ReceiptEnvelope, Storage
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    Provenance,
    TransformRecord,
    adapter_locator,
)
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.schema import canonical_schema
from neptune.model.series import SeriesProvenance, step_template
from neptune.model.source import LocalPath
from neptune.model.time import NANOSECOND, ClockRole, Timestamp
from neptune.store.package import (
    ENVELOPE,
    MANIFEST,
    RECEIPT,
    RECEIPT_TEXT,
    PackageError,
    blob_path,
    package_files,
    package_id,
    read_envelope,
    read_files,
    read_package,
    series_path,
    table_path,
    write_envelope,
    write_package,
)
from neptune.store.receipt import build_receipt, render_receipt

LOG_BYTES = b"\x89MCAP0\r\n" + bytes(200)
NOTES_BYTES = b"field notes: wind 12 kn\n"
LOG = content_id(LOG_BYTES)


def records(adapter_version: str = "1.0.0") -> list[Any]:
    """A job over a folder: a log that was read, notes that were not, a log that moved away."""
    ledger = SourceLedger()
    log = digest_stream(io.BytesIO(LOG_BYTES))
    notes = digest_stream(io.BytesIO(NOTES_BYTES))
    ledger.observe(LocalPath("old/run.mcap"), log)
    ledger.observe(LocalPath("run.mcap"), log)
    ledger.observe(LocalPath("notes.txt"), notes)
    ledger.mark_absent(LocalPath("old/run.mcap"))
    mcap = transform_record(adapter_id="mcap", adapter_version=adapter_version, config={})

    def cite(*steps: Any) -> Provenance:
        return Provenance(EvidenceRef(LOG, steps), mcap.id, AssertionKind.OBSERVED)

    def id_of(kind: str, provenance: Provenance) -> RecordId:
        return evidence_record_id(kind, provenance.evidence, mcap)

    time_at = cite(ByteRange(0, 8), adapter_locator("mcap:time_field", {"name": "log_time"}))
    log_time = TimestampDomain(
        id=id_of("timestamp_domain", time_at),
        provenance=time_at,
        field="log_time",
        scope=(),
        role=Known(ClockRole.RECEIVE),
        resolution=Known(NANOSECOND),
        epoch=Unknown(),
        timescale=Unknown(),
        declared_monotonic=Unknown(),
    )
    header = cite(ByteRange(8, 20))
    run = Run(
        id=id_of("run", header),
        provenance=header,
        logical_id=Unknown(),
        machine=Known(LogicalId("manifest", "spot-07")),
        first=Known(Timestamp(1_000, log_time.id)),
        last=Known(Timestamp(9_000, log_time.id)),
    )
    channel = cite(ByteRange(28, 40))
    stream = Stream(
        id=id_of("stream", channel),
        provenance=channel,
        run=run.id,
        topic=Known("/imu"),
        schema_name=Unknown(),
        schema_encoding=Unknown(),
        schema_definition=Unknown(),
        message_encoding=Ambiguous((Candidate("json"), Candidate("cbor"))),
        metadata=(),
        clocks=(log_time.id,),
        message_count=Known(3),
        first=NotCovered(),
        last=NotCovered(),
        series=SeriesProvenance(
            LOG,
            (step_template("byte_range", per_row=("length", "offset")),),
            AssertionKind.OBSERVED,
        ),
    )
    findings = [
        ingest_finding(
            code="mcap.chunk_crc_mismatch",
            category=FindingCategory.CORRUPT,
            severity=Severity.ERROR,
            subject=EvidenceRef(LOG, (ByteRange(100, 50),)),
            transform=mcap,
            message="a chunk's CRC does not match; its messages are not in the series",
            records=[stream.id],
        ),
        ingest_finding(
            code="mcap.encoding_ambiguous",
            category=FindingCategory.AMBIGUOUS,
            severity=Severity.WARNING,
            subject=channel.evidence,
            transform=mcap,
            message="the channel's encoding reads as json or cbor",
            records=[stream.id],
        ),
    ]
    return [
        *ledger.artifacts(),
        *ledger.revisions(),
        *ledger.absences(),
        mcap,
        log_time,
        run,
        stream,
        *findings,
    ]


def stream_of(items: list[Any]) -> Stream:
    return next(record for record in items if isinstance(record, Stream))


def mcap_of(items: list[Any]) -> TransformRecord:
    return next(record for record in items if isinstance(record, TransformRecord))


# --- Determinism -------------------------------------------------------------------------------


def test_the_same_records_give_the_same_bytes_in_any_order() -> None:
    files = package_files(records())
    shuffled = records()
    random.Random(7).shuffle(shuffled)
    assert package_files(shuffled) == files
    assert package_id(files) == package_id(package_files(records()))


def test_a_new_adapter_version_is_a_new_receipt_and_a_new_package() -> None:
    old, new = package_files(records("1.0.0")), package_files(records("1.0.1"))
    assert package_id(old) != package_id(new)
    assert build_receipt(records("1.0.0")).id != build_receipt(records("1.0.1")).id
    assert old == package_files(records("1.0.0"))  # the old lineage is untouched


# --- What the receipt says ---------------------------------------------------------------------


def test_the_receipt_says_what_was_read_by_whom_and_what_was_not() -> None:
    receipt = build_receipt(records())
    mcap = mcap_of(records())
    by_path = {source.location.key[1]: source for source in receipt.sources}
    assert by_path["run.mcap"].read_by == (mcap.id,)
    assert by_path["notes.txt"].read_by == ()  # seen and hashed, but no adapter read it
    assert receipt.absent == (LocalPath("old/run.mcap"),)  # it moved; the ledger says so
    assert [(t.adapter_id, t.adapter_version) for t in receipt.transforms] == [("mcap", "1.0.0")]


def test_the_receipt_says_what_came_out_and_what_went_wrong() -> None:
    receipt = build_receipt(records())
    counts = dict(receipt.records)
    assert (counts["run"], counts["stream"], counts["machine"]) == (1, 1, 0)
    (run,) = receipt.runs
    assert (run.streams, run.first, run.last) == (
        1,
        Known(Timestamp(1_000, receipt.clocks[0].id)),
        Known(Timestamp(9_000, receipt.clocks[0].id)),
    )
    assert [(clock.field, clock.scope) for clock in receipt.clocks] == [("log_time", ())]
    assert [finding.severity for finding in receipt.findings] == [Severity.ERROR, Severity.WARNING]
    assert [(field.record, field.pointer) for field in receipt.ambiguous] == [
        (stream_of(records()).id, "/message_encoding")
    ]


def test_the_receipt_reads_the_same_for_people() -> None:
    text = render_receipt(build_receipt(records()))
    assert text == render_receipt(build_receipt(records()))
    assert "| `notes.txt` | 24 |" in text and "not read" in text
    assert "`old/run.mcap`" in text
    assert "1000 on `log_time`" in text  # ticks on their clock: nothing converted
    assert "**error** `mcap.chunk_crc_mismatch` (corrupt)" in text
    assert "`/message_encoding`" in text


# --- Write, read, verify -----------------------------------------------------------------------


def test_write_read_write_is_byte_identical(tmp_path: Path) -> None:
    files = package_files(records())
    assert write_package(tmp_path / "package", files) == package_id(files)
    package = read_package(tmp_path / "package")
    assert package.id == package_id(files)
    assert package.files() == files
    assert package.receipt == build_receipt(records())
    for kind in ("run", "stream", "machine"):
        assert (tmp_path / "package" / table_path(kind)).exists()  # empty tables are empty files
    assert (tmp_path / "package" / table_path("machine")).read_bytes() == b""


def test_the_envelope_never_changes_the_package(tmp_path: Path) -> None:
    files = package_files(records())
    root = tmp_path / "package"
    write_package(root, files)
    envelope = ReceiptEnvelope(
        receipt=build_receipt(records()).id,
        job="job-1",
        started="2026-10-01T09:00:00Z",
        finished="2026-10-01T09:00:01Z",
        host="lab-ws-3",
        root="/data/folder",
        durations=(("ingest", 0.5),),
    )
    write_envelope(root, envelope)
    assert read_envelope(root) == envelope
    assert read_package(root).id == package_id(files)
    assert ENVELOPE not in {file.path for file in read_package(root).manifest.files}
    with pytest.raises(PackageError, match="envelope is for receipt"):
        write_envelope(root, replace(envelope, receipt=mcap_of(records()).id))


def test_sources_are_referenced_unless_materialised() -> None:
    referenced = read_files(package_files(records()))
    assert {h.storage for h in referenced.manifest.sources} == {Storage.REFERENCED}
    files = package_files(records(), blobs={LOG: LOG_BYTES})
    assert files[blob_path(LOG)] == LOG_BYTES
    package = read_files(files)
    storage = {h.content_id: h.storage for h in package.manifest.sources}
    assert storage[LOG] is Storage.MATERIALISED
    assert package.blobs == {LOG: LOG_BYTES}
    with pytest.raises(PackageError, match="do not hash"):
        package_files(records(), blobs={LOG: NOTES_BYTES})
    with pytest.raises(PackageError, match="not a source artifact"):
        package_files(records(), blobs={content_id(b"other"): b"other"})


def test_series_are_named_by_their_stream() -> None:
    stream = stream_of(records())
    files = package_files(records(), series={stream.id: b"PAR1 fake parquet PAR1"})
    assert files[series_path(stream.id)] == b"PAR1 fake parquet PAR1"
    assert read_files(files).series == {stream.id: b"PAR1 fake parquet PAR1"}
    with pytest.raises(PackageError, match="not a stream"):
        package_files(records(), series={mcap_of(records()).id: b"x"})


def test_the_package_documents_validate_against_the_schema() -> None:
    schema = canonical_schema()
    files = package_files(records())
    for name, document in (("PackageManifest", MANIFEST), ("IngestReceipt", RECEIPT)):
        validator = Draft202012Validator(
            {"$defs": schema["$defs"], "$ref": f"#/$defs/{name}", "$schema": schema["$schema"]}
        )
        assert list(validator.iter_errors(canonical_json.loads(files[document]))) == []


# --- What the reader refuses -------------------------------------------------------------------


def with_manifest_for(files: dict[str, bytes], changed: dict[str, bytes]) -> dict[str, bytes]:
    """``files`` with some changed, and a manifest recomputed to match: only deeper checks fail."""
    package = read_files(files)
    edited = {**files, **changed}
    listed = tuple(
        replace(file, size=len(edited[file.path]), sha256=content_id(edited[file.path]))
        for file in package.manifest.files
    )
    manifest = replace(package.manifest, files=listed)
    return {**edited, MANIFEST: canonical_json.dumps(manifest.to_json())}


def test_tampered_files_are_refused(tmp_path: Path) -> None:
    files = package_files(records())
    root = tmp_path / "package"
    write_package(root, files)
    run_table = root / table_path("run")
    run_table.write_bytes(run_table.read_bytes().replace(b"spot-07", b"spot-08"))
    with pytest.raises(PackageError, match="does not match its size and hash"):
        read_package(root)


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (lambda f: {**f, "notes.txt": b"stray"}, "unlisted"),
        (lambda f: {k: v for k, v in f.items() if k != table_path("run")}, "missing"),
        (lambda f: with_manifest_for(f, {table_path("run"): b""}), "the manifest says otherwise"),
        (
            lambda f: with_manifest_for(
                f, {RECEIPT_TEXT: f[RECEIPT_TEXT].replace(b"not read", b"read")}
            ),
            "not the rendering",
        ),
        (
            lambda f: with_manifest_for(
                f, {RECEIPT: canonical_json.dumps(build_receipt(records("1.0.1")).to_json())}
            ),
            "not the receipt of these records",
        ),
        (lambda f: {k: v for k, v in f.items() if k != MANIFEST}, "no manifest"),
    ],
    ids=["stray file", "missing table", "count", "rendering", "receipt", "no manifest"],
)
def test_inconsistent_packages_are_refused(edit: Any, error: str) -> None:
    with pytest.raises(PackageError, match=error):
        read_files(edit(package_files(records())))


def test_a_table_out_of_order_is_refused() -> None:
    files = package_files(records())
    table = table_path("ingest_finding")
    lines = files[table].splitlines()
    swapped = b"\n".join(reversed(lines)) + b"\n"
    with pytest.raises(PackageError, match="sorted by id"):
        read_files(with_manifest_for(files, {table: swapped}))


def test_symlinks_and_occupied_directories_are_refused(tmp_path: Path) -> None:
    files = package_files(records())
    root = tmp_path / "package"
    write_package(root, files)
    with pytest.raises(PackageError, match="not an empty directory"):
        write_package(root, files)
    (root / "records" / "link.jsonl").symlink_to(root / table_path("run"))
    with pytest.raises(PackageError, match="symlink"):
        read_package(root)


def test_only_records_of_known_kinds_are_packaged() -> None:
    with pytest.raises(PackageError, match="known kind"):
        package_files([*records(), object()])
    with pytest.raises(PackageError, match="share an id"):
        package_files([*records(), stream_of(records())])
