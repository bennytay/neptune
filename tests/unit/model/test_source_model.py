from typing import Any

import pytest

from neptune.identity import canonical_json
from neptune.model.ids import (
    ContentId,
    ExternalObjectRef,
    LogicalId,
    RecordId,
    parse_content_id,
    parse_record_id,
)
from neptune.model.jsonvalue import JsonValue
from neptune.model.record import SCHEMA_VERSION
from neptune.model.source import (
    LocalPath,
    RawLocalPath,
    SourceAbsence,
    SourceArtifact,
    SourceRevision,
    local_location,
    location_from_json,
    source_absence_from_json,
    source_artifact_from_json,
    source_revision_from_json,
)

CID = ContentId("sha256:" + "a" * 64)
RID = RecordId("rec:sha256:" + "b" * 64)
OTHER = RecordId("rec:sha256:" + "c" * 64)


@pytest.mark.parametrize(
    "text", ["", "sha256:" + "a" * 63, "sha256:" + "A" * 64, "SHA256:" + "a" * 64, "a" * 64]
)
def test_parse_content_id_rejects_malformed(text: str) -> None:
    with pytest.raises(ValueError, match="content id"):
        parse_content_id(text)


def test_parse_record_id_rejects_content_id() -> None:
    with pytest.raises(ValueError, match="record id"):
        parse_record_id(CID)
    with pytest.raises(ValueError, match="content id"):
        parse_content_id(RID)


@pytest.mark.parametrize("path", ["a", "a/b.mcap", "dir with space/é", "a\\b", "..a/b.."])
def test_local_path_accepts_relative_paths(path: str) -> None:
    assert LocalPath(path).path == path


@pytest.mark.parametrize(
    "path", ["", "/abs", "a/", "a//b", "./a", "a/./b", "../a", "a/../b", "a\x00b", "\udcff"]
)
def test_local_path_rejects_unsafe_or_unrepresentable(path: str) -> None:
    with pytest.raises(ValueError):
        LocalPath(path)


def test_location_keys_ignore_revision_token() -> None:
    a = ExternalObjectRef("s3", "bucket/run1.mcap", "etag-1")
    b = ExternalObjectRef("s3", "bucket/run1.mcap", "etag-2")
    assert a.key == b.key
    assert a.key != LocalPath("bucket/run1.mcap").key


@pytest.mark.parametrize(
    ("connector", "object_id", "token"), [("S3", "o", "t"), ("s3", "", "t"), ("s3", "o", "")]
)
def test_external_ref_validation(connector: str, object_id: str, token: str) -> None:
    with pytest.raises(ValueError):
        ExternalObjectRef(connector, object_id, token)


def test_logical_id_validation() -> None:
    assert LogicalId("serial", "SPOT-1234").to_json() == {
        "namespace": "serial",
        "value": "SPOT-1234",
    }
    with pytest.raises(ValueError):
        LogicalId("Serial", "x")
    with pytest.raises(ValueError):
        LogicalId("serial", "")


@pytest.mark.parametrize(("size", "chunks"), [(0, 0), (1, 1), (16, 1), (17, 2)])
def test_artifact_chunk_count_invariant(size: int, chunks: int) -> None:
    SourceArtifact(CID, size, 16, (CID,) * chunks)
    with pytest.raises(ValueError, match="chunk hashes"):
        SourceArtifact(CID, size, 16, (CID,) * (chunks + 1))


def test_artifact_rejects_bad_sizes() -> None:
    with pytest.raises(ValueError, match="size"):
        SourceArtifact(CID, -1, 16, ())
    with pytest.raises(ValueError, match="chunk_size"):
        SourceArtifact(CID, 0, 0, ())


def test_revision_supersedes_at_most_one_other() -> None:
    other = RecordId("rec:sha256:" + "c" * 64)
    SourceRevision(RID, LocalPath("a"), CID, (other,))
    with pytest.raises(ValueError, match="at most one"):
        SourceRevision(RID, LocalPath("a"), CID, (other, other))
    with pytest.raises(ValueError, match="itself"):
        SourceRevision(RID, LocalPath("a"), CID, (RID,))


def test_raw_local_path_is_only_for_non_utf8() -> None:
    raw = RawLocalPath(b"runs/r\xff.mcap")
    assert raw.to_json() == {"kind": "local_raw", "path_hex": "72756e732f72ff2e6d636170"}
    assert raw.key != LocalPath("runs/r�.mcap").key
    with pytest.raises(ValueError, match="use LocalPath"):
        RawLocalPath(b"runs/ok.mcap")


