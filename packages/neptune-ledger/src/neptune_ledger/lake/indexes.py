"""Reads over the derived time and spatial indexes of one tenant (ADR 0015).

``IndexCatalog.window`` answers "what exists on clock C between t1 and t2" and
``IndexCatalog.within`` "what is in this box of frame F". Neither is a catalog-API call yet:
``query`` (MVL-98) and ``access/`` (MVL-99) decide that surface. Every call runs in one read-only
snapshot at one catalog point, which it returns, and every request outside its rules is a
structured refusal, never an exception and never a guess: a window that names another clock
without a ``ClockMapping``, or a box that names no frame.
"""

import math
from collections.abc import Sequence
from typing import Any, Final

import psycopg
from psycopg import sql

from neptune.model.frames import FrameRef
from neptune.model.ids import parse_record_id
from neptune.model.knowledge import Knowledge, Known, NotCovered
from neptune.model.spatial import CrsCode
from neptune.model.units import unit_from_json
from neptune_ledger.api import codec
from neptune_ledger.api.types import CatalogFinding, TimeWindow, TransactionKey
from neptune_ledger.catalog.migrate import tenant_schema
from neptune_ledger.lake.space_index import (
    CrsReference,
    FrameReference,
    Reference,
    SpatialBox,
    SpatialResult,
    read_within,
)
from neptune_ledger.lake.time_index import WindowResult, read_window
from neptune_ledger.threads.alignment import clock_mapping
from neptune_ledger.threads.merge import ClockMapping, IntervalMapper

Conn = psycopg.Connection[tuple[Any, ...]]

_MAX_SEQ: Final = 2**63 - 1
# Bounds on a request's lists: a window names a few clocks and mappings, not thousands.
MAX_CLOCKS: Final = 64
MAX_MAPPINGS: Final = 256


