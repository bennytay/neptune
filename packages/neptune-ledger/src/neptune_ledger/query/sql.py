"""SQL passthrough: one SELECT over views of one scoped answer, in a sealed DuckDB (ADR 0016 §7).

The statement is hostile input. Before it runs:

- DuckDB's own parser must find exactly one statement, of type ``SELECT``; anything else
  (DDL, DML, ``SET``, ``PRAGMA`` writes, ``ATTACH``, ``COPY``, ``INSTALL``, ``LOAD``, ``CALL``,
  ``EXPORT``, ``EXPLAIN``, a second statement) is refused;
- the statement runs in a child process (``sql_child.py``) whose address space is capped by the
  operating system (``RLIMIT_AS``): whatever the statement allocates, it cannot grow this
  process, and at the cap the child fails or dies while this process keeps the rows it has;
- in the child, a new in-memory database is opened with external access off (no file, glob,
  attach, copy or HTTP), extension install and autoload off, unsigned and community extensions
  refused, Python replacement scans off, one thread, a memory limit, no spilling to disk, and its
  configuration locked;
- the only data in it are the views, sent as Arrow IPC: the scope's own answer.

The child fetches small batches and cuts them to the row and byte limits itself, so an honest
statement stops early and this process never holds more than the byte limit of rows. A
watchdog kills the child at the budget's deadline. Engine errors are findings. Where the
operating system cannot cap a process's memory, the statement is refused.
"""

import contextlib
import json
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO, Any, Final

import pyarrow as pa

from neptune_ledger.api.types import CatalogFinding
from neptune_ledger.query.budget import Budget
from neptune_ledger.query.sql_child import LENGTH, config

MAX_STATEMENT: Final = 64 * 1024
FETCH_ROWS: Final = 1024
CHILD: Final = Path(__file__).with_name("sql_child.py")
# A frame larger than the byte limit by more than this is not one the child writes.
FRAME_SLACK: Final = 1 << 20
EXIT_WAIT: Final = 5.0


def can_cap_memory() -> bool:
    """Whether the operating system enforces the child's address-space cap: Linux only."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import resource
    except ImportError:  # pragma: no cover - every Linux Python has it
        return False
    return hasattr(resource, "RLIMIT_AS") and Path("/proc/self/status").is_file()


def _refused(detail: str) -> tuple[None, list[CatalogFinding]]:
    return None, [CatalogFinding("invalid_request", "statement", detail)]


def run_sql(
    statement: object, views: dict[str, Any], budget: Budget, memory: int
) -> tuple[Any | None, list[CatalogFinding]]:
    """The statement's rows (a prefix when a limit cut them) and findings; ``None`` and an
    ``invalid_request`` finding when the statement is refused or fails. ``memory`` is the
    child's cap in bytes; a statement it stops is a ``memory`` cut, recorded in ``budget``."""
    if not isinstance(statement, str) or not statement.strip():
        return _refused("the statement is a non-empty string")
    if len(statement.encode("utf-8", "surrogatepass")) > MAX_STATEMENT or "\x00" in statement:
        return _refused(f"the statement is at most {MAX_STATEMENT} bytes, without NUL")
    problem = _classify(statement)
    if problem is not None:
        return _refused(problem)
    if not can_cap_memory():
        detail = (
            "SQL passthrough runs only where the operating system caps the statement's memory"
            f" (RLIMIT_AS on Linux); this platform is {sys.platform}"
        )
        return None, [CatalogFinding("invalid_request", "platform", detail)]
    left = budget.remaining()
    if left is not None and left <= 0:
        budget.time_ran_out()
        return _empty(), []
    return _Child(statement, views, budget, memory).run(left)


def _classify(statement: str) -> str | None:
    """Why DuckDB's own parser refuses ``statement``, or None for exactly one SELECT. Parsing
    runs nothing, so it happens here, before a child starts."""
    import duckdb

    con = duckdb.connect(":memory:", config=config(64 * 2**20))
    try:
        parsed = con.extract_statements(statement)
    except duckdb.Error as exc:
        return _first_line(exc)
    finally:
        con.close()
    if len(parsed) != 1 or parsed[0].type != duckdb.StatementType.SELECT:
        kinds = ", ".join(p.type.name for p in parsed) or "none"
        return f"exactly one SELECT statement is accepted, not: {kinds}"
    return None


