"""Catalog metadata as ``stated`` structured records, cited to the API response (ADR 0009 §4).

A hosted catalog (Roboto's dataset, file and event API; a Rerun Hub segment table) says things about
the objects it indexes: names, tags, annotations with time ranges, entity paths. That is evidence a
person or a system authored, so it is ``stated``, never ``observed`` and never inferred. This module
turns a list of JSON objects such a catalog returned into the compiler's own record kinds
(``StructuredTable`` and ``StructuredRecord``, root ADR 0020 §5) with nothing added:

- The objects are kept as one **catalog document**: ``{"items": [...]}`` in a fixed byte form
  (sorted keys, ASCII, no whitespace, items in sorted order). The document is a deterministic
  function of the objects, whatever order the pages came in. Its content id is the evidence source
  of every record, so a record's tier-2 id is derived from bytes the compiler can store (root ADR
  0003), and its ``ExternalObjectRef`` names it as an object of the connector.
- Each table cites ``/items``, each row ``/items/<i>`` and each cell ``/items/<i>/<key>``, as JSON
  pointers, with ``assertion_kind`` ``stated``.
- A cell is the value as the catalog gave it. A string is text, a number or boolean keeps its type,
  and an object or array is its own JSON as text (sorted keys). ``null``, an absent key and an empty
  string are ``Unknown``: the catalog could have said and did not. Nothing is parsed, converted or
  normalised (a time stays the integer the catalog wrote; a tag list stays a list).
- A clock a catalog names (``start_time`` of an event) becomes a ``TimestampDomain`` whose epoch,
  timescale, resolution and role are ``Unknown`` unless the operator declared them. A catalog's
  documentation saying "nanoseconds, assumed Unix epoch" is not something its responses state. A
  declared part is the operator's word, not the catalog's: it is in the transform config that the
  record's provenance names (so a different declaration is a different transform), and the record's
  evidence cites only the catalog value that names the clock.

Nothing here touches the network, a file or a clock.
"""

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from functools import cached_property
from typing import Final

from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id
from neptune.model.ids import ContentId, ExternalObjectRef
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import (
    AssertionKind,
    Knowledge,
    Known,
    Unknown,
)
from neptune.model.provenance import EvidenceRef, JsonPointer, Provenance, TransformRecord
from neptune.model.reference import TimestampDomain
from neptune.model.time import ClockRole, Epoch, Timescale
from neptune.model.world import CellValue, StructuredRecord, StructuredTable

MAX_DEPTH: Final = 64  # nesting of a parsed API response; deeper is refused, never recursed into
CLOCK_PREFIX: Final = "@clock:"  # companion columns; a catalog key of this form is not kept
MAX_CELL_BYTES: Final = 1 << 20  # one cell's text; a longer one is not stored whole


class DocumentInvalid(ValueError):
    """An API response is not the JSON this module accepts (duplicate keys, NaN, too deep)."""


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

    def number(text: str) -> float:
        value = float(text)
        if not math.isfinite(value):  # 1e999 is a number to the parser and no JSON to ``dumps``
            raise DocumentInvalid("a number is not finite")
        return value

    try:
        value: JsonValue = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=number,
        )
    except DocumentInvalid:
        raise
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError, JSONDecodeError, digit limit
        raise DocumentInvalid("not JSON") from exc
    if _too_deep(value):
        raise DocumentInvalid(f"nested deeper than {MAX_DEPTH}")
    return value


def _too_deep(value: JsonValue) -> bool:
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

    Unlike the compiler's canonical JSON it keeps ``null``: the catalog said ``null``, and the
    record is where that becomes ``Unknown``. ASCII keeps a lone surrogate a response carried
    representable.
    """
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


@dataclass(frozen=True)
class CatalogDocument:
    """What a catalog returned, as one object: ``data`` is ``{"items": [...]}``."""

    ref: ExternalObjectRef
    data: bytes

    @cached_property
    def content_id(self) -> ContentId:
        return content_id(self.data)

    @cached_property
    def raw_items(self) -> tuple[JsonValue, ...]:
        parsed = parse_json(self.data)
        assert isinstance(parsed, Mapping)
        items = parsed["items"]
        assert isinstance(items, list)
        return tuple(items)

    @cached_property
    def items(self) -> tuple[Mapping[str, JsonValue], ...]:
        """One mapping per element of ``/items``, at the same index: an element that is not an
        object is an empty mapping here, so every pointer ``/items/<i>`` names what it says."""
        return tuple(item if isinstance(item, Mapping) else {} for item in self.raw_items)


def build_document(
    connector_id: str, object_id: str, items: Sequence[JsonValue]
) -> CatalogDocument:
    """The document of ``items``: sorted by their own bytes and de-duplicated, so the document
    does not depend on the order or the paging the catalog answered in.

    Its revision token is ``records:<sha256 of the bytes>``: a catalog record changed in any way is
    a new revision of the document, and an unchanged one is the same revision.
    """
    unique = sorted({dumps(item) for item in items})
    data = b'{"items":[' + b",".join(unique) + b"]}"
    token = "records:" + hashlib.sha256(data).hexdigest()
    return CatalogDocument(ExternalObjectRef(connector_id, object_id, token), data)


def pointer(*parts: str | int) -> str:
    """An RFC 6901 pointer to ``parts``."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


