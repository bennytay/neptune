"""Putting written files on disk before a rename publishes them (ADR 0026 §2, §4).

A rename is atomic, not durable: after a power loss, a renamed directory can hold files whose
bytes never reached the disk. So everything under a finished directory is flushed with ``fsync``
before it is renamed into place, and the directory it lands in is flushed after.
"""

import os
from pathlib import Path


def fsync_directory(path: Path) -> None:
    """Flush a directory's entries: the names created, renamed or removed in it."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_tree(root: Path) -> None:
    """Flush every file and directory under ``root``, and ``root`` itself."""
    for path in root.rglob("*"):
        if path.is_symlink():
            continue  # nothing Neptune writes is a link; its target is not ours to flush
        if path.is_dir():
            fsync_directory(path)
        else:
            with path.open("rb") as written:
                os.fsync(written.fileno())
    fsync_directory(root)
