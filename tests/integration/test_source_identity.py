"""MVL-2 and MVL-59 acceptance, end to end: walk a real directory, hash it, record it, reconcile."""

import os
from pathlib import Path

import pytest

from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource
from neptune.identity import canonical_json
from neptune.identity.revisions import SourceLedger
from neptune.model.source import LocalPath, RawLocalPath, SourceAbsence, SourceRevision

pytestmark = pytest.mark.integration


def snapshot(ledger: SourceLedger) -> bytes:
    rows = [
        *(a.to_json() for a in ledger.artifacts()),
        *(r.to_json() for r in ledger.revisions()),
        *(a.to_json() for a in ledger.absences()),
    ]
    return b"".join(canonical_json.dumps(row) + b"\n" for row in rows)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs/run1.mcap").write_bytes(b"\x89MCAP0\r\n" + bytes(range(256)) * 64)
    (tmp_path / "robot.urdf").write_bytes(b"<robot name='spot'/>")
    (tmp_path / "copy_of_robot.urdf").write_bytes(b"<robot name='spot'/>")
    return tmp_path


# MVL-2


def test_identical_bytes_ingested_twice_resolve_idempotently(corpus: Path) -> None:
    ledger = SourceLedger()
    scan(LocalSource(corpus), ledger)
    before = snapshot(ledger)
    second = scan(LocalSource(corpus), ledger)
    assert not any(o.new_artifact or o.new_revision for o in second.observations)
    assert second.absences == ()
    assert snapshot(ledger) == before
    fresh = SourceLedger()
    scan(LocalSource(corpus), fresh)
    assert snapshot(fresh) == before


def test_changed_file_creates_revision_without_mutating_history(corpus: Path) -> None:
    ledger = SourceLedger()
    scan(LocalSource(corpus), ledger)
    original = ledger.head(LocalPath("runs/run1.mcap"))
    assert isinstance(original, SourceRevision)
    before = set(ledger.revisions())

    (corpus / "runs/run1.mcap").write_bytes(b"\x89MCAP0\r\n truncated")
    scan(LocalSource(corpus), ledger)

    head = ledger.head(LocalPath("runs/run1.mcap"))
    assert isinstance(head, SourceRevision) and head.supersedes == (original.id,)
    assert before < set(ledger.revisions())
    assert ledger.artifact(original.content_id) is not None


def test_rename_or_move_creates_no_new_evidence(corpus: Path) -> None:
    ledger = SourceLedger()
    scan(LocalSource(corpus), ledger)
    artifacts = ledger.artifacts()

    (corpus / "archive").mkdir()
    (corpus / "runs/run1.mcap").rename(corpus / "archive/2026-09-30.mcap")
    result = scan(LocalSource(corpus), ledger)

    assert not any(o.new_artifact for o in result.observations)
    assert ledger.artifacts() == artifacts
    moved = ledger.head(LocalPath("archive/2026-09-30.mcap"))
    old = ledger.head(LocalPath("runs/run1.mcap"))
    assert isinstance(moved, SourceRevision) and isinstance(old, SourceAbsence)
    assert old.supersedes[0] in {
        r.id for r in ledger.revisions() if r.content_id == moved.content_id
    }


def test_duplicate_files_are_one_artifact(corpus: Path) -> None:
    ledger = SourceLedger()
    scan(LocalSource(corpus), ledger)
    assert len(ledger.artifacts()) == 2
    assert len(ledger.revisions()) == 3


# MVL-59


def test_non_utf8_name_is_ingested_and_round_trips(tmp_path: Path) -> None:
    raw = os.fsencode(tmp_path) + b"/run\xff.mcap"
    try:
        Path(os.fsdecode(raw)).write_bytes(b"\x89MCAP0\r\n")
    except OSError:
        pytest.skip("filesystem rejects non-UTF-8 names")
    ledger = SourceLedger()
    [observation] = scan(LocalSource(tmp_path), ledger).observations
    assert observation.revision.location == RawLocalPath(b"run\xff.mcap")
    assert canonical_json.loads(canonical_json.dumps(observation.revision.to_json()))
    with LocalSource(tmp_path).open(RawLocalPath(b"run\xff.mcap")) as stream:
        assert stream.read() == b"\x89MCAP0\r\n"


def test_deleted_file_is_absent_and_reappearance_supersedes(corpus: Path) -> None:
    ledger = SourceLedger()
    scan(LocalSource(corpus), ledger)
    data = (corpus / "robot.urdf").read_bytes()
    (corpus / "robot.urdf").unlink()

    [absence] = scan(LocalSource(corpus), ledger).absences
    assert absence.location == LocalPath("robot.urdf")
    assert scan(LocalSource(corpus), ledger).absences == ()  # idempotent

    (corpus / "robot.urdf").write_bytes(data)
    scan(LocalSource(corpus), ledger)
    head = ledger.head(LocalPath("robot.urdf"))
    assert isinstance(head, SourceRevision) and head.supersedes == (absence.id,)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_file_under_unreadable_directory_is_not_marked_absent(corpus: Path) -> None:
    ledger = SourceLedger()
    scan(LocalSource(corpus), ledger)
    (corpus / "runs").chmod(0)
    try:
        result = scan(LocalSource(corpus), ledger)
    finally:
        (corpus / "runs").chmod(0o755)
    assert result.absences == ()
    assert isinstance(ledger.head(LocalPath("runs/run1.mcap")), SourceRevision)


def test_symlink_target_recorded_and_never_read_through(
    corpus: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.mcap").write_bytes(b"outside the root")
    (corpus / "linked.mcap").symlink_to(outside / "secret.mcap")

    ledger = SourceLedger()
    result = scan(LocalSource(corpus), ledger)
    [link] = result.symlinks
    assert link.location == LocalPath("linked.mcap")
    assert link.target == os.fsencode(outside / "secret.mcap")
    assert ledger.head(LocalPath("linked.mcap")) is None
    assert b"outside the root" not in snapshot(ledger)


def test_directory_replaced_by_symlink_is_not_marked_absent(corpus: Path) -> None:
    ledger = SourceLedger()
    scan(LocalSource(corpus), ledger)
    (corpus / "runs").rename(corpus / "runs.real")
    (corpus / "runs").symlink_to(corpus / "runs.real")
    result = scan(LocalSource(corpus), ledger)
    assert [a.location for a in result.absences] == []
    assert isinstance(ledger.head(LocalPath("runs/run1.mcap")), SourceRevision)


def test_file_replaced_by_symlink_is_absent(corpus: Path) -> None:
    ledger = SourceLedger()
    scan(LocalSource(corpus), ledger)
    (corpus / "robot.urdf").unlink()
    (corpus / "robot.urdf").symlink_to(corpus / "copy_of_robot.urdf")
    result = scan(LocalSource(corpus), ledger)
    assert [a.location for a in result.absences] == [LocalPath("robot.urdf")]
    assert [s.location for s in result.symlinks] == [LocalPath("robot.urdf")]
