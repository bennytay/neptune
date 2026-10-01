"""The Parquet column contract for time series, and how each row cites its evidence (ADR 0018).

A ``Stream`` record (``neptune.model.run``) describes one series: a Parquet file with one row per
sample (a message, a log row, a video frame). Column names fall into namespaces, so nothing a
source names can collide with Neptune's own columns:

- ``seq``: int64, never null. The sample's 0-based position among the stream's samples in source
  order.
- ``time/<i>``: int64. Ticks on the stream's clock ``i``, exactly as the source encodes them. Plain
  integers, never Parquet's TIMESTAMP type, which would assert a unit and UTC (ADR 0005).
- ``locator/<i>/<field>``: int64, string or double, never null. The fields of locator step ``i``
  that differ from row to row (``SeriesProvenance``).
- ``value/<name>``: a decoded field, exactly as the source encodes it. The adapter names these
  columns and documents how nested fields map onto them.
- ``state/<column>``: dictionary-encoded string, never null, and present only for a wrapped column
  (one whose rows are not all ``known``). It holds each row's ``KnowledgeState``; the column is
  null exactly where the state is not ``known`` (ADR 0004 §7).

Rows are sorted by their ticks on clock 0, then by ``seq``; rows whose clock 0 is not known come
last (``row_order``).

Whatever a row's provenance shares with every other row is hoisted onto the ``Stream``; the rest
is in the row's ``locator/`` columns.

``SeriesBatch`` carries rows between an adapter and the store, column by column, each column typed
as the source encodes it (ADR 0024 §5).
"""

import math
import re
import struct
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeAlias

from neptune.model._fields import exact_object, json_array, json_str
from neptune.model.ids import (
    ContentId,
    RecordId,
    check_text,
    check_token,
    parse_content_id,
    parse_record_id,
)
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import AssertionKind, KnowledgeState
from neptune.model.provenance import (
    AdapterLocator,
    EvidenceRef,
    FrameLocator,
    Locator,
    Scalar,
    locator_from_json,
    step_keys,
)
from neptune.model.time import INT64_MAX, INT64_MIN

SEQ: Final = "seq"
TIME: Final = "time"
LOCATOR: Final = "locator"
VALUE: Final = "value"
STATE: Final = "state"

# The states a wrapped column holds, row by row. KnownAbsent needs a citation of what defines the
# absence and Ambiguous needs its candidates; neither fits in one cell (ADR 0018 §6).
SERIES_STATES: Final = frozenset(
    {
        KnowledgeState.KNOWN,
        KnowledgeState.UNKNOWN,
        KnowledgeState.NOT_COVERED,
        KnowledgeState.NOT_APPLICABLE,
    }
)

# One row as a Parquet reader yields it (``Table.to_pylist()``): column name to value, None if null.
Row: TypeAlias = Mapping[str, object]


def time_column(clock: int) -> str:
    """The column holding ticks on the stream's clock ``clock``."""
    return f"{TIME}/{clock}"


def locator_column(step: int, field: str) -> str:
    """The column holding field ``field`` of locator step ``step``."""
    return f"{LOCATOR}/{step}/{field}"


def value_column(name: str) -> str:
    """The column holding the decoded field the adapter calls ``name``."""
    check_text("value name", name)
    return f"{VALUE}/{name}"


def state_column(column: str) -> str:
    """The column holding ``column``'s state, row by row."""
    return f"{STATE}/{column}"


# --- Reading rows ------------------------------------------------------------------------------


