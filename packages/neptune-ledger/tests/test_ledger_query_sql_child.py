"""The SQL passthrough's child process: its memory cap, environment, deadline and byte cut
(Ledger ADR 0016 §7). These call ``run_sql`` directly over a small view, so need no catalog."""

import json
import os
import signal
import struct
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from neptune_ledger.api.types import QueryBudget
from neptune_ledger.query.budget import GIB, MIB, Budget
from neptune_ledger.query.sql import CHILD, can_cap_memory, child_environment, run_sql

pytestmark = pytest.mark.skipif(
    not can_cap_memory(), reason="needs an OS memory cap (Linux 4.7+ RLIMIT_DATA)"
)

VIEWS = {"records": pa.table({"n": [1, 2, 3]})}


def run(statement: str, *, max_bytes: int = 512 * MIB, millis: int = 30_000) -> tuple[Any, Budget]:
    budget = Budget(QueryBudget(max_rows=10**8, max_bytes=max_bytes, max_millis=millis))
    table, findings = run_sql(statement, VIEWS, budget, GIB)
    assert findings == [], findings
    return table, budget


@pytest.mark.slow
def test_an_honest_statement_is_never_cut_for_memory() -> None:
    # The reviewer's repro, about 1.3 s a run: under an address-space cap (RLIMIT_AS) the malloc
    # arenas DuckDB's idle threads reserve spent the headroom, and it was cut for `memory` in 7
    # runs of 12 here. A shorter statement (range(500000000)) never failed, so the shape stays.
    statement = "SELECT sum(range) AS s FROM range(1500000000)"
    for attempt in range(6):
        table, budget = run(statement)
        assert budget.exceeded == set(), (attempt, budget.exceeded)
        assert table.to_pylist() == [{"s": 1500000000 * 1499999999 // 2}], attempt


def test_a_slow_statement_is_cut_for_time_not_memory() -> None:
    for attempt in range(3):
        table, budget = run("SELECT sleep_ms(100000) AS z", millis=500)
        assert budget.exceeded == {"time"}, (attempt, budget.exceeded)
        assert table.num_rows == 0


@pytest.mark.parametrize(
    ("statement", "max_bytes", "rows", "cut"),
    [
        # The whole answer is exactly the limit: every row, and no cut.
        ("SELECT range AS n FROM range(1000)", 8000, 1000, False),
        ("SELECT range AS n FROM range(1000)", 7999, 999, True),
        ("SELECT (range % 2 = 0) AS b FROM range(100000)", 12500, 100000, False),
        ("SELECT (range % 2 = 0) AS b FROM range(100000)", 12499, 99992, True),
        # A cut inside a later batch: 8 bytes per row, 8 MiB.
        ("SELECT range AS n FROM range(10000000)", 8 * MIB, MIB, True),
    ],
)
def test_a_byte_cut_returns_the_longest_prefix_that_fits(
    statement: str, max_bytes: int, rows: int, cut: bool
) -> None:
    table, budget = run(statement, max_bytes=max_bytes)
    assert table.num_rows == rows
    assert table.nbytes <= max_bytes
    assert budget.exceeded == ({"bytes"} if cut else set())
    first = table.column(0).to_pylist()
    if table.column_names == ["n"]:
        assert first == list(range(rows)), "a prefix"


def test_a_byte_cut_over_strings_is_the_longest_prefix() -> None:
    statement = "SELECT range AS n, repeat('ab', (range % 5)::INT) AS s FROM range(100000)"
    table, budget = run(statement, max_bytes=300_000)
    assert budget.exceeded == {"bytes"}
    assert table.nbytes <= 300_000
    # One row more, counted the same way (the same batches), no longer fits.
    longer, _ = run(statement.replace("100000", str(table.num_rows + 1)), max_bytes=10**9)
    assert longer.num_rows == table.num_rows + 1
    assert longer.nbytes > 300_000
    assert table.column("n").to_pylist() == list(range(table.num_rows))


def test_the_child_sees_none_of_this_process_environment_or_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEPTUNE_LEDGER_SENTINEL_SECRET", "do-not-pass")
    seen: dict[str, Any] = {}
    real = subprocess.Popen

    class Spy(real):  # type: ignore[valid-type,misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            seen["environ"], seen["fds"] = _started(self.pid)

    with (tmp_path / "held-open.txt").open("w") as held:
        os.set_inheritable(held.fileno(), True)
        monkeypatch.setattr(subprocess, "Popen", Spy)
        table, _ = run("SELECT 42 AS answer")
    environ = seen["environ"]
    assert "NEPTUNE_LEDGER_SENTINEL_SECRET" not in environ
    assert set(environ) == {"PATH", "LANG", "MALLOC_ARENA_MAX"}
    assert environ == child_environment()
    assert seen["fds"] == [0, 1, 2]
    assert table.to_pylist() == [{"answer": 42}]


def test_the_child_dies_with_its_parent(tmp_path: Path) -> None:
    # A parent that dies mid-statement: its child must not run on, reparented, with no deadline.
    script = textwrap.dedent(
        """
        import os, subprocess, sys, threading
        import pyarrow as pa
        from neptune_ledger.api.types import QueryBudget
        from neptune_ledger.query import sql
        from neptune_ledger.query.budget import Budget

        real = subprocess.Popen

        class Spy(real):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                print(self.pid, flush=True)
                threading.Timer(1.0, os._exit, (0,)).start()

        sql.subprocess.Popen = Spy
        budget = Budget(QueryBudget(max_rows=10, max_bytes=10**6, max_millis=600_000))
        views = {"records": pa.table({"n": [1]})}
        sql.run_sql("SELECT count(*) FROM range(1000000000000)", views, budget, 2**30)
        """
    )
    parent = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    child = int(parent.stdout.split()[0])
    try:
        assert _gone(child, within=10.0), f"child {child} outlived its parent"
    finally:
        if not _gone(child, within=0.0):
            os.kill(child, signal.SIGKILL)  # the leaf this test started, by its own pid


def test_the_child_ends_itself_at_its_own_deadline() -> None:
    # No watchdog here: the child's own alarm ends it, the budget's 0.5 s plus its margin.
    header = {
        "statement": "SELECT count(*) FROM range(1000000000000)",
        "memory": GIB,
        "fetch_rows": 1024,
        "max_rows": 10,
        "max_bytes": 10**6,
        "views": ["records"],
    }
    encoded = json.dumps(header).encode()
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, VIEWS["records"].schema) as writer:
        writer.write_table(VIEWS["records"])
    started = time.monotonic()
    child = subprocess.run(
        [sys.executable, "-I", str(CHILD), str(os.getpid()), "0.5"],
        input=struct.pack("<I", len(encoded)) + encoded + sink.getvalue().to_pybytes(),
        capture_output=True,
        env=child_environment(),
        timeout=60,
        check=False,
    )
    took = time.monotonic() - started
    assert child.returncode in (-signal.SIGALRM, -signal.SIGXCPU), child.returncode
    assert took < 10.0, took


def test_a_child_whose_parent_is_not_the_one_named_does_nothing() -> None:
    child = subprocess.run(
        [sys.executable, "-I", str(CHILD), "1", "-"],
        input=b"",
        capture_output=True,
        env=child_environment(),
        timeout=60,
        check=False,
    )
    assert child.returncode == 4
    assert child.stdout == b""


def _started(pid: int) -> tuple[dict[str, str], list[int]]:
    """The environment ``pid`` was started with, and its open descriptors once it waits for its
    header. Popen can return before the exec is visible in ``/proc``, so this polls."""
    proc = Path(f"/proc/{pid}")
    deadline = time.monotonic() + 30.0
    while not (proc / "cmdline").read_bytes() and time.monotonic() < deadline:
        time.sleep(0.01)
    raw = (proc / "environ").read_bytes()
    environ = dict(item.decode().split("=", 1) for item in raw.split(b"\0") if item)
    fds: list[int] = []
    while time.monotonic() < deadline:
        # While it starts it opens and closes modules; waiting for its header, it holds only
        # what it inherited. An inherited descriptor would never leave this list.
        fds = sorted(int(fd.name) for fd in (proc / "fd").iterdir())
        if fds == [0, 1, 2]:
            break
        time.sleep(0.01)
    return environ, fds


def _gone(pid: int, within: float) -> bool:
    deadline = time.monotonic() + within
    while True:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state in {"Z", "X"}:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