@dataclass(frozen=True)
class DeclaredClock:
    """What the operator declared about a catalog's clock; every part optional, none assumed."""

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


def parse_clock(declared: "JsonValue | None") -> DeclaredClock:
    """A clock declared in options: ``{"epoch": "unix", "timescale": "posix", "resolution":
    "1/1000000000", "role": "sample"}``, each key optional. Anything else is refused."""
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
                raise ValueError("resolution is text such as '1/1000000000' (seconds per tick)")
            numerator, _, denominator = text.partition("/")
            numbers = [part for part in (numerator, denominator or "1")]
            if not all(part.isascii() and part.isdigit() and len(part) <= 30 for part in numbers):
                raise ValueError("resolution is text such as '1/1000000000' (seconds per tick)")
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


@dataclass(frozen=True)
class StatedCatalog:
    """Records over one or more documents, all by one transform."""

    documents: tuple[CatalogDocument, ...] = ()
    tables: tuple[StructuredTable, ...] = ()
    rows: tuple[StructuredRecord, ...] = ()
    domains: tuple[TimestampDomain, ...] = ()
    skipped: tuple[tuple[str, int, str], ...] = field(default=())  # (document, item, reason)


def _cell(
    document: CatalogDocument,
    transform: TransformRecord,
    row: int,
    key: str,
    value: JsonValue,
    reasons: list[str],
) -> Knowledge[CellValue]:
    where = EvidenceRef(document.content_id, (JsonPointer(pointer("items", row, key)),))
    provenance = Provenance(where, transform.id, AssertionKind.STATED)
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


def _usable_key(key: str) -> bool:
    """A key a header can hold: valid Unicode, and not the name of a companion column."""
    if key.startswith(CLOCK_PREFIX):
        return False
    try:
        key.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def stated_table(
    document: CatalogDocument,
    name: str,
    transform: TransformRecord,
    *,
    clocks: Mapping[str, TimestampDomain] | None = None,
) -> StatedCatalog:
    """One ``StructuredTable`` and a ``StructuredRecord`` per object of ``document``.

    The header is every key any object has, sorted. An object's cell is ``Unknown`` for a key it
    does not have. ``clocks`` maps a column name to the clock its integer cells count on: each such
    column gets a companion column ``@clock:<name>`` whose cell is that clock's record id where the
    object has a value, citing that value. A catalog key that starts with ``@clock:``, or is not
    valid Unicode, is not a column: it is reported as ``key_unusable`` and its value is not stored,
    so a companion is never confused with a catalog's own key. An element of the document that is
    not an object is a row of ``Unknown``, reported as ``not_an_object``. Companions are appended
    after the sorted keys.
    """
    clocks = clocks or {}
    items = document.items
    keys = sorted({key for item in items for key in item if _usable_key(key)})
    header = (*keys, *(f"{CLOCK_PREFIX}{column}" for column in sorted(clocks)))
    table_evidence = EvidenceRef(document.content_id, (JsonPointer(pointer("items")),))
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
        if not isinstance(document.raw_items[index], Mapping):
            reasons.append("not_an_object")  # a row of Unknown: the catalog said something else
        reasons.extend(sorted({"key_unusable" for key in item if not _usable_key(key)}))
        cells: list[Knowledge[CellValue]] = [
            _cell(document, transform, index, key, item[key], reasons)
            if key in item
            else Unknown(
                Provenance(
                    EvidenceRef(document.content_id, (JsonPointer(pointer("items", index)),)),
                    transform.id,
                    AssertionKind.STATED,
                )
            )
            for key in keys
        ]
        for column in sorted(clocks):
            value = item.get(column)
            where = EvidenceRef(
                document.content_id, (JsonPointer(pointer("items", index, column)),)
            )
            provenance = Provenance(where, transform.id, AssertionKind.STATED)
            if isinstance(value, int) and not isinstance(value, bool):
                cells.append(Known(clocks[column].id, provenance))
            else:
                cells.append(Unknown(provenance))
        skipped.extend((name, index, reason) for reason in sorted(set(reasons)))
        evidence = EvidenceRef(document.content_id, (JsonPointer(pointer("items", index)),))
        rows.append(
            StructuredRecord(
                id=evidence_record_id(StructuredRecord.kind, evidence, transform),
                provenance=Provenance(evidence, transform.id, AssertionKind.STATED),
                table=table.id,
                row=index,
                cells=tuple(cells),
            )
        )
    return StatedCatalog((document,), (table,), tuple(rows), (), tuple(skipped))


def clock_domain(
    document: CatalogDocument,
    field_name: str,
    scope: tuple[str, ...],
    transform: TransformRecord,
    declared: DeclaredClock,
) -> TimestampDomain | None:
    """The clock a catalog's ``field_name`` counts on, or ``None`` if no object has the field.

    It cites the first object (in document order) that has the field. Role, epoch, timescale and
    resolution are ``Unknown`` unless declared; ``declared_monotonic`` always is (events are not a
    series).
    """
    for index, item in enumerate(document.items):
        value = item.get(field_name)
        if isinstance(value, int) and not isinstance(value, bool):
            where = EvidenceRef(
                document.content_id, (JsonPointer(pointer("items", index, field_name)),)
            )
            provenance = Provenance(where, transform.id, AssertionKind.STATED)

            return TimestampDomain(
                id=evidence_record_id(TimestampDomain.kind, where, transform),
                provenance=provenance,
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
