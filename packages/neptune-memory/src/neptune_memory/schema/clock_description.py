"""The ``clock_description`` literal: what the Ledger declares about one clock (ADR 0029).

A ``clock_description`` claim's subject is a clock node (its compiler ``TimestampDomain`` record
id); its object is a ``ClockDescription``, copied from the records that declare the clock and
nothing else:

- ``field`` and ``scope``: which time field of which part of the source the ticks are read from,
  verbatim (the domain's own, root ADR 0005 §2); for a shared ``CivilClock`` (a node Memory keys
  by its definition, ``schema.interval``), ``CIVIL_CLOCK_FIELD`` and no scope;
- ``role``, ``resolution`` (exact seconds per tick), ``epoch`` and ``timescale``: the domain's
  states exactly as declared, ``Unknown`` or ``NotCovered`` where the record says so;
- ``civil_time_zone``: the zone the clock's ``civil_time_zone`` companion declares (root ADR 0061
  §1): ``Known`` or ``Ambiguous`` IANA names as written, ``Unknown`` where the source could state
  one and does not, ``NotCovered`` where its format has no place for one. A companion never holds
  ``NotApplicable`` (a civil clock always has a zone), so here ``NotApplicable`` means exactly
  that no companion names the clock: the Ledger holds no zone record for it, which says nothing
  about whether it is a wall clock. A companion exists only for a civil (wall) clock, so any other
  state is also the record that the ticks count local wall time (root ADR 0023 §2, ADR 0061 §2);
- ``zone_declared_by``: ``Known`` adapter id of the transform whose record declares the zone, as
  that transform record states it (``deploy_event_log_map``: a mapping's configuration declared
  it, root ADR 0061 §3); ``Unknown`` when that transform record is not in the Ledger or several
  adapters declare the zone; ``NotApplicable`` when no companion names the clock.

Every state inherits the claim's provenance, as a ``ClockMap``'s parameters do: the claim cites
the domain's and the companion's evidence. Nothing here converts: the ticks of a wall clock still
count from 1970-01-01T00:00:00 of that wall clock, and relating them to any other clock (or to
UTC) is a stated mapping's job, never this literal's.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final

from neptune.model.ids import check_text, check_token
from neptune.model.knowledge import (
    Ambiguous,
    Inherited,
    Known,
    NotApplicable,
    NotCovered,
    Unknown,
    from_json,
    to_json,
)
from neptune.model.reference import check_iana_zone
from neptune.model.time import ClockRole, Epoch, Timescale, resolution_from_json, resolution_to_json

if TYPE_CHECKING:
    from collections.abc import Callable

    from neptune.model.jsonvalue import JsonObject, JsonValue
    from neptune.model.knowledge import Grounding, Knowledge

_KEYS: Final = frozenset(
    {
        "civil_time_zone",
        "epoch",
        "field",
        "resolution",
        "role",
        "scope",
        "timescale",
        "zone_declared_by",
    }
)
# The ``field`` of a shared ``CivilClock``'s description (``schema.interval.CivilClock``): it is
# read from no field of any source; its timescale, epoch and tick length are its definition.
CIVIL_CLOCK_FIELD: Final = "civil clock"
# A scope names parts of one source; a deeper one is not a clock any record declares.
MAX_SCOPE_STEPS: Final = 64


def _inherits(name: str, state: object, *, refused: tuple[type, ...] = ()) -> None:
    """A state inherits the claim's provenance; a state of ``refused`` types is never one, and
    neither is ``KnownAbsent``, which always cites its own evidence."""
    allowed = (Known, Unknown, NotCovered, NotApplicable, Ambiguous)
    if not isinstance(state, allowed) or isinstance(state, refused):
        raise ValueError(f"{name} is not a state a clock description holds: {state!r}")
    slots: list[object] = []
    if isinstance(state, Known | Unknown | NotCovered):
        slots.append(state.provenance)
    elif isinstance(state, Ambiguous):
        slots.extend(c.provenance for c in state.candidates)
    if not all(isinstance(slot, Inherited) for slot in slots):
        raise ValueError(f"{name} inherits the claim's provenance (INHERITED)")


def _values(state: Knowledge[Any]) -> list[Any]:
    if isinstance(state, Known):
        return [state.value]
    if isinstance(state, Ambiguous):
        return [c.value for c in state.candidates]
    return []


@dataclass(frozen=True)
class ClockDescription:
    """One clock as the Ledger's records declare it; see the module docstring."""

    field: str
    scope: tuple[str, ...]
    role: Knowledge[ClockRole]
    resolution: Knowledge[Fraction]
    epoch: Knowledge[Epoch]
    timescale: Knowledge[Timescale]
    civil_time_zone: Knowledge[str]
    zone_declared_by: Knowledge[str]

    def __post_init__(self) -> None:
        check_text("field", self.field)
        if not isinstance(self.scope, tuple) or len(self.scope) > MAX_SCOPE_STEPS:
            raise ValueError(f"scope is a tuple of at most {MAX_SCOPE_STEPS} names")
        for part in self.scope:
            check_text("scope", part)
        for name, cls in (
            ("role", ClockRole),
            ("resolution", Fraction),
            ("epoch", Epoch),
            ("timescale", Timescale),
        ):
            state = getattr(self, name)
            _inherits(name, state)
            for value in _values(state):
                if not isinstance(value, cls):
                    raise TypeError(f"{name} holds a {cls.__name__}, got {value!r}")
        if any(value <= 0 for value in _values(self.resolution)):
            raise ValueError("resolution is positive: seconds per tick")
        zone, declared_by = self.civil_time_zone, self.zone_declared_by
        # A civil clock always has a zone; the question is only whether its source says it (root
        # ADR 0061 §1), so NotApplicable is free to mean "no companion names the clock".
        _inherits("civil_time_zone", zone)
        for name in _values(zone):
            check_iana_zone("civil_time_zone", name)
        _inherits("zone_declared_by", declared_by, refused=(NotCovered, Ambiguous))
        for adapter in _values(declared_by):
            check_token("zone_declared_by", adapter)
        if isinstance(zone, NotApplicable) != isinstance(declared_by, NotApplicable):
            raise ValueError("zone_declared_by is NotApplicable exactly when civil_time_zone is")

    @property
    def is_wall_clock(self) -> bool:
        """Whether a ``civil_time_zone`` record names the clock: only a civil clock has one."""
        return not isinstance(self.civil_time_zone, NotApplicable)

    def to_json(self) -> JsonObject:
        return {
            "civil_time_zone": to_json(self.civil_time_zone),
            "epoch": to_json(self.epoch, str),
            "field": self.field,
            "resolution": to_json(self.resolution, resolution_to_json),
            "role": to_json(self.role, str),
            "scope": list(self.scope),
            "timescale": to_json(self.timescale, str),
            "zone_declared_by": to_json(self.zone_declared_by),
        }