def _int(value: object, what: str, low: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer, got {value!r}")
    if not low <= value <= INT64_MAX:
        raise ValueError(f"{what} must fit in [{low}, 2^63): {value}")
    return value


def seq_of(row: Row) -> int:
    """The row's 0-based position in source order."""
    if SEQ not in row:
        raise ValueError(f"row has no {SEQ!r} column")
    return _int(row[SEQ], SEQ, 0)


def ticks_of(row: Row, column: str) -> int:
    """The ticks a known time cell holds: a signed 64-bit integer."""
    return _int(row[column], column, INT64_MIN)


def cell_state(row: Row, column: str) -> KnowledgeState:
    """``column``'s state in ``row``: its state column's value, or ``known`` if it has none.

    Enforces the null rule: the column holds a value exactly where its state is ``known``.
    """
    if column not in row:
        raise ValueError(f"row has no column {column!r}")
    tag = row.get(state_column(column), KnowledgeState.KNOWN)
    if not isinstance(tag, str) or tag not in SERIES_STATES:
        raise ValueError(
            f"{state_column(column)} must be one of {sorted(map(str, SERIES_STATES))}, got {tag!r}"
        )
    state = KnowledgeState(tag)
    if (row[column] is None) is (state is KnowledgeState.KNOWN):
        raise ValueError(
            f"{column} must hold a value exactly where its state is known;"
            f" got {row[column]!r} with state {state}"
        )
    return state


def row_order(row: Row) -> tuple[bool, int, int]:
    """The series sort key (ADR 0018 §7): clock-0 ticks, then ``seq``; unknown times sort last."""
    column = time_column(0)
    known = cell_state(row, column) is KnowledgeState.KNOWN
    return (not known, ticks_of(row, column) if known else 0, seq_of(row))


def _is_value(column: str) -> bool:
    return column.startswith(f"{VALUE}/") and len(column) > len(VALUE) + 1


def check_columns(row: Row, required: Iterable[str]) -> None:
    """Every required column is present, and every other one is a value or the state of one.

    A state column may qualify a time or value column, never ``seq``, a locator or another state.
    Value columns are checked against the null rule.
    """
    required = tuple(required)
    missing = [column for column in required if column not in row]
    if missing:
        raise ValueError(f"row is missing columns {missing}")
    for column in row:
        if column in required or _is_value(column):
            continue
        target = column.removeprefix(f"{STATE}/")
        qualifies = target.startswith(f"{TIME}/") or _is_value(target)
        if target != column and target in row and qualifies:
            continue
        raise ValueError(f"column {column!r} is outside the series contract")
    for column in row:
        if _is_value(column):
            cell_state(row, column)


# --- Row provenance (ADR 0006 §6, ADR 0018 §5) -------------------------------------------------


@dataclass(frozen=True)
class StepTemplate:
    """One locator step: the fields every row shares (``fixed``) and the ones each row supplies.

    ``per_row`` names the fields read from the row's ``locator/<step>/<field>`` columns; ``fixed``
    holds the others, sorted by name. ``kind`` is a core step kind or an adapter's
    ``<adapter id>:<name>``. A coordinate frame is not a place a sample is read from, so ``frame``
    is refused. Build one with ``step_template``.
    """

    kind: str
    fixed: tuple[tuple[str, Scalar], ...]
    per_row: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str):
            raise TypeError(f"kind must be a str, got {type(self.kind).__name__}")
        if self.kind == FrameLocator.kind:
            raise ValueError("a coordinate frame is not a place a series row is read from")
        names = [name for name, _ in self.fixed]
        if names != sorted(set(names)):
            raise ValueError(f"fixed fields must be unique and sorted by name: {names}")
        if list(self.per_row) != sorted(set(self.per_row)):
            raise ValueError(f"per-row fields must be unique and sorted: {self.per_row}")
        for name in self.per_row:
            check_token("per-row field", name)
        if "kind" in names or "kind" in self.per_row:
            raise ValueError("'kind' is the step's own kind, not a field")
        if both := set(names) & set(self.per_row):
            raise ValueError(f"a field is fixed or per row, never both: {sorted(both)}")
        for name, value in self.fixed:
            if not isinstance(value, str | int | float):
                raise TypeError(f"fixed field {name} must be a JSON scalar, got {value!r}")
        if ":" in self.kind:
            AdapterLocator(self.kind, self.fixed)  # checks the kind and the fixed fields
        elif (keys := {"kind", *names, *self.per_row}) != step_keys(self.kind, keys):
            expected = sorted(step_keys(self.kind, keys) - {"kind"})
            raise ValueError(f"a {self.kind} step has fields {expected}, got {sorted(keys)}")
        if not self.per_row:
            self.fill(0, {})  # nothing varies, so the step must already be valid

    def columns(self, step: int) -> tuple[str, ...]:
        """The ``locator/`` columns this template reads when it is step ``step``."""
        return tuple(locator_column(step, name) for name in self.per_row)

    def fill(self, step: int, row: Row) -> Locator:
        """This step for one row: ``fixed`` plus the row's values, parsed strictly."""
        data: dict[str, JsonValue] = {"kind": self.kind, **dict(self.fixed)}
        for name in self.per_row:
            column = locator_column(step, name)
            if column not in row:
                raise ValueError(f"row has no column {column!r}")
            value = row[column]
            if not isinstance(value, str | int | float):
                raise ValueError(f"{column} must hold a string or a number, got {value!r}")
            data[name] = value
        return locator_from_json(data)

    def to_json(self) -> JsonObject:
        return {"fixed": dict(self.fixed), "kind": self.kind, "per_row": list(self.per_row)}


def step_template(
    kind: str, fixed: Mapping[str, Scalar] | None = None, per_row: Iterable[str] = ()
) -> StepTemplate:
    return StepTemplate(kind, tuple(sorted((fixed or {}).items())), tuple(sorted(per_row)))


