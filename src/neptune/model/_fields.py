"""Helpers shared by records that mix structural fields with ``Knowledge``-wrapped ones.

Construction guards (a float resolution or a bare-string enum must not slip past the type hints)
and strict JSON readers (unexpected or missing keys and wrongly typed values are errors).
"""

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, TypeGuard, TypeVar

from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import Ambiguous, Knowledge, Known
from neptune.model.units import Dimension, Unit

T = TypeVar("T")
E = TypeVar("E", bound=StrEnum)


def values_of(knowledge: Knowledge[T]) -> list[T]:
    """Every value a state asserts or offers: Known's value, each Ambiguous candidate's."""
    match knowledge:
        case Known(value=value):
            return [value]
        case Ambiguous(candidates=candidates):
            return [candidate.value for candidate in candidates]
        case _:
            return []


def check_type(field: str, knowledge: Knowledge[Any], kind: type) -> None:
    """Runtime guard: every value the state asserts or offers is a ``kind``."""
    for value in values_of(knowledge):
        if not isinstance(value, kind):
            raise ValueError(f"{field} must be a {kind.__name__}, got {value!r}")


def check_unit(field: str, unit: Knowledge[Unit], dimension: Dimension) -> None:
    """Runtime guard: every unit the state asserts or offers is a ``Unit`` of ``dimension``."""
    check_type(field, unit, Unit)
    for value in values_of(unit):
        if value.dimension != dimension:
            raise ValueError(f"{field} {value.symbol!r} does not have dimension {dimension}")


def unit_json(unit: Unit) -> JsonValue:
    """Encoder for ``Knowledge[Unit]`` fields: ``to_json(knowledge, unit_json)``."""
    return unit.to_json()


def enum_decoder(enum: type[E]) -> Callable[[JsonValue], E]:
    def decode(data: JsonValue) -> E:
        return enum(json_str(data, enum.__name__))

    return decode


def exact_object(data: JsonValue, what: str, keys: set[str]) -> Mapping[str, JsonValue]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{what} must be a JSON object, got {type(data).__name__}")
    if data.keys() != keys:
        missing, extra = keys - data.keys(), data.keys() - keys
        raise ValueError(f"bad {what}: missing {sorted(missing)}, unexpected {sorted(extra)}")
    return data


def is_int(value: JsonValue) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def json_str(value: JsonValue, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string, got {type(value).__name__}")
    return value


def json_int(value: JsonValue, what: str) -> int:
    if not is_int(value):
        raise ValueError(f"{what} must be an integer, got {value!r}")
    return value


def json_array(value: JsonValue, what: str) -> tuple[JsonValue, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{what} must be an array, got {type(value).__name__}")
    return tuple(value)


def json_bool(value: JsonValue) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"expected a boolean, got {type(value).__name__}")
    return value
