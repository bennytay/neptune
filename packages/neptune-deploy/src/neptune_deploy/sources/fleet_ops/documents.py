"""Fleet-ops metadata as ``stated`` structured records, cited to its document (ADR 0010 §3).

A fleet-operations system (Formant's API, an Open-RMF task log or fleet-state database) says things
about a deployment: a device's name, an event, an intervention, a task and its robot. That is
evidence a person or a system authored, so it is ``stated``, never ``observed`` and never inferred
(root ADR 0051). This module turns a list of JSON objects such a system returned into the
compiler's own record kinds (``StructuredTable`` and ``StructuredRecord``, root ADR 0020 §5) with
nothing added:

- The objects are kept as one **document**: ``{"items": [...]}`` in a fixed byte form (sorted keys,
  ASCII, no whitespace, items in sorted order, duplicates removed). The document is a function of
  the objects, whatever order or paging they arrived in. Its content id is the evidence source of
  every record, so a record's tier-2 id is derived from bytes the compiler can store (root ADR
  0003).
- Each table cites ``/items``, each row ``/items/<i>`` and each cell ``/items/<i>/<key>``, as JSON
  pointers, with ``assertion_kind`` ``stated``.
- A cell is the value as the system gave it: a string is text, a number or boolean keeps its type,
  an object or array is its own JSON as text (sorted keys). ``null``, an absent key and an empty
  string are ``Unknown``. Nothing is parsed, converted or normalised.
- A clock a system names becomes a ``TimestampDomain`` whose epoch, timescale, resolution and role
  are ``Unknown`` unless the operator declared them.

The shape is the one the Roboto and Rerun connectors use (MVL-155, ``sources/stated_records.py``);
it is kept here, under another name, so the two branches do not collide. Unifying them is a
follow-up once both are on ``main`` (ADR 0010, consequences).

Nothing here touches the network, a file or a clock.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from functools import cached_property
from typing import Any, Final

from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id
from neptune.model.ids import ContentId, ExternalObjectRef
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind, Knowledge, Known, Unknown
from neptune.model.provenance import EvidenceRef, JsonPointer, Provenance, TransformRecord
from neptune.model.reference import TimestampDomain
from neptune.model.time import ClockRole, Epoch, Timescale
from neptune.model.world import CellValue, StructuredRecord, StructuredTable

MAX_DEPTH: Final = 64  # nesting of a parsed response; deeper is refused, never recursed into
MAX_CELL_BYTES: Final = 1 << 20  # one cell's text; a longer one is not stored whole


class DocumentInvalid(ValueError):
    """A response or file is not the JSON this module accepts (duplicate keys, NaN, too deep)."""


def parse_json(data: bytes) -> JsonValue:
    """Strict JSON: UTF-8, no duplicate keys, no ``NaN`` or ``Infinity``, bounded nesting.

    Anything else is ``DocumentInvalid``. A duplicate key would let one reader see the first value
    and another the last, so it is refused outright.
    """

    def pairs(items: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        found: dict[str, JsonValue] = {}
        for name, value in items:
            if name in found:
                raise DocumentInvalid("an object repeats a key")
            found[name] = value
        return found

    def constant(token: str) -> JsonValue:
        raise DocumentInvalid(f"{token} is not JSON")

    try:
        value: JsonValue = json.loads(
            data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
    except DocumentInvalid:
        raise
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError, JSONDecodeError, digit limit
        raise DocumentInvalid("not JSON") from exc
    if too_deep(value):
        raise DocumentInvalid(f"nested deeper than {MAX_DEPTH}")
    return value


def too_deep(value: JsonValue) -> bool:
    """Iterative: a hostile response cannot exhaust the stack here."""
    stack: list[tuple[JsonValue, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, Mapping):
            children: Sequence[JsonValue] = list(item.values())
        elif isinstance(item, list | tuple):
            children = item
        else:
            continue
        if depth >= MAX_DEPTH:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def dumps(value: JsonValue) -> bytes:
    """The fixed byte form of a JSON value: sorted keys, ASCII, no whitespace.

    It keeps ``null``: the system said ``null``, and the record is where that becomes ``Unknown``.
    ASCII keeps a lone surrogate a response carried representable.
    """
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


@dataclass(frozen=True)
class Document:
    """What a system returned, as one object: ``data`` is ``{"items": [...]}``."""

    ref: ExternalObjectRef
    data: bytes

    @cached_property
    def content_id(self) -> ContentId:
        return content_id(self.data)

    @cached_property
    def items(self) -> tuple[Mapping[str, JsonValue], ...]:
        parsed = parse_json(self.data)
        assert isinstance(parsed, Mapping)
        items = parsed["items"]
        assert isinstance(items, list)
        return tuple(item for item in items if isinstance(item, Mapping))

    @cached_property
    def item_count(self) -> int:
        return len(self.items)


def build_document(connector_id: str, object_id: str, items: Sequence[JsonValue]) -> Document:
    """The document of ``items``: sorted by their own bytes and de-duplicated, so it does not
    depend on the order or the paging the system answered in. Only objects are items.

    Its revision token is ``records:<sha256 of the bytes>``: a record changed in any way is a new
    revision of the document, and an unchanged one is the same revision.
    """
    unique = sorted({dumps(item) for item in items if isinstance(item, Mapping)})
    data = b'{"items":[' + b",".join(unique) + b"]}"
    token = "records:" + hashlib.sha256(data).hexdigest()
    return Document(ExternalObjectRef(connector_id, object_id, token), data)


def pointer(*parts: str | int) -> str:
    """An RFC 6901 pointer to ``parts``."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def cite(document: Document, *parts: str | int) -> EvidenceRef:
    """The place in ``document`` at ``parts``."""
    return EvidenceRef(document.content_id, (JsonPointer(pointer(*parts)),))


