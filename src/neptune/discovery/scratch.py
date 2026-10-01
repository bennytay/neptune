"""Scratch space: the only place Neptune writes while handling untrusted input (ADR 0029 §4).

- The caller names a **private root**. It is created ``0700`` if missing, must be a real directory
  owned by this user, is tightened to ``0700`` if looser, and must not overlap the ingest root in
  either direction. Nothing is ever written into the source tree.
- ``scratch_space`` yields a fresh ``0700`` directory under the root for one unit of work and
  removes it on exit, success or not. While it lives, a lock file inside it is held with
  ``flock``.
- ``clear_scratch`` runs on resume. It removes every directory whose lock nobody holds (its owner
  died) and skips the ones another live process holds, so processes sharing a root cannot delete
  each other's work. The private root itself is ``flock``ed around both: shared while a scratch
  directory is created and locked, exclusive while ``clear_scratch`` sweeps, so a sweep never sees
  a directory whose lock is not yet held.

Scratch names are random; nothing in them ever reaches a record, so determinism is unaffected.
"""

import fcntl
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

LOCK_NAME: Final = ".lock"
_PREFIX: Final = "scratch-"


class ScratchError(Exception):
    """The private root cannot be used as scratch space."""


def prepare_private_root(private_root: Path, *, ingest_root: Path | None = None) -> Path:
    """Create or check the private root; raise ``ScratchError`` if it is unsafe."""
    root = Path(private_root)
    if ingest_root is not None:
        # Checked first, on resolved paths, so nothing is ever created inside the source tree.
        private = root.resolve()
        ingest = Path(ingest_root).resolve()
        if private == ingest or private in ingest.parents or ingest in private.parents:
            raise ScratchError(f"{root}: the private root overlaps the ingest root {ingest_root}")
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        root.mkdir(parents=True, mode=0o700)
        info = os.lstat(root)
    if stat.S_ISLNK(info.st_mode):
        raise ScratchError(f"{root}: the private root must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise ScratchError(f"{root}: the private root must be a directory")
    if info.st_uid != os.geteuid():
        raise ScratchError(f"{root}: the private root is owned by another user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        root.chmod(0o700)
    return root


@contextmanager
def scratch_space(private_root: Path, *, ingest_root: Path | None = None) -> Iterator[Path]:
    """A fresh private directory for one unit of work, removed when the block ends."""
    root = prepare_private_root(private_root, ingest_root=ingest_root)
    with _root_lock(root, fcntl.LOCK_SH):  # no sweep runs until the new lock is held
        directory = Path(tempfile.mkdtemp(prefix=_PREFIX, dir=root))
        lock = -1
        try:
            lock = os.open(
                directory / LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            _remove(directory)
            if lock >= 0:
                os.close(lock)
            raise
    try:
        yield directory
    finally:
        # Remove before releasing the lock, so a concurrent clear_scratch cannot race the removal.
        _remove(directory)
        os.close(lock)


def clear_scratch(private_root: Path) -> int:
    """Remove leftover scratch nobody holds a lock on; return how many entries were removed.

    Anything in the private root that is not a scratch directory is debris and is removed too.
    Symlinks are unlinked, never followed.
    """
    root = prepare_private_root(private_root)
    with _root_lock(root, fcntl.LOCK_EX):
        return _sweep(root)


def _sweep(root: Path) -> int:
    removed = 0
    for entry in sorted(root.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            entry.unlink(missing_ok=True)
            removed += 1
            continue
        lock = _try_lock(entry / LOCK_NAME)
        if lock is _LIVE:
            continue
        _remove(entry)
        removed += 1
        if isinstance(lock, int):
            os.close(lock)
    return removed


_LIVE: Final = object()


@contextmanager
def _root_lock(root: Path, operation: int) -> Iterator[None]:
    """Hold ``flock(operation)`` on the private root directory itself for the block."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, operation)
        yield
    finally:
        os.close(fd)


def _try_lock(path: Path) -> int | object | None:
    """An fd holding the lock, ``_LIVE`` if another process holds it, ``None`` if there is none."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        return None  # a symlink or a non-file in its place: stale by definition
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return _LIVE
    return fd


def _remove(directory: Path) -> None:
    if directory.is_symlink():
        directory.unlink(missing_ok=True)
        return
    shutil.rmtree(directory, ignore_errors=True)