@pytest.mark.parametrize(
    "raw", [b"", b"/\xff", b"\xff/", b"a//\xff", b"./\xff", b"\xff/../b", b"\xff\x00"]
)
def test_raw_local_path_rejects_unsafe(raw: bytes) -> None:
    with pytest.raises(ValueError):
        RawLocalPath(raw)


def test_local_location_picks_the_one_representation() -> None:
    assert local_location(b"a/\xc3\xa9") == LocalPath("a/é")
    assert local_location(b"a/\xe9") == RawLocalPath(b"a/\xe9")


def test_absence_supersedes_exactly_one() -> None:
    SourceAbsence(RID, LocalPath("a"), (RecordId("rec:sha256:" + "c" * 64),))
    with pytest.raises(ValueError, match="exactly one"):
        SourceAbsence(RID, LocalPath("a"), ())
    with pytest.raises(ValueError, match="itself"):
        SourceAbsence(RID, LocalPath("a"), (RID,))


# --- JSON ----------------------------------------------------------------------------------------

LOCATIONS = [
    LocalPath("runs/run1.mcap"),
    RawLocalPath(b"runs/r\xff.mcap"),
    ExternalObjectRef("s3", "bucket/run1.mcap", "etag-1"),
]


@pytest.mark.parametrize("location", LOCATIONS, ids=lambda loc: type(loc).__name__)
def test_locations_round_trip(location: Any) -> None:
    data = canonical_json.loads(canonical_json.dumps(location.to_json()))
    assert location_from_json(data) == location


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "local"},
        {"kind": "local", "path": "a", "extra": 1},
        {"kind": "local", "path": "/abs"},
        {"kind": "local_raw", "path_hex": "72FF"},  # canonical hex is lowercase
        {"kind": "local_raw", "path_hex": "72f"},
        {"kind": "local_raw", "path_hex": "7275"},  # valid UTF-8 must be a LocalPath
        {"kind": "external", "connector_id": "s3", "object_id": "o"},
        {"kind": "path", "path": "a"},
        "runs/run1.mcap",
    ],
)
def test_location_json_is_parsed_strictly(data: JsonValue) -> None:
    with pytest.raises(ValueError):
        location_from_json(data)


def test_ledger_records_carry_the_envelope_and_round_trip() -> None:
    artifact = SourceArtifact(CID, 17, 16, (CID, CID))
    records: list[tuple[Any, Any]] = [
        (artifact, source_artifact_from_json),
        (SourceRevision(RID, RawLocalPath(b"r\xff"), CID, (OTHER,)), source_revision_from_json),
        (SourceAbsence(RID, LocalPath("a"), (OTHER,)), source_absence_from_json),
    ]
    for record, decode in records:
        data = record.to_json()
        assert (data["kind"], data["schema_version"]) == (record.kind, SCHEMA_VERSION)
        assert decode(canonical_json.loads(canonical_json.dumps(data))) == record
    assert artifact.to_json() == {
        "chunk_size": 16,
        "chunks": [CID, CID],
        "content_id": CID,
        "kind": "source_artifact",
        "schema_version": SCHEMA_VERSION,
        "size": 17,
    }


@pytest.mark.parametrize(
    ("decode", "data"),
    [
        (source_artifact_from_json, {"size": 1.0, "chunks": [CID]}),
        (source_artifact_from_json, {"size": 1, "chunks": CID}),
        (source_artifact_from_json, {"size": 1, "chunks": []}),  # 1 byte needs one chunk hash
        (source_artifact_from_json, {"content_id": RID}),
        (source_revision_from_json, {"supersedes": RID}),
        (source_revision_from_json, {"location": "a"}),
        (source_absence_from_json, {"supersedes": []}),
        (source_absence_from_json, {"kind": "source_revision"}),
    ],
)
def test_ledger_json_is_parsed_strictly(decode: Any, data: dict[str, JsonValue]) -> None:
    samples: dict[Any, dict[str, JsonValue]] = {
        source_artifact_from_json: dict(SourceArtifact(CID, 1, 16, (CID,)).to_json()),
        source_revision_from_json: dict(SourceRevision(RID, LocalPath("a"), CID, ()).to_json()),
        source_absence_from_json: dict(SourceAbsence(RID, LocalPath("a"), (OTHER,)).to_json()),
    }
    with pytest.raises(ValueError):
        decode({**samples[decode], **data})
