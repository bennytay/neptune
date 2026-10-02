"""Declared lists that can be blank: a list field as a ``Knowledge`` state (ADR 0061 §4).

A list a declaration states (the machines an incident involved, the serials a work order replaced)
is a fact like any other value, so a blank cell cannot become ``()`` (non-negotiable 3). A
``Listed[T]`` field is ``Knowledge[tuple[T, ...]]``:

- ``Known(())`` is a list the declaration states is empty; ``Known((a, b))`` its items.
- ``Unknown`` is a list it leaves blank, ``NotCovered`` one its format has no place for, and
  ``NotApplicable`` one that does not apply.
- ``KnownAbsent`` is refused: "declared empty" is ``Known(())``, one fact with one encoding.
  ``Ambiguous`` is refused too: an item in doubt is an ``Ambiguous`` item of a ``Known`` list,
  so a reader never finds a stated id inside a rejected reading of the whole list.

Kinds are frozen (ADR 0023 §1), so the JSON stays what the kind's first version wrote wherever it
can: a ``Known`` list that inherits the record's provenance is the bare array. Every other state
is a ``Knowledge`` object (ADR 0011) whose value is that array, and a record that holds one is
written at ``LIST_STATES_SINCE``, so an older reader refuses it by version, never by key.
"""

from collections.abc import Callable, Mapping
from typing import Annotated, Any, Final, TypeAlias, TypeVar

from neptune.model._fields import values_of
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    INHERITED,
    Ambiguous,
    Grounding,
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
    from_json,
    to_json,
)
from neptune.model.record import OLDEST_READABLE_VERSION

# The schema version from which a list field may hold a state other than an inherited Known.
LIST_STATES_SINCE: Final = 5

T = TypeVar("T")


class ListedMarker:
    """``Annotated`` metadata marking a ``Knowledge[tuple]`` field written by these rules."""

    def __repr__(self) -> str:
        return "LISTED"


LISTED: Final = ListedMarker()

Listed: TypeAlias = Annotated[Knowledge[tuple[T, ...]], LISTED]


def is_bare(state: Knowledge[tuple[Any, ...]]) -> bool:
    """Whether ``state`` is written as the bare array: ``Known`` and inheriting provenance."""
    return isinstance(state, Known) and state.provenance is INHERITED


def check_listed(
    name: str,
    state: Knowledge[tuple[Any, ...]],
    check_items: Callable[[str, tuple[Any, ...]], None],
) -> None:
    """A state, never a bare tuple, ``KnownAbsent`` or ``Ambiguous``; ``check_items`` checks
    each list."""
    if isinstance(state, KnownAbsent):
        raise ValueError(f"{name}: a list declared empty is Known(()), not KnownAbsent")
    if isinstance(state, Ambiguous):
        raise ValueError(f"{name}: a list is Known with Ambiguous items, not Ambiguous whole")
    if not isinstance(state, Known | Unknown | NotCovered | NotApplicable):
        raise TypeError(f"{name} must be a Knowledge state of a tuple, got {state!r}")
    for items in values_of(state):
        check_items(name, items)


def listed_version(state: Knowledge[tuple[Any, ...]]) -> int:
    """The lowest schema version whose readers read ``state``'s JSON."""
    return OLDEST_READABLE_VERSION if is_bare(state) else LIST_STATES_SINCE


def listed_to_json(
    state: Knowledge[tuple[T, ...]], encode: Callable[[tuple[T, ...]], JsonValue]
) -> JsonValue:
    if isinstance(state, Known) and is_bare(state):
        return encode(state.value)
    return to_json(state, encode)


def listed_from_json(
    data: JsonValue,
    decode: Callable[[JsonValue], tuple[T, ...]],
    decode_provenance: Callable[[JsonObject], Grounding],
    what: str,
) -> Knowledge[tuple[T, ...]]:
    """Read strictly: an array, or a state that is not the array written another way."""
    if isinstance(data, list | tuple):
        return Known(decode(data))
    if not isinstance(data, Mapping):
        raise ValueError(f"{what} must be an array or a knowledge state, got {data!r}")
    state = from_json(data, decode, decode_provenance)
    if is_bare(state):
        raise ValueError(
            f"{what}: a known list that inherits its provenance is written as an array"
        )
    return state
