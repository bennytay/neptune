"""The time index: every interval a package states or holds, per clock (ADR 0015 §1, §3).

Registration writes one ``time_interval`` row per interval, from the verified package:

- a **record** with world time (ADR 0003 §3: runs, streams, calibrations), its ``record.world_*``
  exactly: start ticks, and the end on the same clock or NULL when it is open;
- a **series file**, per clock its stream carries (``time/<i>``), the least and greatest known
  tick and how many rows have, or lack, a known tick there. Read from the file's own column, never
  from Parquet statistics, which the writer computed and nothing verified.

``read_window`` answers "what exists on clock C between t1 and t2" with one index lookup per
clock. Intervals on another clock are returned only when the caller names that clock and the
``ClockMapping`` records to carry it onto C; a request naming another clock without mappings is
refused, never answered by comparing ticks of two clocks. Ticks are never converted.
"""

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import psycopg
import pyarrow.compute as pc
import pyarrow.parquet as pq

from neptune.model.knowledge import Knowledge
from neptune.model.run import Stream
from neptune.model.series import time_column
from neptune.store.package import IngestPackage, series_path
from neptune_ledger.api.types import (
    CatalogFinding,
    MappedInterval,
    MappingPath,
    TimeWindow,
    TransactionKey,
)
from neptune_ledger.catalog.check import open_below
from neptune_ledger.threads.merge import (
    MAX_ENTRY_STEPS,
    MAX_MERGE_STEPS,
    MAX_PATHS_NAMED,
    ClockMapping,
    IntervalMapper,
    Unmapped,
)

Conn = psycopg.Connection[tuple[Any, ...]]
Subject = Literal["record", "series"]

# Rows per batch when a series file's clock columns are read at registration.
READ_ROWS: Final = 65_536

# The rows of one clock whose stated extent [first, last or first] meets [lo, hi], at a catalog
# point. The GiST range search and the B-tree on (clock, first_tick) both serve it; the extent
# test on first_tick and last_tick is what decides.
_WINDOW: Final = """
SELECT subject, kind, record_id, package_id, clock, first_tick, last_tick, rows_known,
       rows_unknown, registration_key
  FROM time_interval
 WHERE tenant_id = %(tenant)s AND clock = %(clock)s
   AND span && int8range(%(lo)s, %(hi)s, '[]')
   AND first_tick <= %(hi)s AND coalesce(last_tick, first_tick) >= %(lo)s
   AND registration_key <= %(as_of)s
"""
# The same, for a clock read whole: a window whose upper end is the largest tick.
_WINDOW_TO_END: Final = _WINDOW.replace("int8range(%(lo)s, %(hi)s, '[]')", "int8range(%(lo)s, NULL)")


@dataclass(frozen=True)
class IntervalRow:
    """One ``time_interval`` row without tenant, package and registration key."""

    subject: Subject
    kind: str
    record_id: str
    clock: str
    first: int
    last: int | None
    rows_known: int | None = None
    rows_unknown: int | None = None


def record_intervals(records: Iterable[Any]) -> tuple[IntervalRow, ...]:
    """The intervals of a package's record rows (``catalog.index.RecordRow``) with world time."""
    return tuple(
        sorted(
            (
                IntervalRow("record", r.kind, r.record_id, r.world_clock, r.world_first, r.world_last)
                for r in records
                if r.world_clock is not None
            ),
            key=_row_key,
        )
    )


