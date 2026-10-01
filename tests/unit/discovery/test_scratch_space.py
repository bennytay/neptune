"""Scratch space: private, never in the source tree, gone on exit, cleared on resume."""

import fcntl
import os
import stat
import threading
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


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """The ingest root every private root here is checked against."""
    root = tmp_path / "corpus"
    root.mkdir()
    return root


def test_scratch_is_private_and_removed_on_exit(tmp_path: Path, corpus: Path) -> None:
    root = tmp_path / "private"
    with scratch_space(root, ingest_root=corpus) as scratch:
        assert scratch.parent == root
        assert mode(root) == 0o700 and mode(scratch) == 0o700
        assert (scratch / LOCK_NAME).exists()
        (scratch / "spool").write_bytes(b"x" * 10)
        (scratch / "deeper").mkdir()
        (scratch / "deeper" / "link").symlink_to(tmp_path)  # a planted link is not followed
    assert not scratch.exists()
    assert tmp_path.exists() and list(root.iterdir()) == []


def test_scratch_is_removed_when_the_block_raises(tmp_path: Path, corpus: Path) -> None:
    root = tmp_path / "private"
    with pytest.raises(RuntimeError), scratch_space(root, ingest_root=corpus) as scratch:
        (scratch / "partial").write_bytes(b"x")
        raise RuntimeError("the parser crashed")
    assert list(root.iterdir()) == []


def test_two_spaces_coexist_and_do_not_collide(tmp_path: Path, corpus: Path) -> None:
    with (
        scratch_space(tmp_path / "p", ingest_root=corpus) as first,
        scratch_space(tmp_path / "p", ingest_root=corpus) as second,
    ):
        assert first != second and first.exists() and second.exists()


@pytest.mark.parametrize("what", ["symlink", "file"])
def test_the_private_root_must_be_a_real_directory(tmp_path: Path, corpus: Path, what: str) -> None:
    (tmp_path / "real").mkdir()
    bad = tmp_path / "bad"
    if what == "symlink":
        bad.symlink_to(tmp_path / "real")
    else:
        bad.write_bytes(b"")
    with pytest.raises(ScratchError):
        prepare_private_root(bad, ingest_root=corpus)


def test_the_private_root_never_overlaps_the_ingest_root(tmp_path: Path, corpus: Path) -> None:
    (corpus / "runs").mkdir()
    for private in (corpus, corpus / ".neptune", corpus / "runs" / "scratch", tmp_path):
        with pytest.raises(ScratchError, match="overlaps"):
            prepare_private_root(private, ingest_root=corpus)
    assert not (corpus / ".neptune").exists()  # refused before anything is created
    assert prepare_private_root(tmp_path / "ok", ingest_root=corpus) == tmp_path / "ok"
    (tmp_path / "alias").symlink_to(corpus)
    with pytest.raises(ScratchError, match="overlaps"):
        prepare_private_root(tmp_path / "alias" / "s", ingest_root=corpus)


def test_the_ingest_root_is_required(tmp_path: Path) -> None:
    """Without it the overlap check could not run, so there is no default to fall back on."""
    private = tmp_path / "private"
    with pytest.raises(TypeError):
        prepare_private_root(private)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        scratch_space(private)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        clear_scratch(private)  # type: ignore[call-arg]
    assert not private.exists()


def test_a_sweep_never_reaches_the_source_tree(tmp_path: Path, corpus: Path) -> None:
    """``clear_scratch`` removes debris, so a private root inside the corpus would delete it."""
    (corpus / "evidence.bin").write_bytes(b"source bytes")
    (corpus / "runs").mkdir()
    for private in (corpus, corpus / "runs", tmp_path):
        with pytest.raises(ScratchError, match="overlaps"):
            clear_scratch(private, ingest_root=corpus)
    assert (corpus / "evidence.bin").read_bytes() == b"source bytes"
    assert (corpus / "runs").is_dir()


def test_loose_permissions_are_tightened(tmp_path: Path, corpus: Path) -> None:
    root = tmp_path / "loose"
    root.mkdir(mode=0o755)
    prepare_private_root(root, ingest_root=corpus)
    assert mode(root) == 0o700


def test_clear_scratch_removes_stale_and_debris_and_keeps_live(
    tmp_path: Path, corpus: Path
) -> None:
    root = prepare_private_root(tmp_path / "private", ingest_root=corpus)
    stale = root / "scratch-stale"
    stale.mkdir()
    (stale / LOCK_NAME).touch()
    (stale / "junk").write_bytes(b"x")
    unlocked = root / "scratch-unlocked"
    unlocked.mkdir()
    (root / "debris.bin").write_bytes(b"x")
    (root / "planted").symlink_to(tmp_path)
    with scratch_space(root, ingest_root=corpus) as live:
        (live / "work").write_bytes(b"w")
        assert clear_scratch(root, ingest_root=corpus) == 4
        assert (live / "work").exists()
        assert sorted(p.name for p in root.iterdir()) == [live.name]
    assert tmp_path.exists() and list(root.iterdir()) == []
    assert clear_scratch(root, ingest_root=corpus) == 0


def test_a_sweep_waits_while_a_scratch_directory_is_being_set_up(
    tmp_path: Path, corpus: Path
) -> None:
    """``scratch_space`` holds the root shared until its lock is held; a sweep needs it whole."""
    root = prepare_private_root(tmp_path / "private", ingest_root=corpus)
    half_made = root / "scratch-half-made"
    half_made.mkdir()  # created, its lock not yet taken: another process is mid-way through
    setting_up = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(setting_up, fcntl.LOCK_SH)
    removed: list[int] = []
    sweep = threading.Thread(target=lambda: removed.append(clear_scratch(root, ingest_root=corpus)))
    try:
        sweep.start()
        sweep.join(timeout=0.2)
        assert sweep.is_alive() and half_made.exists()  # the sweep waits for the set-up
    finally:
        os.close(setting_up)
    sweep.join(timeout=10)
    assert removed == [1] and not half_made.exists()
