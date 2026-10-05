"""The ``clock_map`` literal: what one clock mapping says, as a claim object (ADR 0011 §2).

A ``clock_map`` claim's subject is the source clock; its object is a ``ClockMap``. A direct map
carries the compiler ``ClockMapping``'s parameters exactly as stated (root ADR 0050 §5):
``target(t) = anchor.target.ticks + rate * (t - anchor.source.ticks)``, ``residual_bound`` a
non-negative duration on the target clock. Each is a ``Knowledge`` state that inherits the claim's
provenance, as a quantity's unit does: ``Unknown`` where the record says the evidence could state it
and does not. A *composed* map is a chain of direct maps (``chain``: their records, hop by hop;
``via``: the clocks between, in order); it carries no parameters of its own (``NotApplicable``),
because anything it could carry is arithmetic over the cited hops, which ``schema.clocks.convert``
does at query time. Nothing here estimates.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final

from neptune.model.alignment import ClockAnchor, clock_anchor_from_json
from neptune.model.ids import RecordId, parse_record_id
from neptune.model.knowledge import (
    Ambiguous,
    Inherited,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
    from_json,
    to_json,
)
from neptune.model.time import (
    Duration,
    duration_from_json,
    resolution_from_json,
    resolution_to_json,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from neptune.model.jsonvalue import JsonObject, JsonValue
    from neptune.model.knowledge import Grounding, Knowledge

_PARAMETERS: Final = ("anchor", "rate", "residual_bound")
_KEYS: Final = frozenset({"anchor", "chain", "method", "rate", "residual_bound", "target", "via"})


class MapMethod(StrEnum):
    """How the map relates its clocks: the compiler's two methods, or a chain of maps."""

    STATED = "stated"  # the source states the relation (root ADR 0050 §5)
    CO_SAMPLED = "co_sampled"  # two readings of one sample (root ADR 0050 §5, ADR 0060 §9)
    COMPOSED = "composed"  # a chain of stated or co-sampled maps, composed by Memory


def _inherits(field: str, state: object) -> None:
    """A parameter's state inherits the claim's provenance; ``KnownAbsent`` is never one."""
    if isinstance(state, KnownAbsent) or not isinstance(
        state, Known | Unknown | NotCovered | NotApplicable | Ambiguous
    ):
        raise ValueError(
            f"{field} is Known, Unknown, NotCovered, NotApplicable or Ambiguous: {state!r}"
        )
    slots: list[object] = []
    if isinstance(state, Known | Unknown | NotCovered):
        slots.append(state.provenance)
    elif isinstance(state, Ambiguous):
        slots.extend(c.provenance for c in state.candidates)
    if not all(isinstance(slot, Inherited) for slot in slots):
        raise ValueError(f"{field} inherits the claim's provenance (INHERITED)")


def _values(state: Knowledge[Any]) -> list[Any]:
    if isinstance(state, Known):
        return [state.value]
    if isinstance(state, Ambiguous):
        return [c.value for c in state.candidates]
    return []


@dataclass(frozen=True)
class ClockMap:
    """One mapping from the claim's subject clock onto ``target``, as its evidence states it."""

    target: RecordId
    method: MapMethod
    anchor: Knowledge[ClockAnchor]
    rate: Knowledge[Fraction]
    residual_bound: Knowledge[Duration]
    chain: tuple[RecordId, ...] = ()
    via: tuple[RecordId, ...] = ()

    def __post_init__(self) -> None:
        parse_record_id(self.target)
        if not isinstance(self.method, MapMethod):
            raise TypeError(f"method must be a MapMethod, got {self.method!r}")
        for name in _PARAMETERS:
            _inherits(name, getattr(self, name))
        for anchor in _values(self.anchor):
            if not isinstance(anchor, ClockAnchor):
                raise ValueError(f"anchor must be a ClockAnchor, got {anchor!r}")
            if anchor.target.domain_id != self.target:
                raise ValueError("an anchor's target instant is on the target clock")
        for rate in _values(self.rate):
            if not isinstance(rate, Fraction) or rate <= 0:
                raise ValueError(
                    f"rate is a positive Fraction: a clock map increases, got {rate!r}"
                )
        for bound in _values(self.residual_bound):
            if not isinstance(bound, Duration) or bound.domain_id != self.target or bound.ticks < 0:
                raise ValueError("residual_bound is a non-negative duration on the target clock")
        for name in ("chain", "via"):
            ids = getattr(self, name)
            if not isinstance(ids, tuple):
                raise TypeError(f"{name} must be a tuple of record ids")
            for rid in ids:
                parse_record_id(rid)
        if self.method is MapMethod.COMPOSED:
            self._check_composed()
        elif self.chain or self.via:
            raise ValueError("only a composed map has a chain and clocks between")

    def _check_composed(self) -> None:
        if not all(isinstance(getattr(self, n), NotApplicable) for n in _PARAMETERS):
            raise ValueError("a composed map's parameters are its hops': NotApplicable here")
        if not self.via or len(self.chain) != len(self.via) + 1:
            raise ValueError("a composed map has at least two hops and one clock between each")
        if len(set(self.via)) != len(self.via) or self.target in self.via:
            raise ValueError("a composed map's chain never visits a clock twice")
        if len(set(self.chain)) != len(self.chain):
            raise ValueError("a composed map's chain never uses one mapping twice")

    def affine(self) -> tuple[Fraction, Fraction] | None:
        """``(rate, offset)`` with ``target(t) = rate * t + offset``, exactly, when both the
        anchor and the rate are ``Known``; otherwise ``None``. A composed map has none."""
        if not isinstance(self.anchor, Known) or not isinstance(self.rate, Known):
            return None
        anchor, rate = self.anchor.value, self.rate.value
        return rate, Fraction(anchor.target.ticks) - rate * anchor.source.ticks

    def to_json(self) -> JsonObject:
        return {
            "anchor": to_json(self.anchor, ClockAnchor.to_json),
            "chain": list(self.chain),
            "method": str(self.method),
            "rate": to_json(self.rate, resolution_to_json),
            "residual_bound": to_json(self.residual_bound, Duration.to_json),
            "target": self.target,
            "via": list(self.via),
        }


def _no_provenance(_: JsonObject) -> Grounding:
    raise ValueError("a clock_map parameter inherits the claim's provenance")


def _ids(data: JsonValue, what: str) -> tuple[RecordId, ...]:
    if not isinstance(data, list):
        raise ValueError(f"{what} must be an array of record ids")
    return tuple(parse_record_id(_text(item, what)) for item in data)


def _text(data: JsonValue, what: str) -> str:
    if not isinstance(data, str):
        raise ValueError(f"{what} must be a string, got {type(data).__name__}")
    return data


def _state(data: JsonValue, decode: Callable[[JsonValue], Any]) -> Knowledge[Any]:
    return from_json(data, decode, _no_provenance)


def clock_map_from_json(data: JsonValue) -> ClockMap:
    """Parse strictly: exactly the keys ``to_json`` writes."""
    if not isinstance(data, dict) or set(data) != _KEYS:
        raise ValueError(f"a clock_map is an object with keys {sorted(_KEYS)}")
    return ClockMap(
        target=parse_record_id(_text(data["target"], "target")),
        method=MapMethod(_text(data["method"], "method")),
        anchor=_state(data["anchor"], clock_anchor_from_json),
        rate=_state(data["rate"], resolution_from_json),
        residual_bound=_state(data["residual_bound"], duration_from_json),
        chain=_ids(data["chain"], "chain"),
        via=_ids(data["via"], "via"),
    )