def stated(document: Document, transform: TransformRecord, *parts: str | int) -> Provenance:
    """Provenance of a value the document states at ``parts``."""
    return Provenance(cite(document, *parts), transform.id, AssertionKind.STATED)


@dataclass(frozen=True)
class DeclaredClock:
    """What the operator declared about a system's clock; every part optional, none assumed."""

    role: ClockRole | None = None
    epoch: Epoch | None = None
    timescale: Timescale | None = None
    resolution: Fraction | None = None  # seconds per tick

    def config(self) -> dict[str, JsonValue]:
        """For the transform record: the declaration is part of what produced the records."""
        found: dict[str, JsonValue] = {}
        if self.role is not None:
            found["role"] = str(self.role)
        if self.epoch is not None:
            found["epoch"] = str(self.epoch)
        if self.timescale is not None:
            found["timescale"] = str(self.timescale)
        if self.resolution is not None:
            found["resolution"] = f"{self.resolution.numerator}/{self.resolution.denominator}"
        return found


def parse_clock(declared: JsonValue | None) -> DeclaredClock:
    """A clock declared in options: ``{"epoch": "unix", "timescale": "posix", "resolution":
    "1/1000", "role": "sample"}``, each key optional. Anything else is refused."""
    if declared is None:
        return DeclaredClock()
    if not isinstance(declared, Mapping):
        raise ValueError("a clock is an object of role, epoch, timescale and resolution")
    unknown = sorted(set(declared) - {"role", "epoch", "timescale", "resolution"})
    if unknown:
        raise ValueError(f"unknown clock fields: {unknown}")
    try:
        resolution = None
        if "resolution" in declared:
            text = declared["resolution"]
            if not isinstance(text, str):
                raise ValueError("resolution is text such as '1/1000' (seconds per tick)")
            numerator, _, denominator = text.partition("/")
            numbers = [numerator, denominator or "1"]
            if not all(part.isascii() and part.isdigit() and len(part) <= 30 for part in numbers):
                raise ValueError("resolution is text such as '1/1000' (seconds per tick)")
            resolution = Fraction(int(numerator), int(denominator or "1"))
            if resolution <= 0:
                raise ValueError("resolution is positive")
        return DeclaredClock(
            role=ClockRole(str(declared["role"])) if "role" in declared else None,
            epoch=Epoch(str(declared["epoch"])) if "epoch" in declared else None,
            timescale=Timescale(str(declared["timescale"])) if "timescale" in declared else None,
            resolution=resolution,
        )
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise ValueError(f"not a valid clock declaration: {exc}") from exc


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def clock_domain(
    document: Document,
    field_name: str,
    scope: tuple[str, ...],
    transform: TransformRecord,
    declared: DeclaredClock,
) -> TimestampDomain | None:
    """The clock a document's integer ``field_name`` counts on, or ``None`` if no item has one.

    It cites the first item (in document order) that has the field. Role, epoch, timescale and
    resolution are ``Unknown`` unless declared; ``declared_monotonic`` always is.
    """
    for index, item in enumerate(document.items):
        if _is_int(item.get(field_name)):
            where = cite(document, "items", index, field_name)
            return TimestampDomain(
                id=evidence_record_id(TimestampDomain.kind, where, transform),
                provenance=Provenance(where, transform.id, AssertionKind.STATED),
                field=field_name,
                scope=scope,
                role=Unknown() if declared.role is None else Known(declared.role),
                resolution=(
                    Unknown() if declared.resolution is None else Known(declared.resolution)
                ),
                epoch=Unknown() if declared.epoch is None else Known(declared.epoch),
                timescale=Unknown() if declared.timescale is None else Known(declared.timescale),
                declared_monotonic=Unknown(),
            )
    return None


