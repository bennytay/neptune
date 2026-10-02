"""An entry's values as ``CalibrationParameter``s, as declared (ADR 0055 §3).

A name is the key path from the entry, ``/``-joined with RFC 6901 escapes (``camera_matrix/data``,
``intrinsics``, ``T_cam_imu/0``). A sequence of numbers is one parameter, its numbers in source
order. A sequence that holds anything else (rows, mappings, text, a number two YAML versions read
differently) is its items, each named by position. Text is kept as written, a ``null`` is
``KnownAbsent``, a YAML alias or a value no record can hold is ``Unknown``, and where YAML 1.1 and
1.2 read a scalar differently the parameter is ``Ambiguous``. Nothing is converted, reordered or
reshaped.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.calibration._items import Item, Kind
from neptune.adapters.structured.tree import pointer_token
from neptune.model.configuration import ConfigScalar, ScalarType
from neptune.model.knowledge import (
    Ambiguous,
    Candidate,
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    Unknown,
)
from neptune.model.machine import CalibrationParameter, ParameterValue
from neptune.model.provenance import Locator, Provenance
from neptune.model.scalars import NonFinite, Real, real
from neptune.model.units import Unit

_NUMERIC: Final = (ScalarType.INT, ScalarType.FLOAT)


@dataclass
class Gaps:
    """What flattening could not hold as a number or text, for findings (by reason)."""

    unread: list[tuple[str, str]] = field(default_factory=list)  # (name, why)
    too_large: list[tuple[str, int]] = field(default_factory=list)  # (name, items)
    non_finite: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    repeated: list[str] = field(default_factory=list)
    shapes: list[tuple[str, int, int]] = field(default_factory=list)  # (name, declared, found)


def _number(scalar: ConfigScalar) -> Real | None:
    """A numeric scalar as a float, ``None`` where it is not a number or is beyond binary64."""
    value = scalar.value
    if scalar.type not in _NUMERIC or isinstance(value, bool):
        return None
    if isinstance(value, NonFinite):
        return value
    if isinstance(value, str):
        return None
    try:
        return real(float(value))
    except OverflowError:
        return None


def _value(scalar: ConfigScalar, text: str | None) -> ParameterValue | None:
    number = _number(scalar)
    if number is not None:
        return (number,)
    if scalar.type in _NUMERIC:
        return None  # an integer beyond binary64
    return text if text is not None else (scalar.value if isinstance(scalar.value, str) else None)


def single_number(item: Item) -> Real | None:
    """The number a scalar item states under every reading, else ``None``."""
    if item.kind is not Kind.SCALAR or len(item.readings) != 1:
        return None
    return _number(item.readings[0])


def numbers(item: Item) -> tuple[Real, ...] | None:
    """The numbers of a sequence of numbers, else ``None``."""
    values: list[Real] = []
    for child in item.children:
        number = single_number(child)
        if number is None:
            return None
        values.append(number)
    return tuple(values)


class Flattener:
    """Flattens the subtree of one entry, skipping named top-level keys."""

    def __init__(
        self,
        cite: Callable[[Locator], Provenance],
        max_array: int,
    ) -> None:
        self.cite = cite
        self.max_array = max_array
        self.gaps = Gaps()
        self.found: dict[str, CalibrationParameter] = {}

    def run(self, entry: Item, skip: Iterable[str] = ()) -> tuple[CalibrationParameter, ...]:
        skipped = set(skip)
        depth = len(entry.path)
        stack = [child for child in reversed(entry.children) if child.name not in skipped]
        while stack:
            item = stack.pop()
            name = self._name(item, depth)
            match item.kind:
                case Kind.MAPPING:
                    self._shape(item, name)
                    stack.extend(reversed(item.children))
                case Kind.SEQUENCE:
                    values = numbers(item) if item.count <= self.max_array else None
                    if item.count > self.max_array:
                        self._add(name, Unknown(self.cite(item.where)), item, large=item.count)
                    elif values is not None:
                        if any(isinstance(n, NonFinite) for n in values):
                            self.gaps.non_finite.append(name)
                        self._add(name, Known(values, self.cite(item.where)), item)
                    else:
                        stack.extend(reversed(item.children))
                case Kind.NULL:
                    self._add(name, KnownAbsent(self.cite(item.where)), item)
                case Kind.SCALAR:
                    self._add(name, self._scalar(item, name), item)
                case Kind.ALIAS | Kind.UNREAD:
                    if item.why == "array_too_large":
                        self._add(name, Unknown(self.cite(item.where)), item, large=item.count)
                    else:
                        self.gaps.unread.append((name, item.why or "not read"))
                        self._add(name, Unknown(self.cite(item.where)), item)
        return tuple(self.found[n] for n in sorted(self.found))

    def _name(self, item: Item, depth: int) -> str:
        segments = [pointer_token(segment) for segment in item.path[depth:]]
        if item.repeated:
            segments[-1] += f"#{item.order}"
        return "/".join(segments)

    def _scalar(self, item: Item, name: str) -> Knowledge[ParameterValue]:
        cited = self.cite(item.where)
        values: list[ParameterValue] = []
        for scalar in item.readings:
            value = _value(scalar, item.text)
            if value is None:
                self.gaps.unread.append((name, "a number beyond binary64"))
                return Unknown(cited)
            if value not in values:
                values.append(value)
        if isinstance(values[0], tuple) and any(isinstance(n, NonFinite) for n in values[0]):
            self.gaps.non_finite.append(name)
        if len(values) == 1:
            return Known(values[0], cited)
        self.gaps.ambiguous.append(name)
        return Ambiguous(tuple(Candidate(value, cited) for value in values))

    def _add(
        self,
        name: str,
        value: Knowledge[ParameterValue],
        item: Item,
        large: int = 0,
    ) -> None:
        if large:
            self.gaps.too_large.append((name, large))
        if name in self.found:  # a repeated key and a '#' key that collide: keep both
            self.gaps.repeated.append(name)
            n = 1
            while f"{name}#{n}" in self.found:
                n += 1
            name = f"{name}#{n}"
        elif item.repeated:
            self.gaps.repeated.append(name)
        self.found[name] = CalibrationParameter(name, value, _unit(value))

    def _shape(self, item: Item, name: str) -> None:
        """An OpenCV or ROS matrix (``rows``, ``cols``, ``data``): does it hold what it says?"""
        rows, cols, data = item.child("rows"), item.child("cols"), item.child("data")
        if rows is None or cols is None or data is None or data.kind is not Kind.SEQUENCE:
            return
        r, c = single_number(rows), single_number(cols)
        if not (isinstance(r, float) and isinstance(c, float)) or r != int(r) or c != int(c):
            return
        channels = 1
        dt = item.child("dt")
        if dt is not None and dt.text and dt.text[:-1].isdigit():
            channels = int(dt.text[:-1])
        declared = int(r) * int(c) * channels
        if declared != data.count:
            self.gaps.shapes.append((name or "/", declared, data.count))


def _unit(value: Knowledge[ParameterValue]) -> Knowledge[Unit]:
    """Text has no unit; no format read here declares a number's, so it is ``Unknown``."""
    match value:
        case KnownAbsent():
            return NotApplicable()
        case Known(value=str()):
            return NotApplicable()
        case Ambiguous(candidates=candidates) if any(isinstance(c.value, str) for c in candidates):
            return NotApplicable()
    return Unknown()
