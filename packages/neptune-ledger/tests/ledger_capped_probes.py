"""What a capped child runs in the tests of ``lake/capped.py`` (MVL-96, ADR 0014 §5).

A capped call names its function, so these live in a module the worker can import.
"""

import contextlib
import os
import threading
import time
from pathlib import Path
from typing import Any

from neptune_ledger.lake.capped import Remote


def worker_pid(remote: Remote, pause: float) -> int:
    """The worker that forked this child, after ``pause`` seconds."""
    time.sleep(pause)
    return os.getppid()


def surroundings(remote: Remote) -> dict[str, Any]:
    """The child's environment variable names, open descriptors, Arrow pool and threads."""
    import pyarrow as pa

    targets = {}
    for entry in Path("/proc/self/fd").iterdir():
        # The listing's own descriptor is closed by now.
        with contextlib.suppress(FileNotFoundError):
            targets[int(entry.name)] = str(entry.readlink())
    return {
        "environment": sorted(os.environ),
        "descriptors": targets,
        "pool": pa.default_memory_pool().backend_name,
        "threads": threading.active_count(),
    }


def abandon_read(remote: Remote, n: int) -> None:
    """Ask for ``n`` bytes and die before reading the reply."""
    remote._conn.send(("read", 0, n))
    os._exit(1)


def ignore_read(remote: Remote, n: int) -> int:
    """Ask for ``n`` bytes, never read the reply, and return as if all were well."""
    remote._conn.send(("read", 0, n))
    time.sleep(0.2)  # the reply reaches the worker's socket first
    return os.getppid()


def announce_and_hang(remote: Remote, path: str) -> None:
    """Write the worker's PID and this child's to ``path``, then hang."""
    Path(path).write_text(f"{os.getppid()} {os.getpid()}", encoding="ascii")
    time.sleep(600)
