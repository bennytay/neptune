"""Budgets on one answer: rows, Arrow bytes and wall time (Ledger ADR 0016 §6).

A budget never truncates silently. Exceeding a limit returns the longest prefix of the answer, in
its order, that fits, with one ``budget_exceeded`` finding per limit that cut it. Row and byte
cuts depend only on the catalog and the spec. A time cut depends on the wall clock, so the
answer it gives is flagged as not reproducible; it is still a prefix, so the same spec with
``max_rows`` set to the rows it returned and no time limit gives the same rows.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from neptune_ledger.api.types import BudgetLimit, BudgetReport, CatalogFinding, QueryBudget

Clock = Callable[[], float]
MIB: Final = 2**20


@dataclass(frozen=True)
class QueryLimits:
    """The Ledger's defaults for limits a spec leaves out, and the ceilings no spec may exceed.

    ``batch_rows`` is how many record rows one catalog statement returns when the scan can stop
    between batches (a lineage filter or a time limit); ``max_streams`` bounds the streams a
    series join reads; ``sql_millis`` and ``sql_memory`` bound an SQL passthrough statement.
    """

    max_rows: int = 1_000_000
    max_bytes: int = 512 * MIB
    max_millis: int | None = None
    sql_millis: int = 30_000
    sql_memory: str = "1GB"
    ceiling_rows: int = 10_000_000
    ceiling_bytes: int = 4096 * MIB
    ceiling_millis: int = 600_000
    batch_rows: int = 10_000
    max_streams: int = 10_000


def over_ceiling(budget: QueryBudget | None, limits: QueryLimits) -> list[str]:
    """The limits of ``budget`` above the Ledger's ceilings, by name."""
    if budget is None:
        return []
    found = []
    for name, value, ceiling in (
        ("max_rows", budget.max_rows, limits.ceiling_rows),
        ("max_bytes", budget.max_bytes, limits.ceiling_bytes),
        ("max_millis", budget.max_millis, limits.ceiling_millis),
    ):
        if value is not None and value > ceiling:
            found.append(f"{name} is at most {ceiling}")
    return found


def effective(budget: QueryBudget | None, limits: QueryLimits, *, sql: bool = False) -> QueryBudget:
    """The limits that apply: the spec's, and the Ledger's defaults for the others."""
    given = budget or QueryBudget()
    default_millis = limits.sql_millis if sql else limits.max_millis
    return QueryBudget(
        max_rows=given.max_rows if given.max_rows is not None else limits.max_rows,
        max_bytes=given.max_bytes if given.max_bytes is not None else limits.max_bytes,
        max_millis=given.max_millis if given.max_millis is not None else default_millis,
    )


class Budget:
    """One call's limits, its deadline, and which limits have cut its answer so far."""

    def __init__(self, limits: QueryBudget, clock: Clock = time.monotonic) -> None:
        assert limits.max_rows is not None and limits.max_bytes is not None
        self.limits = limits
        self.max_rows: int = limits.max_rows
        self.max_bytes: int = limits.max_bytes
        self._clock = clock
        self._deadline = None if limits.max_millis is None else clock() + limits.max_millis / 1000
        self.exceeded: set[BudgetLimit] = set()

    def remaining(self) -> float | None:
        """Seconds left before the deadline, at least 0; None without a time limit."""
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - self._clock())

    def out_of_time(self) -> bool:
        """Whether the deadline has passed; once it has, the time limit has cut the answer."""
        left = self.remaining()
        if left is not None and left <= 0:
            self.exceeded.add("time")
            return True
        return False

    def time_ran_out(self) -> None:
        """An engine stopped a statement at the deadline."""
        self.exceeded.add("time")

    def cut(self, table: Any) -> Any:
        """The longest prefix of ``table`` within the row and byte limits, as one chunk."""
        table = table.combine_chunks()
        if table.num_rows > self.max_rows:
            table = table.slice(0, self.max_rows)
            self.exceeded.add("rows")
        if table.nbytes > self.max_bytes:
            # A prefix's bytes grow with its length, so the longest one that fits is found by
            # bisection. Dictionaries count whole in every prefix, so even no row may not fit.
            low, high = 0, table.num_rows
            while low < high:
                middle = (low + high + 1) // 2
                if table.slice(0, middle).nbytes <= self.max_bytes:
                    low = middle
                else:
                    high = middle - 1
            table = table.slice(0, low)
            self.exceeded.add("bytes")
        return table

    def findings(self, table: Any) -> tuple[CatalogFinding, ...]:
        """One ``budget_exceeded`` finding per limit that cut the answer, in name order."""
        rows = table.num_rows
        detail = {
            "bytes": f"the answer exceeds {self.max_bytes} bytes of Arrow data; its first {rows}"
            f" rows are returned",
            "rows": f"more than {self.max_rows} rows answer the query; the first {rows} are"
            " returned",
            "time": f"the time budget of {self.limits.max_millis} ms ran out; the first {rows}"
            " rows of the answer are returned, and which rows depends on the wall clock",
        }
        return tuple(
            CatalogFinding("budget_exceeded", limit, detail[limit])
            for limit in sorted(self.exceeded)
        )

    def report(self, table: Any) -> BudgetReport:
        return BudgetReport(
            limits=self.limits,
            rows=table.num_rows,
            bytes=table.nbytes,
            exceeded=tuple(sorted(self.exceeded)),
            reproducible="time" not in self.exceeded,
        )
