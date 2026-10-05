"""SQL passthrough: one SELECT over views of one scoped answer, in a sealed DuckDB (ADR 0016 §7).

The statement is hostile input. Before it runs:

- DuckDB's own parser must find exactly one statement, of type ``SELECT``; anything else
  (DDL, DML, ``SET``, ``PRAGMA`` writes, ``ATTACH``, ``COPY``, ``INSTALL``, ``LOAD``, ``CALL``,
  ``EXPORT``, ``EXPLAIN``, a second statement) is refused;
- a new in-memory database is opened per call with external access off (no file, glob, attach,
  copy or HTTP), extension install and autoload off, unsigned and community extensions refused,
  Python replacement scans off (a table name never reaches an object in a caller's frame), one
  thread, a memory limit, no spilling to disk, and its configuration locked;
- the only data in it are the views, registered from Arrow tables: the scope's own answer.

A watchdog interrupts the statement at the budget's deadline. Engine errors are findings.
"""

import threading
from typing import Any, Final

import pyarrow as pa

from neptune_ledger.api.types import CatalogFinding
from neptune_ledger.query.budget import Budget

MAX_STATEMENT: Final = 64 * 1024
FETCH_ROWS: Final = 8192


def _config(memory: str) -> dict[str, str | bool | int | float | list[str]]:
    return {
        "enable_external_access": "false",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "allow_unsigned_extensions": "false",
        "allow_community_extensions": "false",
        "python_enable_replacements": "false",
        "threads": "1",
        "memory_limit": memory,
        "max_temp_directory_size": "0B",
        "preserve_insertion_order": "true",
        "lock_configuration": "true",
    }


def _refused(detail: str) -> tuple[None, list[CatalogFinding]]:
    return None, [CatalogFinding("invalid_request", "statement", detail)]


def run_sql(
    statement: object, views: dict[str, Any], budget: Budget, memory: str
) -> tuple[Any | None, list[CatalogFinding]]:
    """The statement's rows (a prefix when a limit cut them) and findings; ``None`` and an
    ``invalid_request`` finding when the statement is refused or fails."""
    import duckdb

    if not isinstance(statement, str) or not statement.strip():
        return _refused("the statement is a non-empty string")
    if len(statement.encode("utf-8", "surrogatepass")) > MAX_STATEMENT or "\x00" in statement:
        return _refused(f"the statement is at most {MAX_STATEMENT} bytes, without NUL")
    con = duckdb.connect(":memory:", config=_config(memory))
    watchdog: threading.Timer | None = None
    try:
        try:
            parsed = con.extract_statements(statement)
        except duckdb.Error as exc:
            return _refused(_first_line(exc))
        if len(parsed) != 1 or parsed[0].type != duckdb.StatementType.SELECT:
            kinds = ", ".join(p.type.name for p in parsed) or "none"
            return _refused(f"exactly one SELECT statement is accepted, not: {kinds}")
        for name, table in views.items():
            con.register(name, table)
        left = budget.remaining()
        if left is not None:
            if left <= 0:
                budget.time_ran_out()
                return _empty(), []
            watchdog = threading.Timer(left, con.interrupt)
            watchdog.start()
        batches: list[Any] = []
        rows = size = 0
        schema = None
        try:
            reader = con.execute(statement).to_arrow_reader(FETCH_ROWS)
            schema = reader.schema
            for batch in reader:
                batches.append(batch)
                rows += batch.num_rows
                size += batch.nbytes
                # One row past the row limit, or past the byte limit, shows a cut: stop reading.
                # So does the deadline, which an interrupt lost before the statement started
                # would otherwise not enforce.
                if rows > budget.max_rows or size > budget.max_bytes or budget.out_of_time():
                    break
        except (pa.ArrowException, duckdb.Error) as exc:
            if not _interrupted(exc):
                return _refused(_first_line(exc))
            budget.time_ran_out()
        if schema is None:
            return _empty(), []
        return pa.Table.from_batches(batches, schema=schema), []
    finally:
        if watchdog is not None:
            watchdog.cancel()
        con.close()


def _interrupted(exc: BaseException) -> bool:
    """The watchdog stopped the statement: DuckDB's interrupt, also when the Arrow reader
    surfaces it as its own error."""
    import duckdb

    return isinstance(exc, duckdb.InterruptException) or "interrupt" in str(exc).lower()


def _empty() -> Any:
    return pa.table({})


def _first_line(exc: BaseException) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0][:300]
