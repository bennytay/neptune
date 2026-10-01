"""Helpers shared by records that mix structural fields with ``Knowledge``-wrapped ones.

Construction guards (a float resolution or a bare-string enum must not slip past the type hints)
and strict JSON readers (unexpected or missing keys and wrongly typed values are errors).
"""

from collections.abc import Callable, Mapping
from enum import StrEnum
from types import UnionType
from typing import Any, TypeAlias, TypeGuard, TypeVar

from neptune.model.ids import LogicalId, check_text, logical_id_from_json
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import Ambiguous, Grounding, Knowledge, Known, from_json, to_json
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


def check_type(field: str, knowledge: Knowledge[Any], kind: type | UnionType) -> None:
    """Runtime guard: every value the state asserts or offers is a ``kind``."""
    for value in values_of(knowledge):
        if not isinstance(value, kind):
            name = getattr(kind, "__name__", str(kind))
            raise ValueError(f"{field} must be a {name}, got {value!r}")


def check_text_values(field: str, knowledge: Knowledge[str]) -> None:
    """Every value the state asserts or offers is non-empty text: a blank is ``Unknown``."""
    check_type(field, knowledge, str)
    for value in values_of(knowledge):
        check_text(field, value)


def text_decoder(what: str) -> Callable[[JsonValue], str]:
    """Decoder for ``Knowledge[str]`` fields; ``check_text_values`` then refuses a blank."""

    def decode(data: JsonValue) -> str:
        return json_str(data, what)

    return decode


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


# --- Declared identifiers (ADR 0019 §2) ----------------------------------------------------------

# The tier-3 ids one declaration gives one real-world thing (a machine, a sensor, a site, an asset),
# each with its own citation: ``Known``, or ``Ambiguous`` where the evidence gives conflicting
# readings of one id. Sorted by (namespace, value), first candidate first; no id repeats.
Identifiers: TypeAlias = tuple[Knowledge[LogicalId], ...]


def _identifier_key(knowledge: Knowledge[LogicalId]) -> tuple[str, str]:
    first = values_of(knowledge)[0]
    return (first.namespace, first.value)


def check_identifiers(field: str, identifiers: Identifiers) -> None:
    """Runtime guard for ``Identifiers``: stated ids only, each once, in canonical order."""
    if not isinstance(identifiers, tuple):
        raise TypeError(f"{field} must be a tuple, got {type(identifiers).__name__}")
    seen: set[LogicalId] = set()
    for knowledge in identifiers:
        if not isinstance(knowledge, Known | Ambiguous):
            raise ValueError(
                f"{field} lists the ids the evidence states, Known or Ambiguous; got {knowledge!r}"
            )
        check_type(field, knowledge, LogicalId)
        for value in values_of(knowledge):
            if value in seen:
                raise ValueError(f"{field} repeat {value}")
            seen.add(value)
    keys = [_identifier_key(knowledge) for knowledge in identifiers]
    if keys != sorted(keys):
        raise ValueError(f"{field} must be sorted by namespace, then value: {keys}")


def identifiers_to_json(identifiers: Identifiers) -> list[JsonValue]:
    return [to_json(knowledge, LogicalId.to_json) for knowledge in identifiers]


def identifiers_from_json(
    data: JsonValue, decode_provenance: Callable[[JsonObject], Grounding]
) -> Identifiers:
    return tuple(
        from_json(item, logical_id_from_json, decode_provenance)
        for item in json_array(data, "identifiers")
    )
