"""A root that is one regular file (ADR 0043): one entry, named by the file's own name."""

import os
from pathlib import Path

import pytest

from neptune.discovery.source import (
    LocalSource,
    SkippedEntry,
    SkipReason,
    SourceAccessError,
    SourceEntry,
)
from neptune.model.source import LocalPath, RawLocalPath


def test_a_file_root_is_one_entry_named_by_its_name(tmp_path: Path) -> None:
    (tmp_path / "episode.mcap").write_bytes(b"payload")
    source = LocalSource(tmp_path / "episode.mcap")
    assert source.is_file
    assert list(source.walk()) == [SourceEntry(LocalPath("episode.mcap"), 7)]
    with source.open(LocalPath("episode.mcap")) as stream:
        assert stream.read() == b"payload"


def test_a_file_root_walks_as_a_folder_holding_only_it(tmp_path: Path) -> None:
    (tmp_path / "only").mkdir()
    (tmp_path / "only" / "log.txt").write_bytes(b"abc")
    folder = list(LocalSource(tmp_path / "only").walk())
    assert list(LocalSource(tmp_path / "only" / "log.txt").walk()) == folder


def test_a_symlinked_file_root_is_named_by_what_it_resolves_to(tmp_path: Path) -> None:
    """The caller chose the root, so it is followed, as a root directory is."""
    (tmp_path / "real.txt").write_bytes(b"abc")
    (tmp_path / "alias").symlink_to(tmp_path / "real.txt")
    source = LocalSource(tmp_path / "alias")
    assert list(source.walk()) == [SourceEntry(LocalPath("real.txt"), 3)]


def test_a_file_root_opens_nothing_but_itself(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"abc")
    (tmp_path / "b.txt").write_bytes(b"secret")
    source = LocalSource(tmp_path / "a.txt")
    for other in (LocalPath("b.txt"), LocalPath("a.txt/x"), RawLocalPath(b"a.tx\xff")):
        with pytest.raises(SourceAccessError) as caught:
            source.open(other)
        assert caught.value.reason is SkipReason.MISSING


def test_a_file_root_that_vanishes_or_changes_kind_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"abc")
    source = LocalSource(path)
    path.unlink()
    [entry] = list(source.walk())
    assert isinstance(entry, SkippedEntry) and entry.reason is SkipReason.MISSING
    with pytest.raises(SourceAccessError) as caught:
        source.open(LocalPath("a.txt"))
    assert caught.value.reason is SkipReason.MISSING
    os.mkfifo(path)
    [entry] = list(source.walk())
    assert isinstance(entry, SkippedEntry) and entry.reason is SkipReason.NOT_REGULAR_FILE


def test_an_unreadable_file_root_is_refused_when_opened(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"abc")
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running with privileges that read any file")
        with pytest.raises(SourceAccessError) as caught:
            LocalSource(path).open(LocalPath("a.txt"))
        assert caught.value.reason is SkipReason.UNREADABLE
    finally:
        path.chmod(0o600)
