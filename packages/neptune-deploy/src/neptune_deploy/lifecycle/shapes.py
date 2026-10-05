"""The shape of every field of the compiler's lifecycle kinds, read from their dataclasses.

A mapping file names fields by the model's own names, and what a field accepts (one cell of text, a
declared id, a list of ids, a part spelled out field by field) follows from its type. Reading the
types keeps the mapper in step with the model (root ADR 0051): a field added to a kind at a later
schema is a new shape here, or a test failure, never a silent gap.
"""

import dataclasses
import typing
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from neptune.model.ids import LogicalId
from neptune.model.knowledge import Known
from neptune.model.lifecycle import LIFECYCLE_KINDS, Score
from neptune.model.time import Timestamp
from neptune.model.units import Unit
from neptune.model.versions import DeclaredVersion

KINDS: Final[dict[str, type[Any]]] = {cls.kind: cls for cls in LIFECYCLE_KINDS}
_ENVELOPE: Final = frozenset({"id", "provenance"})


class Shape(StrEnum):
    TEXT = "text"  # Knowledge[str]: one cell, verbatim
    ID = "id"  # Knowledge[LogicalId]: one cell, under a declared namespace
    TIME = "time"  # Knowledge[Timestamp]: one cell, read by a declared format and zone
    VERSION = "version"  # Knowledge[VersionPrimitive]: one cell, under a declared scheme
    NUMBER = "number"  # Knowledge[Real]: one cell holding a number
    UNIT = "unit"  # Knowledge[Unit]: one cell holding a unit's text
    IDS = "ids"  # Identifiers: cells, each optionally split, under declared namespaces
    STATEMENTS = "statements"  # Statements: cells, each optionally split, in declared order
    LABEL = "label"  # str: a score's name, the column's own label
    PART = "part"  # one part, spelled out field by field
    ITEMS = "items"  # a tuple of parts, each spelled out field by field


@dataclass(frozen=True)
class FieldShape:
    name: str
    shape: Shape
    part: type[Any] | None = None  # for PART and ITEMS


def _known_of(tp: Any) -> Any:
    """``T`` of ``Knowledge[T]``, or ``None`` when ``tp`` is not a ``Knowledge`` union."""
    for arg in typing.get_args(tp):
        if typing.get_origin(arg) is Known:
            return typing.get_args(arg)[0]
    return None


def _is_part(tp: Any) -> bool:
    return isinstance(tp, type) and dataclasses.is_dataclass(tp) and hasattr(tp, "_WHAT")


def _shape(name: str, tp: Any) -> FieldShape:
    listed = _known_of(tp)
    if typing.get_origin(listed) is tuple:
        tp = listed  # a list field is a state of a tuple (root ADR 0061 §4); map the tuple
    if tp is str:
        return FieldShape(name, Shape.LABEL)
    if _is_part(tp):
        return FieldShape(name, Shape.PART, tp)
    if typing.get_origin(tp) is tuple:
        item = typing.get_args(tp)[0]
        if _is_part(item):
            return FieldShape(name, Shape.ITEMS, item)
        inner = _known_of(item)
        if inner is LogicalId:
            return FieldShape(name, Shape.IDS)
        if inner is str:
            return FieldShape(name, Shape.STATEMENTS)
    inner = _known_of(tp)
    simple = {str: Shape.TEXT, LogicalId: Shape.ID, Timestamp: Shape.TIME, Unit: Shape.UNIT}
    if inner in simple:
        return FieldShape(name, simple[inner])
    members = set(typing.get_args(inner)) if inner is not None else set()
    if float in members:
        return FieldShape(name, Shape.NUMBER)
    if DeclaredVersion in members:
        return FieldShape(name, Shape.VERSION)
    raise TypeError(f"{name}: no mapping shape for {tp!r}")


_FIELDS: Final[dict[type[Any], tuple[FieldShape, ...]]] = {}


def fields_of(cls: type[Any]) -> tuple[FieldShape, ...]:
    """Every mappable field of a lifecycle kind or part, in declaration order."""
    if cls not in _FIELDS:
        hints = typing.get_type_hints(cls)
        _FIELDS[cls] = tuple(
            _shape(f.name, hints[f.name])
            for f in dataclasses.fields(cls)
            if f.name not in _ENVELOPE
        )
    return _FIELDS[cls]


def is_score(cls: type[Any]) -> bool:
    return cls is Score
