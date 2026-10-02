"""IngestFinding: what went wrong, where, and what found it (ADR 0017 §9)."""

from dataclasses import replace
from typing import Any

import pytest

from neptune.identity import canonical_json
from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import transform_record
from neptune.model.finding import (
    MAX_MESSAGE_LENGTH,
    FindingCategory,
    IngestFinding,
    Severity,
    ingest_finding_from_json,
    subject_from_json,
    subject_to_json,
)
from neptune.model.ids import ExternalObjectRef, RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import ByteRange, EvidenceRef, RowCell, adapter_locator
from neptune.model.record import OLDEST_READABLE_VERSION, Family
from neptune.model.source import LocalPath, RawLocalPath

LOG = content_id(b"\x89MCAP0\r\n" + bytes(1024))
REGISTER = content_id(b"asset,defects\npump-7,\n")
MCAP = transform_record(adapter_id="mcap", adapter_version="1.0.0", config={})
DISCOVERY = transform_record(adapter_id="neptune.discovery", adapter_version="1", config={})
CHUNK = EvidenceRef(LOG, (ByteRange(512, 256),))
OTHER_CHUNK = EvidenceRef(LOG, (ByteRange(768, 256),))
STREAM = RecordId("rec:sha256:" + "a" * 64)
DOMAIN = RecordId("rec:sha256:" + "b" * 64)


def crc_mismatch(**changes: Any) -> IngestFinding:
    fields: dict[str, Any] = {
        "code": "mcap.chunk_crc_mismatch",
        "category": FindingCategory.CORRUPT,
        "severity": Severity.ERROR,
        "subject": CHUNK,
        "transform": MCAP,
        "message": "chunk 3 fails its CRC; its messages are not ingested",
        "details": {"expected_crc": 1234, "actual_crc": 99},
    }
    fields.update(changes)
    return ingest_finding(**fields)


def round_trip(finding: IngestFinding) -> dict[str, JsonValue]:
    line = canonical_json.dumps(finding.to_json())
    decoded = ingest_finding_from_json(canonical_json.loads(line))
    assert decoded == finding
    assert canonical_json.dumps(decoded.to_json()) == line
    data = canonical_json.loads(line)
    assert isinstance(data, dict)
    return data


# --- What a finding can be about ---------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        CHUNK,
        EvidenceRef(REGISTER, (RowCell(1, 1, "defects"),)),
        EvidenceRef(LOG, (ByteRange(512, 256), adapter_locator("mcap:message", {"index": 7}))),
        EvidenceRef(ExternalObjectRef("s3", "bucket/run.mcap", "etag-1"), (ByteRange(0, 8),)),
        LocalPath("runs/locked"),
        RawLocalPath(b"runs/r\xff"),
        ExternalObjectRef("s3", "bucket/run.mcap", "etag-1"),
    ],
    ids=lambda s: type(s).__name__,
)
def test_a_finding_can_cite_bytes_a_cell_or_a_location_with_no_bytes(subject: Any) -> None:
    finding = crc_mismatch(subject=subject, transform=DISCOVERY)
    assert round_trip(finding)["subject"] == subject_to_json(subject)
    assert subject_from_json(subject_to_json(subject)) == subject


def test_json_shape() -> None:
    finding = crc_mismatch(related=[OTHER_CHUNK], records=[STREAM])
    assert round_trip(finding) == {
        "category": "corrupt",
        "code": "mcap.chunk_crc_mismatch",
        "details": {"actual_crc": 99, "expected_crc": 1234},
        "id": finding.id,
        "kind": "ingest_finding",
        "message": "chunk 3 fails its CRC; its messages are not ingested",
        "records": [STREAM],
        "related": [OTHER_CHUNK.to_json()],
        "schema_version": OLDEST_READABLE_VERSION,
        "severity": "error",
        "subject": {"kind": "evidence", "ref": CHUNK.to_json()},
        "transform": MCAP.id,
    }
    assert IngestFinding.family is Family.FINDING


