"""Parsing the Ledger records the time-domain registry reads (ADR 0011 §1-§2).

Parsing is kept apart from the registry's policy: each parser turns one Ledger record into a typed
value or raises ``Malformed`` (or ``Unstated`` for a mapping whose validity the evidence does not
bound), and decides nothing. ``consolidate.time`` applies the policy to what these return.

Every kind is the compiler's and is read by the compiler's own strict reader:

- ``run`` (root ADR 0018 §1): ``machine``, the declared id of the machine that recorded it, and
  ``first`` / ``last``, its first and last instants (inclusive), each on the clock it is stated on.
- ``stream`` (root ADR 0018 §2): ``run`` and ``clocks``, the ``TimestampDomain`` of every clock its
  samples carry, with ``first`` / ``last`` on one of them.
- ``clock_mapping`` (root ADR 0050 §5): canonical when a source states the relation (``observed`` or
  ``stated``), or a ``derived/`` line a fit estimated (root ADR 0060 §6, ``inferred``). Both share
  the kind; a derived line says ``assertion_kind: inferred`` at its top level.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, TypeVar

from neptune.derived.clocks import inferred_clock_mapping_from_json
from neptune.derived.provenance import INFERRED
from neptune.model.alignment import ValidityWindow, clock_mapping_from_json
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import EvidenceRef, Provenance
from neptune.model.run import run_from_json, stream_from_json
from neptune.model.time import INT64_MIN, Timestamp
from neptune_memory.consolidate.identity_records import Malformed, declared
from neptune_memory.schema.clock_map import ClockMap, MapMethod
from neptune_memory.schema.interval import OPEN, Interval, Open

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from neptune.model.ids import LogicalId, RecordId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune_memory.schema.claim import ClaimAssertionKind

__all__ = ["Malformed"]

_T = TypeVar("_T")

# Ledger record kinds the time-domain registry reads.
RUN: Final = "run"
STREAM: Final = "stream"
CLOCK_MAPPING: Final = "clock_mapping"


class Unstated(ValueError):
    """A mapping whose validity the evidence does not bound: it is never assumed open (root ADR
    0060 §7), so it grounds no claim. ``record`` is the mapping's record id."""

    def __init__(self, message: str, record: RecordId | None = None) -> None:
        super().__init__(message)
        self.record = record


def _strict(parse: Callable[[JsonValue], _T], record: Mapping[str, object]) -> _T:
    """A compiler reader over one record; whatever it refuses is malformed here."""
    try:
        return parse(dict(record))  # type: ignore[arg-type]
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise Malformed(str(exc) or type(exc).__name__) from exc


def _known(knowledge: Knowledge[_T]) -> _T | None:
    return knowledge.value if isinstance(knowledge, Known) else None


def _cited(*states: object) -> list[EvidenceRef]:
    """The evidence each state cites of its own, in order; nothing for an inherited one."""
    found: list[EvidenceRef] = []
    for state in states:
        slots: list[object] = []
        if isinstance(state, Known | Unknown | NotCovered | KnownAbsent):
            slots.append(state.provenance)
        elif isinstance(state, Ambiguous):
            slots.extend(c.provenance for c in state.candidates)
        found.extend(s.evidence for s in slots if isinstance(s, Provenance))
    return found


def _kind(provenance: object) -> AssertionKind | None:
    return provenance.assertion_kind if isinstance(provenance, Provenance) else None


def weakest(kinds: tuple[AssertionKind, ...]) -> AssertionKind:
    """``stated`` when every record a claim rests on is stated; otherwise ``observed``."""
    return (
        AssertionKind.STATED
        if all(k is AssertionKind.STATED for k in kinds)
        else (AssertionKind.OBSERVED)
    )


# --- Runs and streams ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunClocks:
    """A run as the registry reads it: ``None`` where the record does not state a value as Known."""

    record: RecordId
    machine: LogicalId | None
    first: Timestamp | None
    last: Timestamp | None
    evidence: tuple[EvidenceRef, ...]  # the declaration, then the machine field's own citation
    kinds: tuple[AssertionKind, ...]  # of the declaration and of the machine field, if its own


def run(record: Mapping[str, object]) -> RunClocks:
    parsed = _strict(run_from_json, record)
    machine = _known(parsed.machine)
    own = _kind(parsed.machine.provenance) if isinstance(parsed.machine, Known) else None
    return RunClocks(
        record=parsed.id,
        machine=None if machine is None else declared(machine),
        first=_known(parsed.first),
        last=_known(parsed.last),
        evidence=(parsed.provenance.evidence, *_cited(parsed.machine)),
        kinds=(parsed.provenance.assertion_kind, *([] if own is None else [own])),
    )