def series_intervals(root_fd: int, package: IngestPackage) -> tuple[IntervalRow, ...]:
    """The per-clock intervals of a verified package's series files, read from ``root_fd``.

    Each file is opened below the package root without following a link (ADR 0006 §3), and only
    its ``time/<i>`` columns are read. A clock with no known tick in the file has no interval.
    Raises ``OSError`` or ``ValueError`` when a file cannot be read as the stream's series.
    """
    streams = {r.id: r for r in package.records if isinstance(r, Stream)}
    out: list[IntervalRow] = []
    for stream_id in sorted(package.series):
        stream = streams[stream_id]
        names = [time_column(i) for i in range(len(stream.clocks))]
        spans: list[tuple[int, int] | None] = [None] * len(names)
        known = [0] * len(names)
        unknown = [0] * len(names)
        with os.fdopen(open_below(root_fd, series_path(stream_id)), "rb") as handle:
            parquet = pq.ParquetFile(handle)
            for batch in parquet.iter_batches(batch_size=READ_ROWS, columns=names):
                for i in range(len(names)):
                    column = batch.column(i)
                    if str(column.type) != "int64":
                        raise ValueError(f"{names[i]} of stream {stream_id} is not int64 ticks")
                    unknown[i] += column.null_count
                    count = len(column) - column.null_count
                    if not count:
                        continue
                    known[i] += count
                    extremes = pc.min_max(column).as_py()
                    low, high = int(extremes["min"]), int(extremes["max"])
                    span = spans[i]
                    spans[i] = (low, high) if span is None else (min(span[0], low), max(span[1], high))
        for i, clock in enumerate(stream.clocks):
            span = spans[i]
            if span is not None:
                out.append(
                    IntervalRow(
                        "series", "stream", stream_id, clock, span[0], span[1], known[i], unknown[i]
                    )
                )
    return tuple(sorted(out, key=_row_key))


def _row_key(row: IntervalRow) -> tuple[str, str, str]:
    return row.subject, row.record_id, row.clock


# --- Reads -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class IntervalEntry:
    """One interval in a window's answer.

    ``first`` and ``last`` are the stated ticks on ``clock`` (``last`` None when the end is
    open); ``rows_known`` and ``rows_unknown`` count a series file's rows with and without a
    known tick on that clock. ``mapped`` is set only for an interval on another clock: its
    interval on the window's clock, widened by the bounds of the mappings in its path.
    """

    subject: Subject
    kind: str
    record_id: str
    package_id: str
    clock: str
    first: int
    last: int | None
    registration_key: int
    rows_known: int | None = None
    rows_unknown: int | None = None
    mapped: MappedInterval | None = None

    @property
    def end(self) -> int:
        """The stated extent's end: ``last``, or ``first`` when the end is open."""
        return self.first if self.last is None else self.last


@dataclass(frozen=True)
class WindowResult:
    """The answer to a window query at catalog point ``as_of``.

    ``refused`` means nothing was compared; ``findings`` say why. An answer lists every interval
    on the window's clock whose stated extent meets the window, then merges in the intervals of
    the other named clocks whose mapped interval meets it, ordered by ``(lo, hi, clock bytes,
    native key)`` as ADR 0003 §3.5 orders a merged thread.
    """

    outcome: Literal["answered", "refused"]
    window: TimeWindow
    clocks: tuple[str, ...]
    mappings: tuple[str, ...]
    as_of: Knowledge[TransactionKey]
    entries: tuple[IntervalEntry, ...]
    findings: tuple[CatalogFinding, ...]