@dataclass(frozen=True)
class SeriesProvenance:
    """What the provenance of every row of one series shares, hoisted onto its ``Stream``.

    A row's evidence is ``source`` plus ``locator`` filled from the row's ``locator/`` columns, its
    transform is the stream's, and its assertion kind is ``assertion_kind``. At least one locator
    field must vary per row: every row cites its own sample.
    """

    source: ContentId
    locator: tuple[StepTemplate, ...]
    assertion_kind: AssertionKind

    def __post_init__(self) -> None:
        parse_content_id(self.source)
        if not isinstance(self.locator, tuple) or not self.locator:
            raise ValueError("locator must be a non-empty tuple of step templates, outermost first")
        for step in self.locator:
            if not isinstance(step, StepTemplate):
                raise TypeError(f"not a step template: {step!r}")
        if not any(step.per_row for step in self.locator):
            raise ValueError("no locator field varies per row, so every row would cite one place")
        if not isinstance(self.assertion_kind, AssertionKind):
            raise TypeError(
                f"assertion_kind must be observed or stated, got {self.assertion_kind!r};"
                " inferred series belong in derived/"
            )

    @property
    def columns(self) -> tuple[str, ...]:
        """The ``locator/`` columns every row carries, step by step."""
        return tuple(column for i, step in enumerate(self.locator) for column in step.columns(i))

    def evidence(self, row: Row) -> EvidenceRef:
        """The row's evidence: this series' source and the row's own locator."""
        return EvidenceRef(
            self.source, tuple(step.fill(i, row) for i, step in enumerate(self.locator))
        )

    def to_json(self) -> JsonObject:
        return {
            "assertion_kind": str(self.assertion_kind),
            "locator": [step.to_json() for step in self.locator],
            "source": self.source,
        }


def step_template_from_json(data: JsonValue) -> StepTemplate:
    obj = exact_object(data, "step template", {"fixed", "kind", "per_row"})
    fixed = obj["fixed"]
    if not isinstance(fixed, Mapping):
        raise ValueError("fixed must be a JSON object of the step's shared fields")
    fields: dict[str, Scalar] = {}
    for name, value in fixed.items():
        if not isinstance(value, str | int | float):
            raise ValueError(f"fixed field {name} must be a JSON scalar, got {value!r}")
        fields[name] = value
    return StepTemplate(
        json_str(obj["kind"], "kind"),
        tuple(sorted(fields.items())),
        tuple(json_str(name, "per-row field") for name in json_array(obj["per_row"], "per_row")),
    )


def series_provenance_from_json(data: JsonValue) -> SeriesProvenance:
    """Parse strictly; ``"inferred"`` is rejected, as on every canonical record."""
    obj = exact_object(data, "series provenance", {"assertion_kind", "locator", "source"})
    kind = json_str(obj["assertion_kind"], "assertion_kind")
    if kind not in AssertionKind.__members__.values():
        raise ValueError(f"assertion_kind must be observed or stated in model/, got {kind!r}")
    steps = json_array(obj["locator"], "locator")
    return SeriesProvenance(
        parse_content_id(json_str(obj["source"], "source")),
        tuple(step_template_from_json(step) for step in steps),
        AssertionKind(kind),
    )


# --- Batches of rows, typed (ADR 0024 §5) ------------------------------------------------------


class ColumnType(StrEnum):
    """The type of a series column's cells, exactly as the source encodes the field."""

    BOOL = "bool"
    INT8 = "int8"
    INT16 = "int16"
    INT32 = "int32"
    INT64 = "int64"
    UINT8 = "uint8"
    UINT16 = "uint16"
    UINT32 = "uint32"
    UINT64 = "uint64"
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    STRING = "string"
    BINARY = "binary"


_INTEGER_BITS: Final = {
    ColumnType.INT8: (True, 8),
    ColumnType.INT16: (True, 16),
    ColumnType.INT32: (True, 32),
    ColumnType.INT64: (True, 64),
    ColumnType.UINT8: (False, 8),
    ColumnType.UINT16: (False, 16),
    ColumnType.UINT32: (False, 32),
    ColumnType.UINT64: (False, 64),
}

ScalarCell: TypeAlias = bool | int | float | str | bytes
# One cell: a scalar, a tuple of scalars in a repeated column, or None for null.
Cell: TypeAlias = ScalarCell | tuple[ScalarCell, ...] | None

_TIME_COLUMN: Final = re.compile(rf"{TIME}/(0|[1-9][0-9]*)")
_LOCATOR_COLUMN: Final = re.compile(rf"{LOCATOR}/(0|[1-9][0-9]*)/[a-z][a-z0-9_.\-]*")