@dataclass(frozen=True)
class StreamClocks:
    """A stream as the registry reads it: its run, the clocks its samples carry, its span."""

    record: RecordId
    run: RecordId
    clocks: tuple[RecordId, ...]
    first: Timestamp | None
    last: Timestamp | None
    evidence: EvidenceRef
    kind: AssertionKind


def stream(record: Mapping[str, object]) -> StreamClocks:
    parsed = _strict(stream_from_json, record)
    return StreamClocks(
        record=parsed.id,
        run=parsed.run,
        clocks=parsed.clocks,
        first=_known(parsed.first),
        last=_known(parsed.last),
        evidence=parsed.provenance.evidence,
        kind=parsed.provenance.assertion_kind,
    )


# --- Clock mappings -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Hop:
    """One mapping as Memory reads it: from ``source`` onto ``clock_map.target`` over
    ``[start, end)`` on ``source``, with its parameters exactly as the record states them."""

    record: RecordId
    source: RecordId
    assertion_kind: ClaimAssertionKind
    clock_map: ClockMap
    start: Timestamp
    end: Timestamp | Open
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]  # the mapping's record; an estimate's transform record too

    @property
    def target(self) -> RecordId:
        return self.clock_map.target

    @property
    def estimated(self) -> bool:
        return not isinstance(self.assertion_kind, AssertionKind)

    @property
    def interval(self) -> Interval:
        return Interval(self.start, self.end)


def is_estimate(record: Mapping[str, object]) -> bool:
    """A ``derived/`` line: its top level says ``assertion_kind: inferred`` (root ADR 0060 §6)."""
    return record.get("assertion_kind") == INFERRED


def _inherit(field: str, state: Knowledge[Any]) -> Knowledge[Any]:
    """The state with its citation lifted to the claim (which cites it); ``KnownAbsent`` says the
    evidence states there is no anchor, rate or bound, which no clock mapping can be read with."""
    match state:
        case Known(value=value):
            return Known(value)
        case Ambiguous(candidates=candidates):
            return Ambiguous(tuple(Candidate(c.value) for c in candidates))
        case Unknown():
            return Unknown()
        case NotCovered():
            return NotCovered()
        case NotApplicable():
            return state
    raise Malformed(f"{field} is stated absent; a mapping cannot be read without it")


def _window(validity: Knowledge[ValidityWindow]) -> tuple[Timestamp, Timestamp | Open]:
    """``[start, end)`` as stated. A side stated open (``KnownAbsent``) is open: the earliest
    instant the clock can write, or ``OPEN``. A side not stated is never assumed open."""
    window = _known(validity)
    if window is None:
        raise Unstated(f"validity is {validity.state}, not a window")
    start, end = window.start, window.end
    if isinstance(start, Known):
        lower = start.value
    elif isinstance(start, KnownAbsent):
        lower = Timestamp(INT64_MIN, window.clock)
    else:
        raise Unstated(f"the window's start is {start.state}")
    if isinstance(end, Known):
        return lower, end.value
    if isinstance(end, KnownAbsent):
        return lower, OPEN
    raise Unstated(f"the window's end is {end.state}")


def mapping(record: Mapping[str, object]) -> Hop:
    """A compiler clock mapping, declared or estimated, read by the compiler's strict reader."""
    if is_estimate(record):
        estimate = _strict(inferred_clock_mapping_from_json, record)
        kind: ClaimAssertionKind = INFERRED
        evidence: tuple[EvidenceRef, ...] = estimate.evidence
        records: tuple[RecordId, ...] = (estimate.id, estimate.transform)
        parsed: Any = estimate
    else:
        stated = _strict(clock_mapping_from_json, record)
        kind = stated.provenance.assertion_kind
        bounds = [w.value for w in (stated.validity,) if isinstance(w, Known)]
        evidence = (
            stated.provenance.evidence,
            *_cited(stated.anchor, stated.rate, stated.residual_bound, stated.validity),
            *_cited(*(state for w in bounds for state in (w.start, w.end))),
        )
        records = (stated.id,)
        parsed = stated
    try:
        start, end = _window(parsed.validity)
    except Unstated as exc:
        raise Unstated(str(exc), parsed.id) from exc
    try:
        clock_map = ClockMap(
            target=parsed.target,
            method=MapMethod(str(parsed.method)),
            anchor=_inherit("anchor", parsed.anchor),
            rate=_inherit("rate", parsed.rate),
            residual_bound=_inherit("residual_bound", parsed.residual_bound),
        )
    except (ValueError, TypeError) as exc:
        if isinstance(exc, Malformed):
            raise
        raise Malformed(str(exc)) from exc
    return Hop(
        record=parsed.id,
        source=parsed.source,
        assertion_kind=kind,
        clock_map=clock_map,
        start=start,
        end=end,
        evidence=evidence,
        records=records,
    )
