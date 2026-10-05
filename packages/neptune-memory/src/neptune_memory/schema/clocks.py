"""Converting an instant between clocks through ``clock_map`` claims, at query time (ADR 0011 §3).

``convert`` walks the mapping claims of one snapshot (``as_of``) over any ``MemoryReader``, breadth
first from the instant's clock: forward along a mapping (its source to its target) or backward
(target to source, the exact inverse of an increasing affine map). A mapping applies only where
its claim's valid interval holds the instant on its source clock, so a revised mapping's old
parameters are used before the revision and the new ones after. Composed (chain) claims are not
walked: they cite chains of the same direct mappings, whose arithmetic is done here.

Nothing is estimated and nothing is rounded: the result is exact ticks of the target clock (a
``Fraction``), with the error bound the mappings state accumulated along the way
(``rate * bound + residual`` forward, ``(bound + residual) / rate`` backward), ``Unknown`` once any
hop states none. Declared mappings are tried first; estimated (inferred) ones only when no
declared chain converts the instant, and then the result says so.

- ``Known``: one reading. ``Ambiguous``: mappings that hold at that instant disagree (two
  declarations from one instant, or two chains of one length); never one picked.
- ``Unknown``: no chain converts the instant; ``missing`` names the hop that is missing: the
  clocks that were reached, the clock that was not, and the mappings that exist but do not apply
  (outside their validity, or stating no anchor or rate).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
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
# Distinct readings carried per clock; more than this is reported, never silently cut.
MAX_READINGS: Final = 8


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
    """Why no chain converts the instant: from any of ``reached`` to ``target`` there is no
    mapping that holds at the instant. ``outside_validity`` and ``parameters_unknown`` are the
    mappings touching ``reached`` that exist but could not be applied, sorted by claim id."""

    reached: tuple[RecordId, ...]
    target: RecordId
    outside_validity: tuple[Claim, ...]
    parameters_unknown: tuple[Claim, ...]
    too_ambiguous: bool = False  # more than ``MAX_READINGS`` readings of one clock


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
    """The reading carried across ``step``, or why it cannot be: ``unknown`` or ``outside``."""
    affine = step.clock_map.affine()
    if affine is None:
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


def _preference(reading: _Reading) -> tuple[bool, bool, Fraction, tuple[str, ...]]:
    """Of readings with one value, keep the best-evidenced: declared hops only, a stated bound,
    the tightest bound, then the path's claim ids (so the choice is deterministic)."""
    inferred = any(is_inferred(c.assertion_kind) for c in reading.path)
    bound = reading.bound
    return (inferred, bound is None, bound or Fraction(0), tuple(c.id for c in reading.path))


def _search(
    graph: _Graph, ticks: int, source: RecordId, target: RecordId, max_hops: int
) -> Conversion:
    frontier: dict[RecordId, list[_Reading]] = {
        source: [_Reading(Fraction(ticks), Fraction(0), (), ())]
    }
    reached: set[RecordId] = {source}
    outside: dict[str, Claim] = {}
    unknown: dict[str, Claim] = {}
    crowded = False
    for _ in range(max_hops):
        found: dict[RecordId, list[_Reading]] = {}
        for here in sorted(frontier):
            for step in graph.steps(here):
                if step.to in reached:
                    continue
                for reading in frontier[here]:
                    carried = _apply(step, reading)
                    if carried == "unknown":
                        unknown[step.claim.id] = step.claim
                    elif carried == "outside":
                        outside[step.claim.id] = step.claim
                    elif isinstance(carried, _Reading):
                        found.setdefault(step.to, []).append(carried)
        if not found:
            break
        frontier = {}
        for clock, readings in found.items():
            distinct: dict[Fraction, _Reading] = {}
            for reading in sorted(readings, key=_preference):
                distinct.setdefault(reading.ticks, reading)
            if len(distinct) > MAX_READINGS:
                crowded = True
                continue
            frontier[clock] = sorted(distinct.values(), key=lambda r: r.ticks)
        reached.update(found)
        if target in frontier:
            return Conversion(_result(frontier[target], target), None)
        if target in found:  # reached, but with too many readings to report
            break
    missing = MissingHop(
        reached=tuple(sorted(reached - {target})),
        target=target,
        outside_validity=tuple(outside[k] for k in sorted(outside)),
        parameters_unknown=tuple(unknown[k] for k in sorted(unknown)),
        too_ambiguous=crowded,
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