def test_an_unreadable_directory_is_a_location_finding() -> None:
    finding = crc_mismatch(
        code="neptune.discovery.unreadable",
        category=FindingCategory.SKIPPED,
        severity=Severity.WARNING,
        subject=LocalPath("runs/locked"),
        transform=DISCOVERY,
        message="directory could not be listed; nothing below it is asserted absent",
        details={},
    )
    assert round_trip(finding)["subject"] == {"kind": "local", "path": "runs/locked"}


def test_a_conflict_cites_both_sides_and_the_records_it_qualifies() -> None:
    finding = crc_mismatch(
        code="mcap.clock_not_monotonic",
        category=FindingCategory.INCONSISTENT,
        severity=Severity.WARNING,
        related=[OTHER_CHUNK],
        records=[DOMAIN, STREAM],
    )
    assert finding.related == (OTHER_CHUNK,)
    assert finding.records == (STREAM, DOMAIN)  # sorted by the builder


# --- Construction rules ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"code": "crc_mismatch"}, ValueError),  # no producer
        ({"code": "MCAP.crc"}, ValueError),
        ({"code": "mcap."}, ValueError),
        ({"code": "mcap..crc"}, ValueError),
        ({"category": "corrupt"}, TypeError),
        ({"severity": "fatal"}, TypeError),
        ({"subject": "runs/run.mcap"}, TypeError),
        ({"subject": LOG}, TypeError),
        ({"message": ""}, ValueError),
        ({"message": "two\nlines"}, ValueError),
        ({"message": "x" * (MAX_MESSAGE_LENGTH + 1)}, ValueError),
        ({"message": "\ud800"}, ValueError),
        ({"details": [1]}, TypeError),
        ({"details": {"Expected CRC": 1}}, ValueError),
        ({"related": (CHUNK,)}, ValueError),  # the subject is not also related
        ({"related": (OTHER_CHUNK, OTHER_CHUNK)}, ValueError),
        ({"related": [OTHER_CHUNK]}, TypeError),  # a list is mutable
        ({"related": (OTHER_CHUNK.to_json(),)}, TypeError),
        ({"records": (DOMAIN, STREAM)}, ValueError),  # unsorted
        ({"records": (STREAM, STREAM)}, ValueError),
        ({"records": ("stream",)}, ValueError),
        ({"transform": "mcap"}, ValueError),
        ({"id": "rec:x"}, ValueError),
    ],
)
def test_malformed_findings_are_rejected(changes: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        replace(crc_mismatch(), **changes)


def test_the_longest_message_is_accepted() -> None:
    assert len(crc_mismatch(message="x" * MAX_MESSAGE_LENGTH).message) == MAX_MESSAGE_LENGTH


def test_findings_hash_by_id_despite_their_details_object() -> None:
    assert {crc_mismatch(), crc_mismatch()} == {crc_mismatch()}


# --- Strict JSON -------------------------------------------------------------------------------


def mutate(**changes: JsonValue) -> JsonValue:
    data = dict(crc_mismatch().to_json())
    data.update(changes)
    return data


@pytest.mark.parametrize(
    "data",
    [
        mutate(category="broken"),
        mutate(severity="ERROR"),
        mutate(details=[]),
        mutate(related={}),
        mutate(records="rec:sha256:" + "a" * 64),
        mutate(subject={"kind": "evidence", "ref": CHUNK.to_json(), "extra": 1}),
        mutate(subject={"kind": "evidence"}),
        mutate(subject={"kind": "path", "path": "a"}),
        mutate(subject={"kind": "local_raw", "path_hex": "FF"}),
        mutate(confidence=0.5),
        {k: v for k, v in crc_mismatch().to_json().items() if k != "message"},
    ],
)
def test_json_is_parsed_strictly(data: JsonValue) -> None:
    with pytest.raises(ValueError):
        ingest_finding_from_json(data)
