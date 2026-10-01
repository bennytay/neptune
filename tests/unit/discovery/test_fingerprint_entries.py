"""``fingerprint`` over a walk's entries gives what ``scan`` gives: the runtime's split is exact."""

from pathlib import Path

from neptune.discovery.scan import fingerprint, scan
from neptune.discovery.source import LocalSource, SkippedEntry
from neptune.identity.revisions import SourceLedger


def test_walk_then_fingerprint_equals_scan(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"alpha")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"\x00beta")
    (tmp_path / "latest").symlink_to("a.txt")
    source = LocalSource(tmp_path)
    scanned = scan(source, SourceLedger())
    entries = tuple(source.walk())
    split = fingerprint(source, SourceLedger(), entries)
    assert split.observations == scanned.observations
    assert split.symlinks == scanned.symlinks
    assert split.skipped == scanned.skipped == ()
    assert split.absences == scanned.absences == ()


def test_a_file_that_vanishes_between_walk_and_fingerprint_is_blind_not_absent(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_bytes(b"alpha")
    (tmp_path / "b.txt").write_bytes(b"beta")
    source, ledger = LocalSource(tmp_path), SourceLedger()
    fingerprint(source, ledger, tuple(source.walk()))
    entries = tuple(source.walk())
    (tmp_path / "b.txt").unlink()
    result = fingerprint(source, ledger, entries)
    assert [o.revision.location.to_json()["path"] for o in result.observations] == ["a.txt"]
    assert [(s.raw_path, str(s.reason)) for s in result.skipped] == [(b"b.txt", "missing")]
    assert result.absences == ()  # the scan could not see b.txt: nothing is asserted
    assert all(isinstance(s, SkippedEntry) for s in result.skipped)
    later = fingerprint(source, ledger, tuple(source.walk()))
    assert [a.location.to_json()["path"] for a in later.absences] == ["b.txt"]
