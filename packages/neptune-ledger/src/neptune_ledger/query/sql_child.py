"""The SQL passthrough's child process: one statement in a sealed DuckDB under an OS memory cap.

``sql.py`` starts this file as a script (``python -I sql_child.py``), so it imports only the
standard library, PyArrow and DuckDB, never ``neptune_ledger``. The parent writes, on stdin:

- a 4-byte little-endian length and a JSON header (the statement, the memory cap, the fetch
  size, the row and byte limits, the view names);
- one Arrow IPC stream per view, in the header's order.

The child loads the views, seals a new in-memory DuckDB, lowers its own address-space limit
(``RLIMIT_AS``) to what it has mapped plus the cap, and only then runs the statement. It writes
frames on stdout: a 1-byte tag, an 8-byte little-endian length and the payload.

- ``S``: the statement's Arrow schema, once, before any rows;
- ``B``: one Arrow IPC stream holding the next rows, already cut to the row and byte limits;
- ``E``: a JSON end: ``{"cut": [...], "memory": bool, "error": str | null}``.

A process that ends without an ``E`` frame stopped at its memory cap or was killed: the parent
keeps the rows it has and says so.
"""

import json
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO

LENGTH = struct.Struct("<Q")
OUT_OF_MEMORY = ("out of memory", "bad allocation", "bad_alloc", "failed to allocate")


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


def _mapped() -> int:
    """This process's mapped address space in bytes (``VmSize``)."""
    with Path("/proc/self/status").open(encoding="ascii") as status:
        for line in status:
            if line.startswith("VmSize:"):
                return int(line.split()[1]) * 1024
    raise OSError("no VmSize in /proc/self/status")


def cap_memory(memory: int) -> None:
    """Allow ``memory`` more bytes of address space than is mapped now, and no more, for good:
    the hard limit is lowered too, so nothing in this process can raise it again."""
    import resource  # POSIX only: the parent starts this child only where it exists

    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    limit = _mapped() + memory
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _out_of_memory(exc: BaseException) -> bool:
    if isinstance(exc, MemoryError):
        return True
    text = str(exc).lower()
    return type(exc).__name__ in {"OutOfMemoryException", "ArrowMemoryError"} or any(
        mark in text for mark in OUT_OF_MEMORY
    )


def _first_line(exc: BaseException) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0][:300]


def _fit(batch: Any, rows_left: int, bytes_left: int) -> tuple[Any, list[str]]:
    """The longest prefix of ``batch`` within the rows and bytes left, and the limits that cut
    it. A prefix's bytes grow with its length, so the byte cut is a bisection."""
    cut: list[str] = []
    if batch.num_rows > rows_left:
        batch = batch.slice(0, rows_left)
        cut.append("rows")
    if batch.nbytes > bytes_left:
        low, high = 0, batch.num_rows
        while low < high:
            middle = (low + high + 1) // 2
            if batch.slice(0, middle).nbytes <= bytes_left:
                low = middle
            else:
                high = middle - 1
        batch = batch.slice(0, low)
        cut.append("bytes")
    return batch, sorted(cut)


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
            part, cut = _fit(batch, rows_left, bytes_left)
            if part.num_rows:
                sink = pa.BufferOutputStream()
                with pa.ipc.new_stream(sink, part.schema) as writer:
                    writer.write_batch(part)
                _frame(out, b"B", sink.getvalue().to_pybytes())
                rows_left -= part.num_rows
                bytes_left -= part.nbytes
            if cut:
                return {"cut": cut, "memory": False, "error": None}
        return {"cut": [], "memory": False, "error": None}
    finally:
        con.close()


def main() -> int:
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
    sys.exit(main())
