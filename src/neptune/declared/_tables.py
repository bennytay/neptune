"""Registers: a table whose declared header names a kind's id column (ADR 0063 §2).

A table is a register when its header is declared (``Known``: a CSV read with
``csv_header=first_row``, a Markdown or Parquet table) and one of its columns is a kind's id
column (``asset_id``, ``Asset ID``). Each row is then one record of that kind, citing the row, and
each field cites its cell. A JSON table, whose rows are objects, names its columns by each cell's
own key instead. A CSV whose header is undeclared states no column names: if its first row reads
like a register's header, that is a candidate, never a register.
"""

import math
from collections.abc import Sequence
from typing import Any, Final

from neptune.declared._emit import Output, field_key
from neptune.declared._entries import KIND_KEYS, Entry, Value, build
from neptune.model.ids import RecordId
from neptune.model.knowledge import Knowledge, Known, KnownAbsent, NotCovered, Unknown
from neptune.model.provenance import EvidenceRef, JsonPointer, Provenance
from neptune.model.world import StructuredRecord, StructuredTable

# How sure the undeclared-header rule is that a first row is a register's header.
UNDECLARED_HEADER_CONFIDENCE: Final = 0.6


def _not_covered(out: Output, evidence: EvidenceRef) -> Knowledge[object]:
    return NotCovered(out.prov(evidence))


def _pointer_key(evidence: EvidenceRef) -> str | None:
    """A JSON row's cell key: the one segment of its pointer (``/asset_id``), unescaped."""
    last = evidence.locator[-1]
    if isinstance(last, JsonPointer) and last.pointer.count("/") == 1:
        return last.pointer[1:].replace("~1", "/").replace("~0", "~")
    return None


def _cell_value(state: Knowledge[Any], evidence: EvidenceRef) -> Value:
    match state:
        case Known(value=str() as text):
            return Value(evidence, text)
        case Known(value=bool()):
            return Value(evidence, not_text=True)
        case Known(value=int() as number):
            return Value(evidence, str(number))  # a declared integer's digits, exactly
        case Known(value=float() as number) if math.isfinite(number):
            return Value(evidence, not_text=True, number=number)  # a typed number: a coordinate
        case Known():
            return Value(evidence, not_text=True)
        case KnownAbsent(provenance=Provenance(evidence=defined)):
            return Value(defined, absent=True)
        case _:
            return Value(evidence)


def _entry(table: StructuredTable, row: StructuredRecord) -> dict[str, Value]:
    entry: dict[str, Value] = {}
    header = table.header.value if isinstance(table.header, Known) else None
    for column, state in enumerate(row.cells):
        evidence = row.cell_evidence(table, column)
        if header is not None:
            name: str | None = header[column] if column < len(header) else None
        else:  # a JSON row's cell names its key in its own citation (a null's cites itself)
            grounding = getattr(state, "provenance", None)
            own = grounding.evidence if isinstance(grounding, Provenance) else evidence
            name = _pointer_key(own)
        if name:
            entry.setdefault(field_key(name), _cell_value(state, evidence))
    return entry


def _kind(entry: Entry) -> str | None:
    for key, kind in KIND_KEYS:
        if key in entry:
            return kind
    return None


def table_wanted(table: StructuredTable) -> bool:
    """Whether any row of ``table`` can declare something: a register's header names a kind's
    id column; an undeclared table's first row may be a candidate; a JSON table's rows name
    their own keys. A table with a declared header and no id column (telemetry) cannot."""
    match table.header:
        case Known(value=header):
            return any(field_key(name) in _ID_KEYS for name in header)
        case _:
            return True


def row_wanted(table: StructuredTable, row: StructuredRecord) -> bool:
    """Whether the pass reads ``row`` of ``table``: every row of a register, the first row of
    an undeclared table, and a JSON row whose own keys name a kind. A telemetry table's millions
    of rows need never be held for it."""
    match table.header:
        case Unknown():
            return row.row == 0
        case Known(value=header):
            return any(field_key(name) in _ID_KEYS for name in header)
        case _:
            return _kind(_entry(table, row)) is not None


_ID_KEYS: Final = frozenset(key for key, _ in KIND_KEYS)


def read_table(out: Output, table: StructuredTable, rows: Sequence[StructuredRecord]) -> None:
    if isinstance(table.header, Unknown):
        _undeclared(out, table, rows)
        return
    for row in rows:
        entry = _entry(table, row)
        kind = _kind(entry)
        if kind is None:
            continue
        evidence = row.provenance.evidence
        record = build(out, kind, entry, evidence, table.id, _not_covered, (f"{kind}_id",))
        if record is not None:
            out.add(record)


def _undeclared(out: Output, table: StructuredTable, rows: Sequence[StructuredRecord]) -> None:
    """A first row that names a kind's id column, in a table whose header is undeclared."""
    first = next((row for row in rows if row.row == 0), None)
    if first is None:
        return
    for column, state in enumerate(first.cells):
        if isinstance(state, Known) and isinstance(state.value, str):
            for key, kind in KIND_KEYS:
                if field_key(state.value) == key:
                    out.candidate(
                        table.id,
                        _proposes(kind),
                        "undeclared_header_names_register",
                        UNDECLARED_HEADER_CONFIDENCE,
                        state.value,
                        first.cell_evidence(table, column),
                    )
                    return


def _proposes(kind: str) -> str:
    return {"task": "task_brief"}.get(kind, kind)


def rows_by_table(rows: Sequence[StructuredRecord]) -> dict[RecordId, list[StructuredRecord]]:
    found: dict[RecordId, list[StructuredRecord]] = {}
    for row in rows:
        found.setdefault(row.table, []).append(row)
    for listed in found.values():
        listed.sort(key=lambda r: (r.row, r.id))
    return found