def _check_scalar(column: str, kind: ColumnType, value: object) -> None:
    if kind in _INTEGER_BITS:
        signed, bits = _INTEGER_BITS[kind]
        low, high = (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if signed else (0, 2**bits - 1)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{column}: {value!r} is not a {kind}")
    elif kind is ColumnType.BOOL:
        if not isinstance(value, bool):
            raise ValueError(f"{column}: {value!r} is not a bool")
    elif kind in (ColumnType.FLOAT32, ColumnType.FLOAT64):
        if not isinstance(value, float):
            raise ValueError(f"{column}: {value!r} is not a {kind}")
        if kind is ColumnType.FLOAT32 and math.isfinite(value):
            try:
                narrowed = struct.unpack("<f", struct.pack("<f", value))[0]
            except OverflowError:
                narrowed = None
            if narrowed != value:
                raise ValueError(f"{column}: {value!r} is not a float32 value")
    elif kind is ColumnType.STRING:
        if not isinstance(value, str):
            raise ValueError(f"{column}: {value!r} is not a string")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"{column}: string is not valid Unicode") from exc
    elif not isinstance(value, bytes):
        raise ValueError(f"{column}: {value!r} is not binary")


@dataclass(frozen=True)
class SeriesColumn:
    """One column of a series batch: its name, its cells' type, and one cell per row.

    ``repeated`` makes every non-null cell a tuple of ``type`` values (a covariance array). The
    name is in one of the series namespaces (``seq``, ``time/<i>``, ``locator/<i>/<field>``,
    ``value/<name>``, ``state/<column>``), and Neptune's own columns have fixed types: ``seq`` and
    ``time/<i>`` are int64, a locator field int64, float64 or string, a state string.
    """

    name: str
    type: ColumnType
    values: tuple[Cell, ...]
    repeated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.type, ColumnType):
            raise TypeError(f"{self.name}: type must be a ColumnType, got {self.type!r}")
        if not isinstance(self.values, tuple):
            raise TypeError(f"{self.name}: values must be a tuple")
        allowed = _fixed_types(self.name)
        if allowed is not None and (self.repeated or self.type not in allowed):
            raise ValueError(f"{self.name} is one of {sorted(map(str, allowed))}, not a list")
        for value in self.values:
            if value is None:
                continue
            if self.repeated:
                if not isinstance(value, tuple):
                    raise ValueError(f"{self.name}: a repeated cell is a tuple, got {value!r}")
                for item in value:
                    _check_scalar(self.name, self.type, item)
            else:
                _check_scalar(self.name, self.type, value)


def _fixed_types(name: str) -> frozenset[ColumnType] | None:
    """The types Neptune's own column ``name`` may have; ``None`` for a value column."""
    if name == SEQ or _TIME_COLUMN.fullmatch(name):
        return frozenset({ColumnType.INT64})
    if _LOCATOR_COLUMN.fullmatch(name):
        return frozenset({ColumnType.INT64, ColumnType.FLOAT64, ColumnType.STRING})
    if name.startswith(f"{STATE}/") and len(name) > len(STATE) + 1:
        return frozenset({ColumnType.STRING})
    if _is_value(name):
        return None
    raise ValueError(f"{name!r} is not a series column (ADR 0018)")


@dataclass(frozen=True)
class SeriesBatch:
    """Rows of one stream's series, column by column: what an adapter emits and the store writes.

    ``stream`` is the id of the ``Stream`` the rows belong to. An adapter emits one batch per
    stream per chunk (``neptune.adapters.contract.ChunkOutput``); the store writes a stream's
    batches into its Parquet file (MVL-16). ``rows`` yields them for ``Stream.check_row``.
    """

    stream: RecordId
    columns: tuple[SeriesColumn, ...]

    def __post_init__(self) -> None:
        parse_record_id(self.stream)
        if not self.columns or not all(isinstance(c, SeriesColumn) for c in self.columns):
            raise ValueError("a batch has at least one SeriesColumn")
        names = [column.name for column in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"a batch repeats a column: {names}")
        if SEQ not in names:
            raise ValueError("a batch has a seq column")
        if len({len(column.values) for column in self.columns}) != 1 or not self.length:
            raise ValueError("a batch's columns hold the same number of rows, at least one")

    @property
    def length(self) -> int:
        return len(self.columns[0].values)

    def schema(self) -> tuple[tuple[str, ColumnType, bool], ...]:
        """Each column's name, type and repetition, sorted by name."""
        return tuple(sorted((c.name, c.type, c.repeated) for c in self.columns))

    def rows(self) -> Iterator[dict[str, object]]:
        """Each row as column name to cell, as a Parquet reader yields it."""
        for index in range(self.length):
            yield {column.name: column.values[index] for column in self.columns}
