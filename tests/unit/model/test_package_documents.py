"""The package's own documents: manifest, receipt core and envelope, read strictly (ADR 0022).

How the store builds, writes and verifies them is tested in tests/unit/store/.
"""

from dataclasses import replace
from typing import Any

import pytest

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import Known, NotCovered, Unknown
from neptune.model.package import (
    AmbiguousField,
    IngestReceipt,
    PackageFile,
    PackageManifest,
    ReceiptClock,
    ReceiptEntity,
    ReceiptEnvelope,
    ReceiptFinding,
    ReceiptRun,
    ReceiptSource,
    ReceiptStream,
    ReceiptTransform,
    SourceHandle,
    Storage,
    ingest_receipt_from_json,
    package_manifest_from_json,
    receipt_envelope_from_json,
)
from neptune.model.record import OLDEST_READABLE_VERSION, SCHEMA_VERSION, SchemaVersionError
from neptune.model.source import LocalPath
from neptune.model.time import Timestamp

SOURCE = content_id(b"flight log")
TRANSFORM = transform_record(adapter_id="ulog", adapter_version="1.0.0", config={})
RUN = RecordId("rec:sha256:" + "a" * 64)
STREAM = RecordId("rec:sha256:" + "b" * 64)
CLOCK = RecordId("rec:sha256:" + "c" * 64)
FINDING = RecordId("rec:sha256:" + "d" * 64)
RECEIPT = RecordId("rec:sha256:" + "e" * 64)


def manifest(**changes: Any) -> PackageManifest:
    value = PackageManifest(
        receipt=RECEIPT,
        tables=(("run", 1), ("stream", 1)),
        sources=(SourceHandle(SOURCE, 10, Storage.REFERENCED),),
        files=(
            PackageFile("receipt.json", 3, content_id(b"abc")),
            PackageFile("records/run.jsonl", 0, content_id(b"")),
        ),
        store={"parquet": {"compression": "zstd"}},
    )
    return replace(value, **changes)


def receipt(**changes: Any) -> IngestReceipt:
    value = IngestReceipt(
        id=RECEIPT,
        sources=(ReceiptSource(LocalPath("flight.ulg"), SOURCE, 10, (TRANSFORM.id,)),),
        absent=(LocalPath("old.ulg"),),
        transforms=(
            ReceiptTransform(
                TRANSFORM.id, "ulog", "1.0.0", TRANSFORM.config_hash, (("pyulog", "1.1"),), ()
            ),
        ),
        records=(("run", 1), ("stream", 1)),
        clocks=(ReceiptClock(CLOCK, "timestamp", ()),),
        runs=(
            ReceiptRun(
                RUN,
                NotCovered(),
                Known(LogicalId("px4.sys_uuid", "0002")),
                Known(Timestamp(12_000_000, CLOCK)),
                NotCovered(),
                1,
            ),
        ),
        streams=(
            ReceiptStream(
                STREAM, RUN, Known("sensor_accel"), (CLOCK,), Unknown(), Unknown(), Unknown()
            ),
        ),
        entities=(ReceiptEntity(RUN, "machine", (LogicalId("px4.sys_uuid", "0002"),)),),
        findings=(
            ReceiptFinding(
                FINDING, "ulog.dropout", FindingCategory.MISSING, Severity.WARNING, "gap"
            ),
        ),
        ambiguous=(AmbiguousField(STREAM, "/topic"),),
    )
    return replace(value, **changes)


def envelope(**changes: Any) -> ReceiptEnvelope:
    value = ReceiptEnvelope(
        receipt=RECEIPT,
        job="job-7",
        started="2026-10-01T09:00:00Z",
        finished="2026-10-01T09:00:02.500Z",
        host="lab-ws-3",
        root="/data/field-2026-09-30",
        durations=(("ingest", 1.75), ("scan", 0.25)),
    )
    return replace(value, **changes)


DOCUMENTS: list[tuple[Any, Any]] = [
    (manifest(), package_manifest_from_json),
    (receipt(), ingest_receipt_from_json),
    (envelope(), receipt_envelope_from_json),
]


@pytest.mark.parametrize(("document", "read"), DOCUMENTS, ids=["manifest", "receipt", "envelope"])
def test_documents_round_trip_and_are_read_strictly(document: Any, read: Any) -> None:
    line = canonical_json.dumps(document.to_json())
    assert read(canonical_json.loads(line)) == document
    data = document.to_json()
    assert (data["kind"], data["schema_version"]) == (document.kind, OLDEST_READABLE_VERSION)
    with pytest.raises(SchemaVersionError, match="newer"):
        read({**data, "schema_version": SCHEMA_VERSION + 1, "later": 1})
    for broken in (
        {**data, "extra": 1},
        {key: value for key, value in data.items() if key != "kind"},
        {**data, "kind": "run"},
    ):
        with pytest.raises(ValueError):
            read(broken)


@pytest.mark.parametrize(
    "change",
    [
        {"tables": (("stream", 1), ("run", 1))},  # sorted by kind
        {"tables": (("run", -1),)},
        {"files": (PackageFile("records/run.jsonl", 0, content_id(b"")),) * 2},
        {"sources": (SourceHandle(SOURCE, 10, Storage.REFERENCED),) * 2},
        {"store": ["zstd"]},
        {"receipt": "receipt.json"},
    ],
)
def test_manifest_fields_are_checked(change: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        manifest(**change)


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../outside", "records//run.jsonl", "records/./run.jsonl", "", "a b"]
)
def test_package_paths_stay_inside_the_package(path: str) -> None:
    with pytest.raises(ValueError):
        PackageFile(path, 0, content_id(b""))


@pytest.mark.parametrize(
    "change",
    [
        {
            "sources": (
                ReceiptSource(LocalPath("b"), SOURCE, 1, ()),
                ReceiptSource(LocalPath("a"), SOURCE, 1, ()),
            )
        },
        {"absent": (LocalPath("flight.ulg"),)},  # present and gone at once
        {
            "findings": (
                ReceiptFinding(FINDING, "b.x", FindingCategory.MISSING, Severity.INFO, "m"),
                ReceiptFinding(RUN, "a.x", FindingCategory.CORRUPT, Severity.ERROR, "m"),
            )
        },  # most severe first
        {"ambiguous": (AmbiguousField(STREAM, "/b"), AmbiguousField(STREAM, "/a"))},
        {"records": (("run", True),)},
        {"runs": (receipt().runs[0],) * 2},
    ],
)
def test_receipt_order_and_contents_are_checked(change: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        receipt(**change)


def test_a_source_nothing_read_has_no_readers() -> None:
    unread = ReceiptSource(LocalPath("notes.txt"), content_id(b"notes"), 5, ())
    value = receipt(sources=(receipt().sources[0], unread))
    assert ingest_receipt_from_json(value.to_json()) == value
    with pytest.raises(ValueError):
        AmbiguousField(STREAM, "topic")  # a pointer to a field starts with '/'


@pytest.mark.parametrize(
    "change",
    [
        {"started": "2026-10-01 09:00:00"},  # RFC 3339, UTC
        {"finished": "2026-10-01T19:00:00+10:00"},
        {"durations": (("scan", -1.0),)},
        {"durations": (("scan", 1),)},
        {"host": ""},
        {"receipt": "latest"},
    ],
)
def test_envelope_fields_are_checked(change: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        envelope(**change)
