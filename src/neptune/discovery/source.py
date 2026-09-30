"""The ``Source`` interface and its local-filesystem implementation.

Walking policy (ADR 0009 §5 as amended by ADR 0010, ``docs/security.md``):

- Regular files are yielded as ``SourceEntry``. Names that are not valid UTF-8 are yielded too,
  located by their exact bytes (``RawLocalPath``).
- Symlinks are never followed, whether they point inside or outside the root. Each is yielded as a
  ``SymlinkEntry`` with its target exactly as stored; resolving it is interpretation, not walking.
- Sockets, FIFOs and devices are never opened. They, and anything the walk could not examine, are
  reported as a ``SkippedEntry`` with a reason; nothing is dropped silently.
- Directories are opened component by component relative to the root with ``O_NOFOLLOW``, so a
  directory swapped for a symlink mid-walk cannot redirect the walk or ``open`` outside the root.
- Order is deterministic: depth-first, siblings in byte order of their names (which is code-point
  order for UTF-8 names).
"""

import errno
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Protocol, TypeAlias

from neptune.model.source import LocalPath, RawLocalPath, SourceLocation, local_location


class SkipReason(StrEnum):
    SYMLINK = "symlink"
    NOT_REGULAR_FILE = "not_regular_file"
    MISSING = "missing"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class SourceEntry:
    """A regular file found by a walk. ``size`` is as observed then; the digest is authoritative."""

    location: SourceLocation
    size: int


@dataclass(frozen=True)
class SymlinkEntry:
    """A symlink found by a walk. ``target`` is the link's contents, byte-exact and unresolved."""

    location: LocalPath | RawLocalPath
    target: bytes


@dataclass(frozen=True)
class SkippedEntry:
    """Something a walk saw and did not yield. ``raw_path`` is relative to the root, undecoded."""

    raw_path: bytes
    reason: SkipReason
    detail: str


WalkEntry: TypeAlias = SourceEntry | SymlinkEntry | SkippedEntry


class SourceAccessError(Exception):
    """``Source.open`` refused or failed; ``reason`` says why in the walk's vocabulary."""

    def __init__(self, location: SourceLocation, reason: SkipReason, detail: str) -> None:
        super().__init__(f"{location}: {reason}: {detail}")
        self.location = location
        self.reason = reason
        self.detail = detail


class Source(Protocol):
    """Where source bytes come from. Local filesystem now; object stores implement this later."""

    def walk(self) -> Iterator[WalkEntry]:
        """Enumerate candidate sources in a deterministic order."""
        ...

    def open(self, location: SourceLocation) -> BinaryIO:
        """Open one location for binary reading, re-applying the walk's safety policy."""
        ...


@dataclass(frozen=True)
class _Directory:
    parts: tuple[bytes, ...]


class LocalSource:
    """Regular files under one root directory.

    The root itself may be a symlink (the caller chose it); nothing below it is followed.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = os.fspath(root)
        if not Path(self._root).is_dir():
            raise NotADirectoryError(self._root)

    def walk(self) -> Iterator[WalkEntry]:
        stack: list[Iterator[WalkEntry | _Directory]] = [iter(self._list(()))]
        while stack:
            item = next(stack[-1], None)
            if item is None:
                stack.pop()
            elif isinstance(item, _Directory):
                stack.append(iter(self._list(item.parts)))
            else:
                yield item

    def open(self, location: SourceLocation) -> BinaryIO:
        if not isinstance(location, (LocalPath, RawLocalPath)):
            raise TypeError(f"LocalSource cannot open {type(location).__name__}")
        *directories, name = location.raw.split(b"/")
        try:
            dir_fd = self._open_directory(tuple(directories))
        except _WalkError as exc:
            raise SourceAccessError(location, exc.reason, exc.detail) from exc
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except OSError as exc:
            reason = _classify(exc, name, dir_fd)
            raise SourceAccessError(location, reason, exc.strerror or str(exc)) from exc
        finally:
            os.close(dir_fd)
        try:
            is_regular = stat.S_ISREG(os.fstat(fd).st_mode)
        except OSError:
            os.close(fd)
            raise
        if not is_regular:
            os.close(fd)
            raise SourceAccessError(location, SkipReason.NOT_REGULAR_FILE, "not a regular file")
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")

    def _list(self, parts: tuple[bytes, ...]) -> list[WalkEntry | _Directory]:
        try:
            dir_fd = self._open_directory(parts)
        except _WalkError as exc:
            return [SkippedEntry(_join(parts), exc.reason, exc.detail)]
        try:
            with os.scandir(dir_fd) as scan:
                names = sorted(os.fsencode(entry.name) for entry in scan)
            return [self._classify_entry(dir_fd, (*parts, name)) for name in names]
        except OSError as exc:
            return [SkippedEntry(_join(parts), SkipReason.UNREADABLE, exc.strerror or str(exc))]
        finally:
            os.close(dir_fd)

    def _classify_entry(self, dir_fd: int, child: tuple[bytes, ...]) -> WalkEntry | _Directory:
        raw = _join(child)
        try:
            info = os.stat(child[-1], dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return SkippedEntry(raw, SkipReason.MISSING, "vanished during walk")
        except OSError as exc:
            return SkippedEntry(raw, SkipReason.UNREADABLE, exc.strerror or str(exc))
        mode = info.st_mode
        if stat.S_ISLNK(mode):
            try:
                target = os.readlink(child[-1], dir_fd=dir_fd)
            except OSError as exc:
                return SkippedEntry(raw, SkipReason.UNREADABLE, exc.strerror or str(exc))
            return SymlinkEntry(local_location(raw), target)
        if stat.S_ISDIR(mode):
            return _Directory(child)
        if stat.S_ISREG(mode):
            return SourceEntry(local_location(raw), info.st_size)
        return SkippedEntry(raw, SkipReason.NOT_REGULAR_FILE, stat.filemode(mode))

    def _open_directory(self, parts: tuple[bytes, ...]) -> int:
        """Open root/parts as a directory fd, refusing any symlink along the way."""
        try:
            fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            raise _WalkError(SkipReason.UNREADABLE, exc.strerror or str(exc)) from exc
        for part in parts:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                reason = _classify(exc, part, fd)
                os.close(fd)
                raise _WalkError(reason, exc.strerror or str(exc)) from exc
            os.close(fd)
            fd = child
        return fd


class _WalkError(Exception):
    def __init__(self, reason: SkipReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _classify(exc: OSError, name: bytes, dir_fd: int) -> SkipReason:
    """Map a failed ``O_NOFOLLOW`` open to a reason.

    Linux reports ``O_DIRECTORY | O_NOFOLLOW`` on a symlink as ENOTDIR, so the entry is re-checked.
    """
    try:
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except OSError:
        mode = None
    if exc.errno == errno.ELOOP or (mode is not None and stat.S_ISLNK(mode)):
        return SkipReason.SYMLINK
    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
        return SkipReason.MISSING
    return SkipReason.UNREADABLE


def _join(parts: tuple[bytes, ...]) -> bytes:
    return b"/".join(parts) if parts else b"."
