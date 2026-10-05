"""Converting an instant between clocks through ``clock_map`` claims, at query time (ADR 0011 §5).

``convert`` reads the mapping claims of one snapshot (``as_of``) over any ``MemoryReader``. It finds
every route of clocks from the instant's clock to the target (simple, at most ``max_hops`` hops)
and carries the instant along each, forward along a mapping (its source to its target) or backward
(the exact inverse of an increasing affine map), through every mapping of each hop that holds at
the instant on its source clock. So a revised mapping's old parameters apply before the revision
and the new ones after. Composed (chain) claims are not walked: they cite chains of the same
direct mappings, whose arithmetic is done here.

Nothing is estimated and nothing is rounded: a reading is exact ticks of the target clock (a
``Fraction``), with the error bound the mappings state accumulated along the way
(``rate * bound + residual`` forward, ``(bound + residual) / rate`` backward), ``Unknown`` once any
hop states none. Declared mappings are tried first; estimated (inferred) ones only when the
declared ones decide nothing, and then the result says so. Routes of every length are compared.

- ``Known``: every route that arrives gives one value.
- ``Ambiguous``: routes or mappings that hold give different values; never one picked.
- ``Unknown``: nothing arrives, or some branch is undecided: a mapping that holds but states no
  anchor or rate, a reading of a middle clock (left by a conflict) that no hop carries while
  another is carried on, or more routes or readings than the limits. ``missing`` names the clocks
  reached, the target, the mappings that could not be applied, and any readings that did arrive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from itertools import pairwise
from typing import TYPE_CHECKING, Final

from neptune.model.ids import RecordId, parse_record_id
from neptune.model.knowledge import Ambiguous, Candidate, Known, Unknown
from neptune.model.time import INT64_MAX, INT64_MIN
from neptune_memory.schema.claim import Claim, TypedLiteral, is_inferred
from neptune_memory.schema.clock_map import ClockMap, MapMethod
from neptune_memory.schema.interval import Open
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CLOCK_MAP, MAPS_TO
from neptune_memory.schema.reader import check_as_of

if TYPE_CHECKING:
    from neptune.model.knowledge import Knowledge
    from neptune_memory.schema.interval import LedgerTx
    from neptune_memory.schema.reader import MemoryReader

# Deep enough for boot -> site clock -> GPS -> another site's clock -> its robot's boot clock and
# back; callers set their own.
DEFAULT_MAX_HOPS: Final = 8
# Distinct readings carried per clock, routes compared, and clocks visited finding them; past any
# of these the result is Unknown and says so (``too_ambiguous``), never one picked.
MAX_READINGS: Final = 8
MAX_ROUTES: Final = 64
MAX_VISITS: Final = 4096


@dataclass(frozen=True)
class Converted:
    """An instant as exact ticks of ``clock``, and how it was reached."""

    ticks: Fraction
    clock: RecordId
    bound: Knowledge[Fraction]  # the largest error the mappings state, in ``clock``'s ticks
    path: tuple[Claim, ...]  # the ``clock_map`` claims applied, in order
    backward: tuple[bool, ...]  # per hop: applied target to source (the inverse)
    inferred: bool  # some hop is an estimated (inferred) mapping


@dataclass(frozen=True)
class MissingHop:
    """Why no conversion is decided: no route from ``reached`` (the clocks a reading got to) to
    ``target`` holds at the instant, or some route is undecided (``readings`` then holds what the
    others give). ``outside_validity`` and ``parameters_unknown`` are the mappings on the routes
    that exist but could not be applied, sorted by claim id."""

    reached: tuple[RecordId, ...]
    target: RecordId
    outside_validity: tuple[Claim, ...]
    parameters_unknown: tuple[Claim, ...]
    too_ambiguous: bool = False  # past ``MAX_READINGS``, ``MAX_ROUTES`` or ``MAX_VISITS``
    readings: tuple[Converted, ...] = ()  # what some routes give while another is undecided


@dataclass(frozen=True)
class Conversion:
    """``result`` is ``Known``, ``Ambiguous`` or ``Unknown``; ``missing`` is set exactly when it is
    ``Unknown``."""

    result: Knowledge[Converted]
    missing: MissingHop | None


@dataclass(frozen=True)
class _Reading:
    ticks: Fraction
    bound: Fraction | None
    path: tuple[Claim, ...]
    backward: tuple[bool, ...]


@dataclass(frozen=True)
class _Step:
    claim: Claim
    clock_map: ClockMap
    to: RecordId
    backward: bool


def _clock(clock: RecordId) -> NodeRef:
    return NodeRef(NodeType.CLOCK, clock)


def _direct(claim: Claim) -> ClockMap | None:
    obj = claim.object
    if claim.predicate != CLOCK_MAP or not isinstance(obj, TypedLiteral):
        return None
    value = obj.value
    if not isinstance(value, ClockMap) or value.method is MapMethod.COMPOSED:
        return None
    return value


class _Graph:
    """The direct mapping claims around each clock, read once per clock."""

    def __init__(self, reader: MemoryReader, as_of: LedgerTx, inferred: bool) -> None:
        self._reader, self._as_of, self._inferred = reader, as_of, inferred
        self._views: dict[RecordId, tuple[tuple[Claim, ...], tuple[Claim, ...]]] = {}
        self._out: dict[RecordId, tuple[tuple[Claim, ClockMap], ...]] = {}
        self._steps: dict[RecordId, tuple[_Step, ...]] = {}

    def _view(self, clock: RecordId) -> tuple[tuple[Claim, ...], tuple[Claim, ...]]:
        """The claims about ``clock`` and pointing at it, from one ``node`` call per clock."""
        if clock not in self._views:
            view = self._reader.node(_clock(clock), self._as_of, include_inferred=self._inferred)
            if isinstance(view, Known):
                self._views[clock] = (view.value.claims, view.value.incoming)
            else:
                self._views[clock] = ((), ())
        return self._views[clock]

    def outgoing(self, clock: RecordId) -> tuple[tuple[Claim, ClockMap], ...]:
        if clock not in self._out:
            claims = self._view(clock)[0]
            found = ((c, _direct(c)) for c in claims if c.valid.domain_id == clock)
            self._out[clock] = tuple((c, m) for c, m in found if m is not None)
        return self._out[clock]

    def steps(self, clock: RecordId) -> tuple[_Step, ...]:
        if clock not in self._steps:
            forward = [_Step(c, m, m.target, False) for c, m in self.outgoing(clock)]
            sources = sorted(
                {
                    c.subject.node_id
                    for c in self._view(clock)[1]
                    if c.predicate == MAPS_TO and c.subject.node_type is NodeType.CLOCK
                }
            )
            backward = [
                _Step(c, m, RecordId(source), True)
                for source in sources
                for c, m in self.outgoing(RecordId(source))
                if m.target == clock
            ]
            steps = sorted([*forward, *backward], key=lambda s: (s.to, s.claim.id))
            self._steps[clock] = tuple(steps)
        return self._steps[clock]


def _holds(claim: Claim, ticks: Fraction) -> bool:
    """Whether the claim's valid interval, on its source clock, holds the instant ``ticks``. A
    start at ``INT64_MIN`` is a window stated open below (ADR 0011 §2): it holds any earlier
    instant a hop can carry, as the chain windows of ``consolidate.time.preimage`` assume."""
    start, end = claim.valid_from, claim.valid_to
    below = start.ticks == INT64_MIN or start.ticks <= ticks
    return below and (isinstance(end, Open) or ticks < end.ticks)


def _apply(step: _Step, reading: _Reading) -> _Reading | str:
    """The reading carried across ``step``, or why not: ``outside`` (the mapping does not hold at
    that instant) or ``unknown`` (it may hold, but states no anchor or rate to apply). A forward
    step's validity is checked first, on the instant it already has; a backward step's needs its
    parameters, so without them it may hold and is ``unknown``."""
    affine = step.clock_map.affine()
    if affine is None:
        if not step.backward and not _holds(step.claim, reading.ticks):
            return "outside"
        return "unknown"
    rate, offset = affine
    residual = step.clock_map.residual_bound
    extra = Fraction(residual.value.ticks) if isinstance(residual, Known) else None
    if step.backward:
        ticks = (reading.ticks - offset) / rate
        source_ticks = ticks
        bound = None if reading.bound is None or extra is None else (reading.bound + extra) / rate
    else:
        source_ticks = reading.ticks
        ticks = rate * reading.ticks + offset
        bound = None if reading.bound is None or extra is None else rate * reading.bound + extra
    if not _holds(step.claim, source_ticks):
        return "outside"
    return _Reading(ticks, bound, (*reading.path, step.claim), (*reading.backward, step.backward))


def _preference(reading: _Reading) -> tuple[bool, bool, Fraction, int, tuple[str, ...]]:
    """Of readings with one value, keep the best-evidenced: declared hops only, a stated bound,
    the tightest bound, the fewest hops, then the path's claim ids (so the choice is
    deterministic). It never chooses between values: those are compared, never picked."""
    inferred = any(is_inferred(c.assertion_kind) for c in reading.path)
    bound = reading.bound
    ids = tuple(c.id for c in reading.path)
    return (inferred, bound is None, bound or Fraction(0), len(ids), ids)


def _distinct(readings: list[_Reading]) -> list[_Reading]:
    """One reading per value, the best-evidenced, in value order."""
    kept: dict[Fraction, _Reading] = {}
    for reading in sorted(readings, key=_preference):
        kept.setdefault(reading.ticks, reading)
    return sorted(kept.values(), key=lambda r: r.ticks)


@dataclass
class _Outcome:
    """What every route to the target says at the instant: the readings that arrive, whether any
    branch was left undecided (a mapping in force that cannot be applied, a reading of a middle
    clock that no hop carries while another reading of it is carried on, or too many readings),
    and the mappings that could not be applied."""

    reached: set[RecordId]
    readings: list[_Reading] = field(default_factory=list)
    undecided: bool = False
    crowded: bool = False
    outside: dict[str, Claim] = field(default_factory=dict)
    unknown: dict[str, Claim] = field(default_factory=dict)


def _routes(
    graph: _Graph, source: RecordId, target: RecordId, max_hops: int
) -> list[tuple[RecordId, ...]] | None:
    """Every simple sequence of clocks from ``source`` to ``target`` that mappings join, of at
    most ``max_hops`` hops, in a fixed order; ``None`` past ``MAX_ROUTES`` (never cut short)."""
    found: list[tuple[RecordId, ...]] = []
    visits = 0

    def walk(path: tuple[RecordId, ...]) -> bool:
        nonlocal visits
        visits += 1
        if visits > MAX_VISITS:
            return False
        for there in sorted({step.to for step in graph.steps(path[-1])}):
            if there in path:
                continue
            if there == target:
                found.append((*path, there))
                if len(found) > MAX_ROUTES:
                    return False
            elif len(path) < max_hops and not walk((*path, there)):
                return False
        return True

    if max_hops == 0:
        return []
    return found if walk((source,)) else None


def _evaluate(
    graph: _Graph, route: tuple[RecordId, ...], start: _Reading, outcome: _Outcome
) -> None:
    """Carry every reading along ``route``, every mapping of each hop that holds; add what
    arrives, and whether any branch was left undecided, to ``outcome``."""
    assert (
        outcome.outside is not None and outcome.unknown is not None and outcome.reached is not None
    )
    readings = [start]
    undecided = False
    for here, there in pairwise(route):
        steps = [s for s in graph.steps(here) if s.to == there]
        carried: list[_Reading] = []
        dropped = 0
        for reading in readings:
            moved = False
            for step in steps:
                out = _apply(step, reading)
                if isinstance(out, _Reading):
                    carried.append(out)
                    moved = True
                elif out == "unknown":
                    outcome.unknown[step.claim.id] = step.claim
                    undecided = moved = True
                else:
                    outcome.outside[step.claim.id] = step.claim
            dropped += not moved
        if carried and dropped:
            undecided = True  # validity picked among readings a conflict left open: never Known
        if not carried:
            break
        outcome.reached.add(there)
        readings = _distinct(carried)
        if len(readings) > MAX_READINGS:
            outcome.crowded = undecided = True
            break
    else:
        outcome.readings.extend(readings)
    outcome.undecided = outcome.undecided or undecided


def _reach(
    graph: _Graph, start: _Reading, source: RecordId, max_hops: int, outcome: _Outcome
) -> None:
    """Every clock a reading of the instant gets to, anywhere, and the mappings it stops at: what
    an ``Unknown`` names as reached and blocked. Breadth first, each clock once."""
    assert (
        outcome.outside is not None and outcome.unknown is not None and outcome.reached is not None
    )
    frontier: dict[RecordId, list[_Reading]] = {source: [start]}
    seen = {source}
    for _ in range(max_hops):
        found: dict[RecordId, list[_Reading]] = {}
        for here in sorted(frontier):
            for step in graph.steps(here):
                if step.to in seen:
                    continue
                for reading in frontier[here]:
                    out = _apply(step, reading)
                    if isinstance(out, _Reading):
                        found.setdefault(step.to, []).append(out)
                    elif out == "unknown":
                        outcome.unknown[step.claim.id] = step.claim
                    else:
                        outcome.outside[step.claim.id] = step.claim
        if not found:
            return
        seen.update(found)
        outcome.reached.update(found)
        frontier = {clock: _distinct(rs)[:MAX_READINGS] for clock, rs in found.items()}


def _search(
    graph: _Graph, ticks: int, source: RecordId, target: RecordId, max_hops: int
) -> Conversion:
    """Every route to the target, compared: one value is ``Known``, several are ``Ambiguous``,
    and any undecided branch (or too many routes or readings) is ``Unknown``, never one picked."""
    start = _Reading(Fraction(ticks), Fraction(0), (), ())
    outcome = _Outcome({source})
    routes = _routes(graph, source, target, max_hops)
    if routes is None:
        outcome.crowded = outcome.undecided = True
        routes = []
    for route in routes:
        _evaluate(graph, route, start, outcome)
    readings = _distinct(outcome.readings)
    if readings and not outcome.undecided:
        return Conversion(_result(readings, target), None)
    _reach(graph, start, source, max_hops, outcome)
    assert (
        outcome.outside is not None and outcome.unknown is not None and outcome.reached is not None
    )
    missing = MissingHop(
        reached=tuple(sorted(outcome.reached - {target})),
        target=target,
        outside_validity=tuple(outcome.outside[k] for k in sorted(outcome.outside)),
        parameters_unknown=tuple(outcome.unknown[k] for k in sorted(outcome.unknown)),
        too_ambiguous=outcome.crowded,
        readings=tuple(_converted(r, target) for r in readings),
    )
    return Conversion(Unknown(), missing)


def _converted(reading: _Reading, clock: RecordId) -> Converted:
    return Converted(
        ticks=reading.ticks,
        clock=clock,
        bound=Unknown() if reading.bound is None else Known(reading.bound),
        path=reading.path,
        backward=reading.backward,
        inferred=any(is_inferred(c.assertion_kind) for c in reading.path),
    )


def _result(readings: list[_Reading], clock: RecordId) -> Knowledge[Converted]:
    if len(readings) == 1:
        return Known(_converted(readings[0], clock))
    return Ambiguous(tuple(Candidate(_converted(r, clock)) for r in readings))


def convert(
    reader: MemoryReader,
    ticks: int,
    from_clock: RecordId,
    to_clock: RecordId,
    as_of: LedgerTx,
    *,
    include_inferred: bool = True,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> Conversion:
    """``ticks`` of ``from_clock`` as ticks of ``to_clock``, through mappings current at ``as_of``.

    Declared mappings first; estimated ones too (``include_inferred``) only if no declared chain
    converts the instant. Raises only for a caller error: ticks outside signed 64-bit, a clock
    that is not a record id, a negative ``max_hops``, or an ``as_of`` past the reader's head.
    """
    if isinstance(ticks, bool) or not isinstance(ticks, int) or not INT64_MIN <= ticks <= INT64_MAX:
        raise ValueError(f"ticks are a signed 64-bit integer: {ticks!r}")
    if isinstance(max_hops, bool) or not isinstance(max_hops, int) or max_hops < 0:
        raise ValueError(f"max_hops must be a non-negative int: {max_hops!r}")
    source, target = parse_record_id(from_clock), parse_record_id(to_clock)
    check_as_of(as_of, reader.head)
    if source == target:
        same = _Reading(Fraction(ticks), Fraction(0), (), ())
        return Conversion(Known(_converted(same, target)), None)
    declared = _search(_Graph(reader, as_of, False), ticks, source, target, max_hops)
    if not include_inferred or not isinstance(declared.result, Unknown):
        return declared
    return _search(_Graph(reader, as_of, True), ticks, source, target, max_hops)