class IndexCatalog:
    """The time and spatial indexes of ``tenant_id``'s catalog, read-only."""

    def __init__(self, conninfo: str, tenant_id: str) -> None:
        self._conninfo = conninfo
        self._tenant = tenant_id
        self._schema = tenant_schema(tenant_id)
        self._conn: Conn | None = None

    def __enter__(self) -> "IndexCatalog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connection(self) -> Conn:
        if self._conn is None or self._conn.closed:
            conn: Conn = psycopg.connect(self._conninfo, autocommit=True)
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self._schema)))
            self._conn = conn
        return self._conn

    # --- time ------------------------------------------------------------------------------------

    def window(
        self,
        window: TimeWindow,
        *,
        clocks: Sequence[str] = (),
        mappings: Sequence[str] = (),
        as_of: int | None = None,
    ) -> WindowResult:
        """Every interval on ``window.clock`` whose stated extent meets the window, and, for each
        other clock in ``clocks``, every interval that the named ``ClockMapping`` records carry
        into it (ADR 0015 §3). Naming another clock without mappings is refused."""
        named_clocks = tuple(dict.fromkeys(c for c in clocks if c != getattr(window, "clock", None)))
        named_mappings = tuple(dict.fromkeys(mappings))
        problem = _window_problem(window, named_clocks, named_mappings, as_of)
        conn = self._connection()
        with conn.transaction():
            point, limit, beyond = _point(conn, as_of)
            if problem is None and beyond:
                problem = (_beyond(as_of),)
            if problem is None:
                problem, held = self._mappings(conn, window, named_clocks, named_mappings, limit)
            if problem:
                return WindowResult(
                    "refused", window, named_clocks, named_mappings, point, (), tuple(problem)
                )
            entries, findings = read_window(conn, self._tenant, window, named_clocks, held, limit)
        return WindowResult(
            "answered", window, named_clocks, named_mappings, point, entries, findings
        )

    def _mappings(
        self,
        conn: Conn,
        window: TimeWindow,
        clocks: tuple[str, ...],
        mappings: tuple[str, ...],
        limit: int,
    ) -> tuple[tuple[CatalogFinding, ...], tuple[ClockMapping, ...]]:
        """Why the window is refused, or nothing, and the mappings it names."""
        held_clocks = {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT c.clock_id FROM clock c JOIN package p USING (tenant_id, package_id)"
                " WHERE c.tenant_id = %s AND c.clock_id = ANY(%s) AND p.tx_seq <= %s",
                (self._tenant, [window.clock, *clocks], limit),
            ).fetchall()
        }
        found = [
            CatalogFinding("unknown_clock", clock, "no registered package holds this clock")
            for clock in (window.clock, *clocks)
            if clock not in held_clocks
        ]
        # A record id names one body in every package that holds it (ADR 0002 §6), so the first
        # registration's row is the mapping (as for a thread merge, ADR 0010 §9).
        held = {
            str(record): clock_mapping(str(record), str(source), str(target), str(text))
            for record, source, target, text in conn.execute(
                "SELECT DISTINCT ON (record_id) record_id, source_clock, target_clock, mapping"
                " FROM thread_clock_mapping WHERE tenant_id = %s AND record_id = ANY(%s)"
                "   AND registration_key <= %s ORDER BY record_id, registration_key",
                (self._tenant, list(mappings), limit),
            ).fetchall()
        }
        found += [
            CatalogFinding("unknown_mapping", m, "no registered package holds a ClockMapping with this id")
            for m in sorted(mappings, key=lambda m: m.encode("utf-8"))
            if m not in held
        ]
        if found:
            return tuple(found), ()
        usable = tuple(held[m] for m in sorted(held, key=lambda m: m.encode("utf-8")))
        mapper = IntervalMapper(window.clock, usable)
        cut = [
            CatalogFinding(
                "invalid_request",
                clock,
                f"no path of the named usable ClockMappings joins this clock to the window's clock"
                f" {window.clock}; intervals on two clocks are never compared without one",
            )
            for clock in clocks
            if not mapper.reaches(clock)
        ]
        return tuple(cut), usable

    # --- space -----------------------------------------------------------------------------------

    def within(
        self, reference: Reference, unit: str, box: SpatialBox, *, as_of: int | None = None
    ) -> SpatialResult:
        """The records whose extent in ``reference`` and ``unit`` meets ``box``, and the other
        members of ``reference`` the box cannot be compared with (ADR 0015 §4)."""
        problem = _space_problem(reference, unit, box, as_of)
        conn = self._connection()
        with conn.transaction():
            point, limit, beyond = _point(conn, as_of)
            if problem is None and beyond:
                problem = (_beyond(as_of),)
            if problem:
                return SpatialResult("refused", reference, unit, box, point, (), (), problem)
            placed, unplaced = read_within(conn, self._tenant, reference, unit, box, limit)
        return SpatialResult("answered", reference, unit, box, point, placed, unplaced, ())


def _point(conn: Conn, as_of: int | None) -> tuple[Knowledge[TransactionKey], int, bool]:
    """The catalog point, its tx_seq, and whether ``as_of`` is beyond the latest point."""
    row = conn.execute(
        "SELECT c.last_seq, c.last_time, l.tx_time FROM tx_clock c"
        " LEFT JOIN registration_log l ON l.tenant_id = c.tenant_id AND l.tx_seq = %s",
        (as_of if as_of is not None and as_of <= _MAX_SEQ else 0,),
    ).fetchone()
    assert row is not None  # every migrated tenant has its clock row
    last_seq, last_time, at = row
    latest: Knowledge[TransactionKey] = (
        Known(TransactionKey(int(last_seq), str(last_time))) if last_seq else NotCovered()
    )
    if as_of is None:
        return latest, int(last_seq), False
    if as_of > int(last_seq):
        return latest, int(last_seq), True
    return Known(TransactionKey(as_of, str(at))), as_of, False