def _no_provenance(_: JsonObject) -> Grounding:
    raise ValueError("a clock description's states inherit the claim's provenance")


def _text(data: JsonValue, what: str) -> str:
    if not isinstance(data, str):
        raise ValueError(f"{what} must be a string, got {type(data).__name__}")
    return data


def _state(data: JsonValue, decode: Callable[[JsonValue], Any]) -> Knowledge[Any]:
    return from_json(data, decode, _no_provenance)


def _enum(cls: Callable[[str], Any], what: str) -> Callable[[JsonValue], Any]:
    return lambda data: cls(_text(data, what))


def clock_description_from_json(data: JsonValue) -> ClockDescription:
    """Parse strictly: exactly the keys ``to_json`` writes."""
    if not isinstance(data, dict) or set(data) != _KEYS:
        raise ValueError(f"a clock_description is an object with keys {sorted(_KEYS)}")
    scope = data["scope"]
    if not isinstance(scope, list):
        raise ValueError("scope must be an array of names")
    return ClockDescription(
        field=_text(data["field"], "field"),
        scope=tuple(_text(part, "scope") for part in scope),
        role=_state(data["role"], _enum(ClockRole, "role")),
        resolution=_state(data["resolution"], resolution_from_json),
        epoch=_state(data["epoch"], _enum(Epoch, "epoch")),
        timescale=_state(data["timescale"], _enum(Timescale, "timescale")),
        civil_time_zone=_state(data["civil_time_zone"], lambda z: _text(z, "civil_time_zone")),
        zone_declared_by=_state(data["zone_declared_by"], lambda a: _text(a, "zone_declared_by")),
    )
