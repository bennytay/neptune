import os
import socket
from pathlib import Path

import pytest

from neptune.discovery.source import (
    LocalSource,
    SkippedEntry,
    SkipReason,
    SourceAccessError,
    SourceEntry,
)
from neptune.model.ids import ExternalObjectRef
from neptune.model.source import LocalPath


def write(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def files(source: LocalSource) -> dict[str, int]:
    """Path -> size of every yielded file, in walk order."""
    found = {}
    for entry in source.walk():
        if isinstance(entry, SourceEntry):
            assert isinstance(entry.location, LocalPath)
            found[entry.location.path] = entry.size
    return found


def entries(source: LocalSource) -> list[str]:
    return list(files(source))


def skips(source: LocalSource) -> dict[bytes, SkipReason]:
    return {e.raw_path: e.reason for e in source.walk() if isinstance(e, SkippedEntry)}


def test_walk_yields_regular_files_depth_first_in_code_point_order(tmp_path: Path) -> None:
    for name in ["b.txt", "a/z", "a/b/c", "a.txt", "B", "é"]:
        write(tmp_path / name, name.encode())
    assert entries(LocalSource(tmp_path)) == ["B", "a/b/c", "a/z", "a.txt", "b.txt", "é"]


def test_walk_reports_sizes(tmp_path: Path) -> None:
    write(tmp_path / "f", b"12345")
    write(tmp_path / "empty", b"")
    assert files(LocalSource(tmp_path)) == {"empty": 0, "f": 5}


def test_empty_root(tmp_path: Path) -> None:
    assert list(LocalSource(tmp_path).walk()) == []


def test_walk_is_deterministic(tmp_path: Path) -> None:
    for i in range(50):
        write(tmp_path / f"d{i % 7}" / f"f{i}")
    assert list(LocalSource(tmp_path).walk()) == list(LocalSource(tmp_path).walk())


def test_symlinks_are_reported_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    write(outside / "secret", b"do not read")
    write(root / "real/file")
    (root / "file_link").symlink_to(root / "real/file")
    (root / "dir_link").symlink_to(root / "real")
    (root / "escape").symlink_to(outside)
    (root / "dangling").symlink_to(tmp_path / "nowhere")
    (root / "loop").symlink_to(root / "loop")
    source = LocalSource(root)
    assert entries(source) == ["real/file"]
    assert skips(source) == {
        name: SkipReason.SYMLINK
        for name in [b"dangling", b"dir_link", b"escape", b"file_link", b"loop"]
    }


def test_root_may_be_a_symlink(tmp_path: Path) -> None:
    write(tmp_path / "real/f")
    (tmp_path / "root").symlink_to(tmp_path / "real")
    assert entries(LocalSource(tmp_path / "root")) == ["f"]


def test_special_files_are_skipped(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "fifo")
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(tmp_path / "sock"))
        assert skips(LocalSource(tmp_path)) == {
            b"fifo": SkipReason.NOT_REGULAR_FILE,
            b"sock": SkipReason.NOT_REGULAR_FILE,
        }


def test_undecodable_names_are_skipped_with_raw_bytes(tmp_path: Path) -> None:
    raw_file = os.fsencode(tmp_path) + b"/bad\xff"
    raw_dir = os.fsencode(tmp_path) + b"/dir\xfe"
    try:
        Path(os.fsdecode(raw_file)).write_bytes(b"x")
        Path(os.fsdecode(raw_dir)).mkdir()
    except OSError:
        pytest.skip("filesystem rejects non-UTF-8 names")
    Path(os.fsdecode(raw_dir + b"/inner")).write_bytes(b"x")
    write(tmp_path / "ok")
    source = LocalSource(tmp_path)
    assert entries(source) == ["ok"]
    assert skips(source) == {
        b"bad\xff": SkipReason.UNDECODABLE_NAME,
        b"dir\xfe": SkipReason.UNDECODABLE_NAME,
    }


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_unreadable_directory_is_reported_and_walk_continues(tmp_path: Path) -> None:
    write(tmp_path / "locked/f")
    write(tmp_path / "open/f")
    (tmp_path / "locked").chmod(0)
    try:
        source = LocalSource(tmp_path)
        assert entries(source) == ["open/f"]
        assert skips(source) == {b"locked": SkipReason.UNREADABLE}
    finally:
        (tmp_path / "locked").chmod(0o755)


def test_root_must_be_a_directory(tmp_path: Path) -> None:
    write(tmp_path / "f")
    with pytest.raises(NotADirectoryError):
        LocalSource(tmp_path / "f")
    with pytest.raises(NotADirectoryError):
        LocalSource(tmp_path / "missing")


def test_open_reads_bytes(tmp_path: Path) -> None:
    write(tmp_path / "a/b", b"payload")
    with LocalSource(tmp_path).open(LocalPath("a/b")) as stream:
        assert stream.read() == b"payload"


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("file_link", SkipReason.SYMLINK),
        ("dir_link/file", SkipReason.SYMLINK),
        ("escape/secret", SkipReason.SYMLINK),
        ("fifo", SkipReason.NOT_REGULAR_FILE),
        ("real", SkipReason.NOT_REGULAR_FILE),
        ("missing", SkipReason.MISSING),
        ("real/file/below", SkipReason.MISSING),
    ],
)
def test_open_refuses_what_walk_skips(tmp_path: Path, location: str, reason: SkipReason) -> None:
    root = tmp_path / "root"
    write(tmp_path / "outside/secret")
    write(root / "real/file")
    (root / "file_link").symlink_to(root / "real/file")
    (root / "dir_link").symlink_to(root / "real")
    (root / "escape").symlink_to(tmp_path / "outside")
    os.mkfifo(root / "fifo")
    with pytest.raises(SourceAccessError) as info:
        LocalSource(root).open(LocalPath(location))
    assert info.value.reason == reason


def test_open_rejects_foreign_locations(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        LocalSource(tmp_path).open(ExternalObjectRef("s3", "o", "t"))


def test_traversal_is_unrepresentable() -> None:
    with pytest.raises(ValueError):
        LocalPath("../etc/passwd")
