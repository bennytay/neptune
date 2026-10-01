"""Scratch space: private, never in the source tree, gone on exit, cleared on resume."""

import os
import stat
from pathlib import Path

import pytest

from neptune.discovery.scratch import (
    LOCK_NAME,
    ScratchError,
    clear_scratch,
    prepare_private_root,
    scratch_space,
)


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_scratch_is_private_and_removed_on_exit(tmp_path: Path) -> None:
    root = tmp_path / "private"
    with scratch_space(root) as scratch:
        assert scratch.parent == root
        assert mode(root) == 0o700 and mode(scratch) == 0o700
        assert (scratch / LOCK_NAME).exists()
        (scratch / "spool").write_bytes(b"x" * 10)
        (scratch / "deeper").mkdir()
        (scratch / "deeper" / "link").symlink_to(tmp_path)  # a planted link is not followed
    assert not scratch.exists()
    assert tmp_path.exists() and list(root.iterdir()) == []


def test_scratch_is_removed_when_the_block_raises(tmp_path: Path) -> None:
    root = tmp_path / "private"
    with pytest.raises(RuntimeError), scratch_space(root) as scratch:
        (scratch / "partial").write_bytes(b"x")
        raise RuntimeError("the parser crashed")
    assert list(root.iterdir()) == []


def test_two_spaces_coexist_and_do_not_collide(tmp_path: Path) -> None:
    with scratch_space(tmp_path / "p") as first, scratch_space(tmp_path / "p") as second:
        assert first != second and first.exists() and second.exists()


@pytest.mark.parametrize("what", ["symlink", "file"])
def test_the_private_root_must_be_a_real_directory(tmp_path: Path, what: str) -> None:
    (tmp_path / "real").mkdir()
    bad = tmp_path / "bad"
    if what == "symlink":
        bad.symlink_to(tmp_path / "real")
    else:
        bad.write_bytes(b"")
    with pytest.raises(ScratchError):
        prepare_private_root(bad)


def test_the_private_root_never_overlaps_the_ingest_root(tmp_path: Path) -> None:
    ingest = tmp_path / "corpus"
    (ingest / "runs").mkdir(parents=True)
    for private in (ingest, ingest / ".neptune", ingest / "runs" / "scratch", tmp_path):
        with pytest.raises(ScratchError, match="overlaps"):
            prepare_private_root(private, ingest_root=ingest)
    assert not (ingest / ".neptune").exists()  # refused before anything is created
    assert prepare_private_root(tmp_path / "ok", ingest_root=ingest) == tmp_path / "ok"
    (tmp_path / "alias").symlink_to(ingest)
    with pytest.raises(ScratchError, match="overlaps"):
        prepare_private_root(tmp_path / "alias" / "s", ingest_root=ingest)


def test_loose_permissions_are_tightened(tmp_path: Path) -> None:
    root = tmp_path / "loose"
    root.mkdir(mode=0o755)
    prepare_private_root(root)
    assert mode(root) == 0o700


def test_clear_scratch_removes_stale_and_debris_and_keeps_live(tmp_path: Path) -> None:
    root = prepare_private_root(tmp_path / "private")
    stale = root / "scratch-stale"
    stale.mkdir()
    (stale / LOCK_NAME).touch()
    (stale / "junk").write_bytes(b"x")
    unlocked = root / "scratch-unlocked"
    unlocked.mkdir()
    (root / "debris.bin").write_bytes(b"x")
    (root / "planted").symlink_to(tmp_path)
    with scratch_space(root) as live:
        (live / "work").write_bytes(b"w")
        assert clear_scratch(root) == 4
        assert (live / "work").exists()
        assert sorted(p.name for p in root.iterdir()) == [live.name]
    assert tmp_path.exists() and list(root.iterdir()) == []
    assert clear_scratch(root) == 0
