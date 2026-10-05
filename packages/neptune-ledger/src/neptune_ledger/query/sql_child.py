"""The SQL passthrough's child process: one statement in a sealed DuckDB under an OS memory cap.

``sql.py`` starts this file as a script (``python -I sql_child.py <parent pid>``) with a minimal
environment and no file descriptors but its pipes, so it imports only the standard library,
PyArrow and DuckDB, never ``neptune_ledger``. First it arranges to die with its parent
(``PR_SET_PDEATHSIG``). The parent writes, on stdin:

- a 4-byte little-endian length and a JSON header (the statement, the memory cap, the deadline,
  the fetch size, the row and byte limits, the view names);
- one Arrow IPC stream per view, in the header's order.

The child arms its own deadline (``SIGALRM``, with ``RLIMIT_CPU`` behind it), loads the views,
seals a new in-memory DuckDB, lowers its private writable memory limit (``RLIMIT_DATA``) to what
it holds plus the cap, and only then runs the statement. It writes frames on stdout: a 1-byte
tag, an 8-byte little-endian length and the payload.

- ``S``: the statement's Arrow schema, once, before any rows;
- ``B``: one Arrow IPC stream holding the next rows, already cut to the row and byte limits;
- ``E``: a JSON end: ``{"cut": [...], "memory": bool, "error": str | null}``.

A process that ends without an ``E`` frame stopped at its memory cap, at its deadline, or was
killed: the parent keeps the rows it has and says so.
"""

import ctypes
import json
import math
import os
import signal
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO

LENGTH = struct.Struct("<Q")
OUT_OF_MEMORY = ("out of memory", "bad allocation", "bad_alloc", "failed to allocate")
PR_SET_PDEATHSIG = 1
# The child's own deadline is the parent's plus this, so the parent's watchdog normally acts first.
DEADLINE_MARGIN = 1.0


def config(memory: int) -> dict[str, str | bool | int | float | list[str]]:
    """The sealed engine: no file, network or extension access, one thread, a memory limit, no
    spilling, and the configuration locked before any user text runs (ADR 0016 §7)."""
    return {
        "enable_external_access": "false",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "allow_unsigned_extensions": "false",
        "allow_community_extensions": "false",
        "python_enable_replacements": "false",
        "threads": "1",
        "memory_limit": f"{memory}B",
        "max_temp_directory_size": "0B",
        "preserve_insertion_order": "true",
        "lock_configuration": "true",
    }


def _read_exact(source: BinaryIO, size: int) -> bytes:
    data = source.read(size)
    if len(data) != size:
        raise EOFError("the parent closed the input early")
    return data


def _frame(out: BinaryIO, tag: bytes, payload: bytes) -> None:
    out.write(tag + LENGTH.pack(len(payload)))
    out.write(payload)
    out.flush()


def _data_bytes() -> int:
    """This process's private writable memory, as ``RLIMIT_DATA`` counts it (``VmData``)."""
    with Path("/proc/self/status").open(encoding="ascii") as status:
        for line in status:
            if line.startswith("VmData:"):
                return int(line.split()[1]) * 1024
    raise OSError("no VmData in /proc/self/status")


def cap_memory(memory: int) -> None:
    """Allow ``memory`` more bytes of private writable memory than is held now, and no more, for
    good: the hard limit is lowered too, so nothing in this process can raise it again.

    ``RLIMIT_DATA`` (Linux 4.7+) counts every private writable mapping, so an allocator's
    ``mmap`` past it fails, but not ``PROT_NONE`` reservations. glibc reserves 64 MiB of address
    space per malloc arena for each thread's first allocation, so an address-space cap
    (``RLIMIT_AS``) was spent by DuckDB's idle threads while their memory stayed small."""
    import resource  # POSIX only: the parent starts this child only where it exists

    _, hard = resource.getrlimit(resource.RLIMIT_DATA)
    limit = _data_bytes() + memory
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))


