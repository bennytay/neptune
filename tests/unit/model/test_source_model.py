import pytest

from neptune.model.ids import (
    ContentId,
    ExternalObjectRef,
    LogicalId,
    RecordId,
    parse_content_id,
    parse_record_id,
)
from neptune.model.source import (
    LocalPath,
    RawLocalPath,
    SourceAbsence,
    SourceArtifact,
    SourceRevision,
    local_location,
)

CID = ContentId("sha256:" + "a" * 64)
RID = RecordId("rec:sha256:" + "b" * 64)


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
