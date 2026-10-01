"""What the two flight-log formats share: citations, a sliding window over the source, aggregated
findings, table rows and the series slots rows are collected in.

Nothing here knows ULog or DataFlash. The windows are bounded (one block, 1 MiB, plus a record), so
a hostile log costs the chunk's rows, never its size in memory.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.contract import AdapterConfig, SourceReader, read_pieces
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind, Knowledge, Known, NotApplicable, Unknown
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    Locator,
    Provenance,
    adapter_locator,
)
from neptune.model.scalars import real
from neptune.model.series import (
    SEQ,
    ColumnType,
    SeriesBatch,
    SeriesColumn,
    SeriesProvenance,
    locator_column,
    state_column,
    step_template,
    time_column,
)
from neptune.model.time import INT64_MAX
from neptune.model.world import CellValue, StructuredRecord, StructuredTable

BLOCK: Final = 1024 * 1024
TIME_FIELD: Final = "flightlog:time_field"
TIME0: Final = time_column(0)
KNOWN, UNKNOWN, NOT_COVERED, NOT_APPLICABLE = "known", "unknown", "not_covered", "not_applicable"
# A table row is a record of several objects, a series row a few cells: rows of a table count
# for this many series rows when a chunk is cut, so a chunk's memory stays about the same.
TABLE_ROW_WEIGHT: Final = 16
MAX_COLUMNS: Final = 2048
MAX_KEYS: Final = 64  # distinct keys one finding code aggregates before folding into "other"
LOCATOR_STEP: Final = "byte_range"

Place = tuple[int, int]  # (offset, length) in the source


class Window:
    """Reads ``[start, end)`` of a source through one buffer of at least ``BLOCK`` bytes.

    ``at(offset, need)`` returns the index in ``buf`` of ``offset`` with ``need`` bytes there, or
    -1 when fewer than ``need`` bytes remain before ``end``. The buffer is replaced as the walk
    moves on, so an index is good only until the next call.
    """

    def __init__(self, source: SourceReader, start: int, end: int, block: int = BLOCK) -> None:
        self.source = source
        self.end = min(end, source.size)
        self.block = block
        self.buf = b""
        self.base = start

    def at(self, offset: int, need: int) -> int:
        if need < 0 or offset < 0 or offset + need > self.end:
            return -1
        index = offset - self.base
        if index >= 0 and index + need <= len(self.buf):
            return index
        want = min(self.end - offset, max(need, self.block))
        self.buf = b"".join(read_pieces(self.source, offset, offset + want))
        self.base = offset
        return 0

    def find(self, pattern: bytes, offset: int) -> int:
        """Absolute offset of the first ``pattern`` at or after ``offset``, or -1."""
        while offset + len(pattern) <= self.end:
            if self.at(offset, len(pattern)) < 0:  # loads the block that starts at offset
                return -1
            found = self.buf.find(pattern, offset - self.base)
            if found >= 0:
                return self.base + found
            offset = self.base + len(self.buf) - len(pattern) + 1
        return -1


class Cite:
    """Evidence, provenance and record ids for one source under one config."""

    def __init__(self, source: SourceReader, config: AdapterConfig) -> None:
        self.source = source
        self.config = config
        self.transform = config.transform

    def evidence(self, place: Place, *more: Locator) -> EvidenceRef:
        return EvidenceRef(self.source.content_id, (ByteRange(*place), *more))

    def provenance(
        self, place: Place, *more: Locator, kind: AssertionKind = AssertionKind.OBSERVED
    ) -> Provenance:
        return Provenance(self.evidence(place, *more), self.transform.id, kind)

    def stated(self, place: Place, *more: Locator) -> Provenance:
        return self.provenance(place, *more, kind=AssertionKind.STATED)

    def record_id(self, kind: str, place: Place, *more: Locator) -> RecordId:
        return evidence_record_id(kind, self.evidence(place, *more), self.transform)


def time_field(name: str) -> Locator:
    return adapter_locator(TIME_FIELD, {"name": name})


# --- Findings -----------------------------------------------------------------------------------


@dataclass
class _Group:
    count: int
    amount: int
    category: FindingCategory
    severity: Severity
    subject: Place
    message: str
    details: dict[str, JsonValue]
    records: tuple[RecordId, ...]


class Findings:
    """Findings of one call. A problem met many times is one finding per code and key: the first
    place it met, how many times, and the bytes or units it cost (``details``), so a hostile log
    cannot make a finding per sample."""

    def __init__(self, source: SourceReader, config: AdapterConfig, prefix: str) -> None:
        self.source = source
        self.config = config
        self.prefix = prefix
        self.items: list[IngestFinding] = []
        self._groups: dict[tuple[str, str], _Group] = {}
        self._keys: dict[str, int] = {}

    def add(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        subject: Place,
        message: str,
        details: Mapping[str, JsonValue] | None = None,
        *,
        records: Iterable[RecordId] = (),
    ) -> None:
        self.items.append(
            ingest_finding(
                code=f"{self.prefix}{code}",
                category=category,
                severity=severity,
                subject=EvidenceRef(self.source.content_id, (ByteRange(*subject),)),
                transform=self.config.transform,
                message=message,
                details=details,
                records=records,
            )
        )

    def aggregate(
        self,
        code: str,
        key: str,
        category: FindingCategory,
        severity: Severity,
        subject: Place,
        message: str,
        details: Mapping[str, JsonValue] | None = None,
        *,
        amount: int = 0,
        records: Iterable[RecordId] = (),
    ) -> None:
        """Count one more occurrence of ``code`` for ``key``; ``message`` is that of the first.

        A code keeps at most ``MAX_KEYS`` keys; further ones count under ``other``, so a log of
        thousands of distinct unknown ids is a few findings, not thousands."""
        if (code, key) not in self._groups and self._keys.get(code, 0) >= MAX_KEYS:
            key = "other"
        if (code, key) not in self._groups:
            self._keys[code] = self._keys.get(code, 0) + 1
        found = self._groups.get((code, key))
        if found is None:
            self._groups[(code, key)] = _Group(
                1, amount, category, severity, subject, message, dict(details or {}), tuple(records)
            )
        else:
            found.count += 1
            found.amount += amount

    def flush(self) -> tuple[IngestFinding, ...]:
        for (code, key), t in sorted(self._groups.items()):
            details = dict(t.details)
            details["count"] = t.count
            if t.amount:
                details["amount"] = t.amount
            if key:
                details["key"] = key
            self.add(code, t.category, t.severity, t.subject, t.message, details, records=t.records)
        self._groups.clear()
        return tuple(self.items)


# --- Table rows ---------------------------------------------------------------------------------


def int_cell(value: int, provenance: Provenance) -> Knowledge[CellValue]:
    """An integer as the log stores it; one past a signed 64-bit count is not representable."""
    return Known(value, provenance) if -(2**63) <= value <= INT64_MAX else Unknown(provenance)


def real_cell(value: float, provenance: Provenance) -> Knowledge[CellValue]:
    return Known(real(float(value)), provenance)


def text_cell(raw: bytes, provenance: Provenance) -> tuple[Knowledge[CellValue], bool]:
    """A C string: up to its first NUL. Blank is ``Unknown`` and invalid UTF-8 is too (flagged)."""
    raw = raw.split(b"\0", 1)[0]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Unknown(provenance), True
    return (Known(text, provenance) if text else Unknown(provenance)), False


def text_value(raw: bytes) -> str | None:
    """A C string as text, or ``None`` when it is not UTF-8."""
    try:
        return raw.split(b"\0", 1)[0].decode("utf-8")
    except UnicodeDecodeError:
        return None


@dataclass
class TableSlot:
    """The rows one chunk holds of one table, and what its records are cited by."""

    name: str
    first: Place  # where the table is declared: the place of its first row
    next_row: int
    table: StructuredTable | None = None
    rows: list[StructuredRecord] = field(default_factory=list)


class Tables:
    """Table records for one chunk: the table is emitted by the chunk that holds its first row."""

    def __init__(
        self, cite: Cite, declared: Mapping[str, JsonValue], starts: Mapping[str, int]
    ) -> None:
        self.cite = cite
        self.slots: dict[str, TableSlot] = {}
        for name, first in declared.items():
            if not (isinstance(first, list) and len(first) == 2):
                raise ValueError(f"table {name}: not a place: {first!r}")
            start = starts.get(name, 0)
            self.slots[name] = TableSlot(name, (int(str(first[0])), int(str(first[1]))), start)

    def add(
        self,
        name: str,
        place: Place,
        cells: tuple[Knowledge[CellValue], ...],
        header: Knowledge[tuple[str, ...]] | None = None,
        *more: Locator,
    ) -> RecordId:
        slot = self.slots[name]
        cite = self.cite
        if place == slot.first and slot.table is None:
            slot.table = StructuredTable(
                id=cite.record_id(StructuredTable.kind, slot.first),
                provenance=cite.provenance(slot.first),
                name=Known(name),
                header=header if header is not None else NotApplicable(),
            )
        row = slot.next_row
        slot.next_row += 1
        table_id = cite.record_id(StructuredTable.kind, slot.first)
        record = StructuredRecord(
            id=cite.record_id(StructuredRecord.kind, place, *more),
            provenance=cite.provenance(place, *more),
            table=table_id,
            row=row,
            cells=cells,
        )
        slot.rows.append(record)
        return record.id

    def records(self) -> list[StructuredTable | StructuredRecord]:
        found: list[StructuredTable | StructuredRecord] = []
        for slot in self.slots.values():
            if slot.table is not None:
                found.append(slot.table)
            found.extend(slot.rows)
        return found


# --- Series slots -------------------------------------------------------------------------------


def series_template(source: SourceReader) -> SeriesProvenance:
    """A row cites its record: one ``byte_range`` in the file, offset and length from the row."""
    step = step_template(LOCATOR_STEP, per_row=("length", "offset"))
    return SeriesProvenance(source.content_id, (step,), AssertionKind.OBSERVED)


ColumnSpec = tuple[str, ColumnType, bool]  # name, type, repeated


def base_columns() -> list[ColumnSpec]:
    return [
        (SEQ, ColumnType.INT64, False),
        (TIME0, ColumnType.INT64, False),
        (state_column(TIME0), ColumnType.STRING, False),
        (locator_column(0, "length"), ColumnType.INT64, False),
        (locator_column(0, "offset"), ColumnType.INT64, False),
    ]


class Slot:
    """The rows of one stream that one chunk holds, column by column."""

    def __init__(self, stream: RecordId, columns: Iterable[ColumnSpec], next_seq: int) -> None:
        self.stream = stream
        self.specs = tuple(sorted(columns))
        self.cells: dict[str, list[object]] = {name: [] for name, _, _ in self.specs}
        self.seq = next_seq
        self.length = locator_column(0, "length")
        self.offset = locator_column(0, "offset")
        self.state = state_column(TIME0)

    def row(
        self,
        place: Place,
        time: int | None,
        values: Mapping[str, object] | None = None,
        time_state: str | None = None,
    ) -> None:
        """Append a row. ``time`` is clock 0's ticks, or ``None`` for ``time_state`` (unknown)."""
        cells = self.cells
        cells[SEQ].append(self.seq)
        self.seq += 1
        cells[self.offset].append(place[0])
        cells[self.length].append(place[1])
        if time is None or time > INT64_MAX:
            cells[TIME0].append(None)
            cells[self.state].append(time_state or UNKNOWN)
        else:
            cells[TIME0].append(time)
            cells[self.state].append(KNOWN)
        for name, value in (values or {}).items():
            cells[name].append(value)

    def put(self, name: str, value: object) -> None:
        self.cells[name].append(value)

    @property
    def rows(self) -> int:
        return len(self.cells[SEQ])

    def batch(self) -> SeriesBatch:
        return SeriesBatch(
            self.stream,
            tuple(
                SeriesColumn(name, kind, tuple(self.cells[name]), repeated)  # type: ignore[arg-type]
                for name, kind, repeated in self.specs
            ),
        )


def place_of(data: JsonValue) -> Place:
    if (
        isinstance(data, list)
        and len(data) == 2
        and all(isinstance(n, int) and not isinstance(n, bool) for n in data)
    ):
        return (int(str(data[0])), int(str(data[1])))
    raise ValueError(f"not a place: {data!r}")


def as_int(data: JsonValue) -> int:
    if isinstance(data, bool) or not isinstance(data, int):
        raise ValueError(f"expected an integer, got {data!r}")
    return data


def as_str(data: JsonValue) -> str:
    if not isinstance(data, str):
        raise ValueError(f"expected a string, got {data!r}")
    return data


def as_list(data: JsonValue) -> list[JsonValue]:
    if not isinstance(data, list):
        raise ValueError(f"expected a list, got {data!r}")
    return data


def as_dict(data: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(data, dict):
        raise ValueError(f"expected an object, got {data!r}")
    return data


__all__ = [
    "BLOCK",
    "Cite",
    "Findings",
    "Slot",
    "Tables",
    "Window",
]