def die_with_parent(parent: int) -> bool:
    """Ask the kernel to kill this process when the thread that started it ends; False when
    the parent ``parent`` has already gone, so nothing is left to answer."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        return False
    return os.getppid() == parent


def arm_deadline(seconds: float | None) -> None:
    """End this process ``seconds`` (plus a margin) from now whatever it is doing: ``SIGALRM``
    with its default action, and a CPU-time limit behind it in case the alarm is blocked."""
    if seconds is None:
        return
    import resource

    wall = max(0.0, float(seconds)) + DEADLINE_MARGIN
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, wall)
    used = resource.getrusage(resource.RUSAGE_SELF)
    cpu = math.ceil(used.ru_utime + used.ru_stime + wall) + 1
    _, hard = resource.getrlimit(resource.RLIMIT_CPU)
    if hard == resource.RLIM_INFINITY or hard > cpu:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))


def _out_of_memory(exc: BaseException) -> bool:
    if isinstance(exc, MemoryError):
        return True
    text = str(exc).lower()
    return type(exc).__name__ in {"OutOfMemoryException", "ArrowMemoryError"} or any(
        mark in text for mark in OUT_OF_MEMORY
    )


def _first_line(exc: BaseException) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0][:300]


def _wire(batch: Any) -> tuple[Any, int]:
    """``batch`` as one Arrow IPC stream, and its bytes as the parent will count them: on the
    batch it reads back, which has no validity bitmap where nothing is null."""
    import pyarrow as pa

    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, batch.schema) as writer:
        writer.write_batch(batch)
    payload = sink.getvalue()
    return payload, pa.ipc.open_stream(payload).read_next_batch().nbytes


def _fit(batch: Any, rows_left: int, bytes_left: int) -> tuple[bytes | None, int, int, list[str]]:
    """The longest prefix of ``batch`` within the rows and bytes left, as the parent counts its
    bytes: its IPC stream (None for no rows), rows, bytes, and the limits that cut it. A
    prefix's bytes grow with its length, so the byte cut is a bisection."""
    cut: list[str] = []
    if batch.num_rows > rows_left:
        batch = batch.slice(0, rows_left)
        cut.append("rows")
    payload, size = _wire(batch)
    rows = batch.num_rows
    if size > bytes_left:
        low, high = 0, rows - 1  # all ``rows`` do not fit
        while low < high:
            middle = (low + high + 1) // 2
            if _wire(batch.slice(0, middle))[1] <= bytes_left:
                low = middle
            else:
                high = middle - 1
        rows = low
        payload, size = _wire(batch.slice(0, low)) if low else (None, 0)
        cut.append("bytes")
    return (payload.to_pybytes() if rows else None), rows, size, sorted(cut)


def _run(header: dict[str, Any], source: BinaryIO, out: BinaryIO) -> dict[str, Any]:
    import duckdb
    import pyarrow as pa

    views = {name: pa.ipc.open_stream(source).read_all() for name in header["views"]}
    memory = int(header["memory"])
    con = duckdb.connect(":memory:", config=config(memory))
    try:
        for name, table in views.items():
            con.register(name, table)
        cap_memory(memory)
        reader = con.execute(header["statement"]).to_arrow_reader(int(header["fetch_rows"]))
        _frame(out, b"S", reader.schema.serialize().to_pybytes())
        rows_left, bytes_left = int(header["max_rows"]), int(header["max_bytes"])
        for batch in reader:
            payload, rows, size, cut = _fit(batch, rows_left, bytes_left)
            if payload is not None:
                _frame(out, b"B", payload)
                rows_left -= rows
                bytes_left -= size
            if cut:
                return {"cut": cut, "memory": False, "error": None}
        return {"cut": [], "memory": False, "error": None}
    finally:
        con.close()


def main(argv: list[str]) -> int:
    """``argv``: the parent's pid, and the seconds this process may run (``-`` for no limit)."""
    if len(argv) != 2 or not die_with_parent(int(argv[0])):
        return 4
    arm_deadline(None if argv[1] == "-" else float(argv[1]))
    source, out = sys.stdin.buffer, sys.stdout.buffer
    try:
        (size,) = struct.unpack("<I", _read_exact(source, 4))
        header = json.loads(_read_exact(source, size))
    except (EOFError, ValueError):
        return 2
    try:
        end = _run(header, source, out)
    except BrokenPipeError:
        return 3
    except BaseException as exc:  # every engine failure is reported, never raised
        if _out_of_memory(exc):
            end = {"cut": [], "memory": True, "error": _first_line(exc)}
        else:
            end = {"cut": [], "memory": False, "error": _first_line(exc)}
    try:
        _frame(out, b"E", json.dumps(end, sort_keys=True).encode("utf-8"))
    except BrokenPipeError:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