def _beyond(as_of: int | None) -> CatalogFinding:
    detail = "as_of is beyond the latest committed catalog point"
    return CatalogFinding("as_of_out_of_range", str(as_of), detail)


def _bad_as_of(as_of: object) -> CatalogFinding | None:
    if as_of is None:
        return None
    if isinstance(as_of, bool) or not isinstance(as_of, int) or not 1 <= as_of <= _MAX_SEQ:
        return CatalogFinding("invalid_request", str(as_of)[:200], "as_of is a tx_seq, at least 1")
    return None


def _window_problem(
    window: object, clocks: tuple[str, ...], mappings: tuple[str, ...], as_of: object
) -> tuple[CatalogFinding, ...] | None:
    found: list[CatalogFinding] = []
    if not isinstance(window, TimeWindow) or not _valid(window):
        detail = "a window is a TimeWindow: a clock's record id and two int64 ticks"
        found.append(CatalogFinding("invalid_request", "window", detail))
    elif window.first > window.last:
        found.append(CatalogFinding("invalid_request", "window", "first is after last"))
    if len(clocks) > MAX_CLOCKS or not all(_record_id(c) for c in clocks):
        detail = f"clocks are at most {MAX_CLOCKS} TimestampDomain record ids"
        found.append(CatalogFinding("invalid_request", "clocks", detail))
    if len(mappings) > MAX_MAPPINGS or not all(_record_id(m) for m in mappings):
        detail = f"mappings are at most {MAX_MAPPINGS} ClockMapping record ids"
        found.append(CatalogFinding("invalid_request", "mappings", detail))
    if clocks and not mappings:
        clock = window.clock if isinstance(window, TimeWindow) else "window"
        for other in clocks:
            detail = (
                f"intervals on {other} are never compared with a window on {clock} unless the"
                " request names the ClockMapping records that relate them (ADR 0015 §3)"
            )
            found.append(CatalogFinding("invalid_request", str(other)[:200], detail))
    bad = _bad_as_of(as_of)
    if bad is not None:
        found.append(bad)
    return tuple(found) or None


def _space_problem(
    reference: object, unit: object, box: object, as_of: object
) -> tuple[CatalogFinding, ...] | None:
    found: list[CatalogFinding] = []
    if not _reference_ok(reference):
        detail = (
            "a spatial query names its frame (frame graph id and frame id) or its CRS (authority"
            " and code); there is no default world frame"
        )
        found.append(CatalogFinding("invalid_request", "reference", detail))
    try:
        unit_from_json(unit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        detail = "a unit is a canonical unit symbol (m, mm, deg); coordinates are never converted"
        found.append(CatalogFinding("invalid_request", "unit", detail))
    if not _box_ok(box):
        detail = "a box is 2 or 3 finite low and high coordinates, low <= high on every axis"
        found.append(CatalogFinding("invalid_request", "box", detail))
    bad = _bad_as_of(as_of)
    if bad is not None:
        found.append(bad)
    return tuple(found) or None


def _reference_ok(reference: object) -> bool:
    try:
        if isinstance(reference, FrameReference):
            FrameRef(reference.frame_id, reference.frame_graph_id)  # type: ignore[arg-type]
            return True
        if isinstance(reference, CrsReference):
            CrsCode(reference.authority, reference.code)
            return True
    except (TypeError, ValueError):
        return False
    return False


def _box_ok(box: object) -> bool:
    if not isinstance(box, SpatialBox):
        return False
    low, high = box.low, box.high
    if not isinstance(low, tuple) or not isinstance(high, tuple) or len(low) != len(high):
        return False
    if len(low) not in (2, 3):
        return False
    for a, b in zip(low, high, strict=True):
        for v in (a, b):
            if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
                return False
        if a > b:
            return False
    return True


def _record_id(value: object) -> bool:
    try:
        parse_record_id(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return True


def _valid(value: object) -> bool:
    try:
        codec.to_json(value)
    except (codec.CodecError, TypeError, ValueError):
        return False
    return True