class _Child:
    """One child process running one statement, and what it sent back."""

    def __init__(self, statement: str, views: dict[str, Any], budget: Budget, memory: int) -> None:
        self.statement = statement
        self.views = views
        self.budget = budget
        self.memory = memory
        self.timed_out = threading.Event()
        self.schema: Any = None
        self.batches: list[Any] = []

    def run(self, left: float | None) -> tuple[Any | None, list[CatalogFinding]]:
        proc = subprocess.Popen(
            [sys.executable, "-I", str(CHILD)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        watchdog = None if left is None else threading.Timer(left, self._expire, (proc,))
        try:
            if watchdog is not None:
                watchdog.start()
            assert proc.stdin is not None and proc.stdout is not None
            end = self._exchange(proc, proc.stdin, proc.stdout)
            if end is None:  # the child broke its protocol: stop it, keep nothing
                proc.kill()
                return _refused("the statement's process wrote an unreadable answer")
        finally:
            if watchdog is not None:
                watchdog.cancel()
            _reap(proc)
        if isinstance(end, dict):
            if end.get("memory"):
                self.budget.memory_ran_out(self.memory, str(end.get("error") or ""))
            elif end.get("error"):
                return _refused(str(end["error"]))
            cut = end.get("cut", [])
            if "rows" in cut:
                self.budget.exceeded.add("rows")
            if "bytes" in cut:
                self.budget.exceeded.add("bytes")
        elif self.timed_out.is_set():
            self.budget.time_ran_out()
        else:
            # It ended without saying how: it died at its memory cap, in an allocation its
            # runtime could not report, or was killed. Either way the rows so far are a prefix.
            self.budget.memory_ran_out(self.memory, f"the process ended, status {proc.returncode}")
        if self.schema is None:
            return _empty(), []
        return pa.Table.from_batches(self.batches, schema=self.schema), []

    def _expire(self, proc: subprocess.Popen[bytes]) -> None:
        self.timed_out.set()
        proc.kill()

    def _exchange(
        self, proc: subprocess.Popen[bytes], stdin: IO[bytes], stdout: IO[bytes]
    ) -> dict[str, Any] | str | None:
        """Send the header and views, then read frames until the end frame (its JSON), the end
        of the output (``"eof"``), or a frame the child cannot have written (None)."""
        header = {
            "statement": self.statement,
            "memory": self.memory,
            "fetch_rows": FETCH_ROWS,
            "max_rows": self.budget.max_rows,
            "max_bytes": self.budget.max_bytes,
            "views": list(self.views),
        }
        encoded = json.dumps(header, sort_keys=True).encode("utf-8")
        try:
            stdin.write(struct.pack("<I", len(encoded)) + encoded)
            for table in self.views.values():
                with pa.ipc.new_stream(stdin, table.schema) as writer:
                    writer.write_table(table)
            stdin.close()
        except (BrokenPipeError, OSError, pa.ArrowException):
            return "eof"  # the child is gone: what it wrote, if anything, says why
        received = 0
        while True:
            head = stdout.read(1 + LENGTH.size)
            if len(head) < 1 + LENGTH.size:
                return "eof"
            tag, (size,) = head[:1], LENGTH.unpack(head[1:])
            if size > self.budget.max_bytes + FRAME_SLACK - received:
                return None
            payload = stdout.read(size)
            if len(payload) < size:
                return "eof"
            received += size
            try:
                if tag == b"E":
                    end: dict[str, Any] = json.loads(payload)
                    return end
                if tag == b"S" and self.schema is None:
                    self.schema = pa.ipc.read_schema(pa.py_buffer(payload))
                elif tag == b"B" and self.schema is not None:
                    self.batches.extend(pa.ipc.open_stream(payload).read_all().to_batches())
                else:
                    return None
            except (ValueError, pa.ArrowException):
                return None
            if self.budget.out_of_time():
                # The deadline passed between frames: an interrupt the watchdog has not yet
                # delivered. Stop here; the rows so far are a prefix.
                self.timed_out.set()
                proc.kill()
                return "eof"


def _reap(proc: subprocess.Popen[bytes]) -> None:
    """Wait for the child, killing it if it outlives its answer; close its pipes."""
    try:
        proc.wait(timeout=EXIT_WAIT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    for pipe in (proc.stdin, proc.stdout):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()


def _empty() -> Any:
    return pa.table({})


def _first_line(exc: BaseException) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0][:300]