def read_window(
    conn: Conn,
    tenant: str,
    window: TimeWindow,
    clocks: Sequence[str],
    mappings: Sequence[ClockMapping],
    limit: int,
) -> tuple[tuple[IntervalEntry, ...], tuple[CatalogFinding, ...]]:
    """The entries of a validated window query at ``limit`` (a tx_seq), and its findings.

    Every named clock other than the window's has a path of usable ``mappings`` to it (the
    request was refused otherwise). Each clock is one index lookup: the window itself on its own
    clock, and on another clock the native range ``IntervalMapper.candidates`` bounds; each
    candidate is then carried through its best usable path and kept if it meets the window.
    """
    mapper = IntervalMapper(window.clock, mappings)
    findings = [
        CatalogFinding(
            "unsupported_mapping",
            m.mapping_id,
            f"{m.unsupported or 'not an affine, increasing map between two clocks'}; not used",
        )
        for m in mapper.unusable
    ]
    keyed: list[tuple[tuple[Any, ...], IntervalEntry]] = []
    for clock in (window.clock, *clocks):
        bounds = mapper.candidates(clock, window.first, window.last)
        lo, hi = bounds if bounds is not None else (_INT64_MIN, _INT64_MAX)
        for entry in _lookup(conn, tenant, clock, max(lo, _INT64_MIN), min(hi, _INT64_MAX), limit):
            if clock == window.clock:
                keyed.append((_merged_key(entry.first, entry.end, entry), entry))
                continue
            placed = mapper.map(clock, entry.first, entry.end)
            if isinstance(placed, Unmapped):
                findings.append(_out_of_range(entry, placed))
                continue
            if placed.lo > window.last or placed.hi < window.first:
                continue
            mapped = MappedInterval(window.clock, placed.lo, placed.hi, placed.path)
            keyed.append(
                (_merged_key(placed.lo, placed.hi, entry), _with_mapped(entry, mapped))
            )
    keyed.sort(key=lambda pair: pair[0])
    findings.sort(key=lambda f: (f.code, f.subject.encode("utf-8"), f.detail))
    return tuple(e for _, e in keyed), tuple(findings)


_INT64_MIN: Final = -(2**63)
_INT64_MAX: Final = 2**63 - 1


def _lookup(conn: Conn, tenant: str, clock: str, lo: int, hi: int, limit: int) -> list[IntervalEntry]:
    statement = _WINDOW_TO_END if hi == _INT64_MAX else _WINDOW
    rows = conn.execute(
        statement,
        {"tenant": tenant, "clock": clock, "lo": lo, "hi": hi, "as_of": limit},
    ).fetchall()
    return [
        IntervalEntry(
            subject=subject,
            kind=str(kind),
            record_id=str(record),
            package_id=str(package),
            clock=str(at),
            first=int(first),
            last=None if last is None else int(last),
            registration_key=int(seq),
            rows_known=None if known is None else int(known),
            rows_unknown=None if missing is None else int(missing),
        )
        for subject, kind, record, package, at, first, last, known, missing, seq in rows
    ]


def _native_key(entry: IntervalEntry) -> tuple[Any, ...]:
    """A strict total order within one clock: extent, registration key, then identity bytes."""
    return (
        entry.first,
        entry.end,
        entry.registration_key,
        entry.subject,
        entry.record_id.encode("utf-8"),
        entry.package_id.encode("utf-8"),
    )


def _merged_key(lo: int, hi: int, entry: IntervalEntry) -> tuple[Any, ...]:
    return (lo, hi, entry.clock.encode("utf-8"), _native_key(entry))


def _with_mapped(entry: IntervalEntry, mapped: MappedInterval) -> IntervalEntry:
    return IntervalEntry(
        entry.subject,
        entry.kind,
        entry.record_id,
        entry.package_id,
        entry.clock,
        entry.first,
        entry.last,
        entry.registration_key,
        entry.rows_known,
        entry.rows_unknown,
        mapped,
    )


def _out_of_range(entry: IntervalEntry, search: Unmapped) -> CatalogFinding:
    named = sorted(search.tried, key=lambda ids: tuple(i.encode("utf-8") for i in ids))
    if search.exhausted:
        detail = (
            f"the path search stopped at its step budget ({MAX_ENTRY_STEPS} per interval,"
            f" {MAX_MERGE_STEPS} per query); the interval is not compared with the window"
        )
    else:
        detail = "no path's validity windows hold this interval; it is not compared with the window"
    if len(named) > MAX_PATHS_NAMED:
        detail += f"; {MAX_PATHS_NAMED} of {len(named)} paths tried are named"
    return CatalogFinding(
        "mapping_out_of_range",
        entry.record_id,
        f"{detail} ({entry.subject} on clock {entry.clock}, package {entry.package_id})",
        paths_tried=tuple(MappingPath(ids) for ids in named[:MAX_PATHS_NAMED]) or None,
    )