@dataclass(frozen=True)
class StatedTable:
    """One table over one document, and the cells it could not store."""

    table: StructuredTable
    rows: tuple[StructuredRecord, ...]
    skipped: tuple[tuple[str, int, str], ...] = field(default=())  # (table name, item, reason)


def _cell(
    document: Document,
    transform: TransformRecord,
    row: int,
    key: str,
    value: JsonValue,
    reasons: list[str],
) -> Knowledge[CellValue]:
    provenance = stated(document, transform, "items", row, key)
    if value is None or value == "":
        return Unknown(provenance)
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool | int | float):
        return Known(value, provenance)
    else:
        text = dumps(value).decode("ascii")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        reasons.append("lone_surrogate")
        return Unknown(provenance)
    if len(text.encode("utf-8")) > MAX_CELL_BYTES:
        reasons.append("cell_too_large")
        return Unknown(provenance)
    return Known(text, provenance)


def stated_table(
    document: Document,
    name: str,
    transform: TransformRecord,
    *,
    clocks: Mapping[str, TimestampDomain] | None = None,
) -> StatedTable:
    """One ``StructuredTable`` and a ``StructuredRecord`` per object of ``document``.

    The header is every key any object has, sorted. An object's cell is ``Unknown`` for a key it
    does not have. ``clocks`` maps a column name to the clock its integer cells count on: each such
    column gets a companion column ``@clock:<name>`` whose cell is that clock's record id where the
    object has a value, citing that value, so a time says which named clock it is on.
    """
    clocks = clocks or {}
    items = document.items
    keys = sorted({key for item in items for key in item})
    header = (*keys, *(f"@clock:{column}" for column in sorted(clocks)))
    table_evidence = cite(document, "items")
    table_provenance = Provenance(table_evidence, transform.id, AssertionKind.STATED)
    table = StructuredTable(
        id=evidence_record_id(StructuredTable.kind, table_evidence, transform),
        provenance=table_provenance,
        name=Known(name, table_provenance),
        header=Known(header, table_provenance) if header else Unknown(table_provenance),
    )
    rows: list[StructuredRecord] = []
    skipped: list[tuple[str, int, str]] = []
    for index, item in enumerate(items):
        reasons: list[str] = []
        cells: list[Knowledge[CellValue]] = [
            _cell(document, transform, index, key, item[key], reasons)
            if key in item
            else Unknown(stated(document, transform, "items", index))
            for key in keys
        ]
        for column in sorted(clocks):
            provenance = stated(document, transform, "items", index, column)
            if _is_int(item.get(column)):
                cells.append(Known(clocks[column].id, provenance))
            else:
                cells.append(Unknown(provenance))
        skipped.extend((name, index, reason) for reason in sorted(set(reasons)))
        evidence = cite(document, "items", index)
        rows.append(
            StructuredRecord(
                id=evidence_record_id(StructuredRecord.kind, evidence, transform),
                provenance=Provenance(evidence, transform.id, AssertionKind.STATED),
                table=table.id,
                row=index,
                cells=tuple(cells),
            )
        )
    return StatedTable(table, tuple(rows), tuple(skipped))


@dataclass(frozen=True)
class Catalog:
    """What a fleet-ops source states: its documents and the evidence records built over them.

    ``records`` is every record, in one deterministic order: for each document in order, its
    clocks, table, rows, then the records built from it (runs, interventions, frames, maps).
    """

    documents: tuple[Document, ...] = ()
    records: tuple[Any, ...] = ()

    def of(self, kind: str) -> tuple[Any, ...]:
        return tuple(record for record in self.records if record.kind == kind)
