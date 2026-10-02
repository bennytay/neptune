"""Estimated clock relations, and aligning an instant across clocks with its bound (ADR 0060).

Two clocks are related only by a ``ClockMapping`` (ADR 0050 §5). A canonical one is what a source
states; the ones here are estimated from sync anchors (two readings taken as one instant on two
clocks) and so are ``inferred``: ADR 0050's fields exactly, with an ``InferredProvenance``.

- ``InferredClockMapping``: an affine map a fit estimated, written to ``derived/clock_mapping``.
- ``InferredTimestampDomain``: a clock found inside a stream's values (a GPS receiver's time in
  its fix messages), ``TimestampDomain``'s fields exactly, written to ``derived/timestamp_domain``.
- ``fit_line``: the deterministic fit: exact least squares over integer ticks, the rate rounded to
  a bounded denominator, the residual bound the largest residual over every anchor, rounded up.
- ``ClockGraph.align``: an instant on one clock as an instant on another, through the mappings
  that relate them, with the bound accumulated along the way; or why it cannot be (``Unaligned``):
  no mapping relates the clocks, the instant lies outside every mapping's validity, or a rate is
  not known. Nothing is ever re-timed: the result is a new value beside the source's ticks.

No float is computed anywhere: ticks are integers, rates and estimates exact fractions.
"""

import math
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Any, Final

from neptune.derived.provenance import DERIVED_SCHEMA_VERSION as DERIVED_SCHEMA_VERSION
from neptune.derived.provenance import INFERRED, InferredProvenance, derived_object
from neptune.model._fields import (
    check_type,
    enum_decoder,
    json_array,
    json_str,
    values_of,
)
from neptune.model.alignment import (
    ClockAnchor,
    ClockMapping,
    MappingMethod,
    ValidityWindow,
    clock_anchor_from_json,
    validity_window_from_json,
)
from neptune.model.ids import RecordId, check_text, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Grounding,
    Knowledge,
    Known,
    KnownAbsent,
    Unknown,
    from_json,
    to_json,
)
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json
from neptune.model.reference import TimestampDomain
from neptune.model.time import (
    INT64_MAX,
    INT64_MIN,
    ClockRole,
    Duration,
    Epoch,
    Timescale,
    Timestamp,
    duration_from_json,
    resolution_from_json,
    resolution_to_json,
)

MAPPING_KIND: Final = ClockMapping.kind
DOMAIN_KIND: Final = TimestampDomain.kind

# The largest denominator a fitted rate may have: exact enough that rounding it moves no map by
# a tick over 10^9 ticks of the source, small enough that its JSON stays short.
MAX_RATE_DENOMINATOR: Final = 10**9


def _inherited_only(data: JsonObject) -> Grounding:
    """A derived record's states inherit its inferred provenance; none carries a canonical one."""
    raise ValueError("a state of a derived record inherits the record's provenance")


def _knowledge(data: JsonValue, decode: Callable[[JsonValue], Any]) -> Knowledge[Any]:
    return from_json(data, decode, _inherited_only)


def _evidence(refs: tuple[EvidenceRef, ...], transform: RecordId) -> InferredProvenance:
    return InferredProvenance(refs, transform)  # checks both


def _envelope(kind: str, record_id: RecordId, provenance: InferredProvenance) -> JsonObject:
    return {
        "assertion_kind": INFERRED,
        "evidence": [ref.to_json() for ref in provenance.evidence],
        "id": record_id,
        "kind": kind,
        "schema_version": DERIVED_SCHEMA_VERSION,
        "transform": provenance.transform,
    }


# --- Inferred clock mappings --------------------------------------------------------------------


@dataclass(frozen=True)
class InferredClockMapping:
    """An affine map from ``source`` ticks to ``target`` ticks that a procedure estimated.

    The fields and their meaning are ``ClockMapping``'s (ADR 0050 §5): ``target(t) =
    anchor.target + rate * (t - anchor.source)``; ``residual_bound`` is a non-negative duration on
    the target clock, ``validity`` a window on the source clock. ``method`` names how the anchors
    relate their two readings (``co_sampled``: one record's two fields). Every state inherits the
    record's ``InferredProvenance``: the ``evidence`` the anchors were read from and the
    ``transform`` that fitted them (ADR 0050 §2). ``residual_bound`` is ``Unknown`` where nothing
    bounds how far apart an anchor's two readings may be; ``rate`` is ``Unknown`` where every
    anchor is one instant of the source, which is then the whole window.
    """

    kind = MAPPING_KIND
    id: RecordId
    transform: RecordId
    evidence: tuple[EvidenceRef, ...]
    source: RecordId
    target: RecordId
    method: MappingMethod
    anchor: Knowledge[ClockAnchor]
    rate: Knowledge[Fraction]
    residual_bound: Knowledge[Duration]
    validity: Knowledge[ValidityWindow]

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        _evidence(self.evidence, self.transform)
        parse_record_id(self.source)
        parse_record_id(self.target)
        if self.source == self.target:
            raise ValueError(f"a clock mapping relates two clocks; {self.source} is both")
        if not isinstance(self.method, MappingMethod):
            raise TypeError(f"method must be a MappingMethod, got {self.method!r}")
        check_type("anchor", self.anchor, ClockAnchor)
        for anchor in values_of(self.anchor):
            if (anchor.source.domain_id, anchor.target.domain_id) != (self.source, self.target):
                raise ValueError("an anchor's instants must be on the source and target clocks")
        check_type("rate", self.rate, Fraction)
        if any(rate <= 0 for rate in values_of(self.rate)):
            raise ValueError(f"rate must be positive: a clock map is increasing, got {self.rate}")
        check_type("residual_bound", self.residual_bound, Duration)
        for bound in values_of(self.residual_bound):
            if bound.domain_id != self.target or bound.ticks < 0:
                raise ValueError("residual_bound is a non-negative duration on the target clock")
        check_type("validity", self.validity, ValidityWindow)
        for window in values_of(self.validity):
            if window.clock != self.source:
                raise ValueError(f"validity must be on {self.source}, got {window.clock}")

    @property
    def provenance(self) -> InferredProvenance:
        return InferredProvenance(self.evidence, self.transform)

    def to_json(self) -> JsonObject:
        return {
            **_envelope(self.kind, self.id, self.provenance),
            "anchor": to_json(self.anchor, ClockAnchor.to_json),
            "method": str(self.method),
            "rate": to_json(self.rate, resolution_to_json),
            "residual_bound": to_json(self.residual_bound, Duration.to_json),
            "source": self.source,
            "target": self.target,
            "validity": to_json(self.validity, ValidityWindow.to_json),
        }


_MAPPING_KEYS: Final = {
    "anchor",
    "evidence",
    "id",
    "method",
    "rate",
    "residual_bound",
    "source",
    "target",
    "transform",
    "validity",
}


def inferred_clock_mapping_from_json(data: JsonValue) -> InferredClockMapping:
    """Parse strictly; no state may carry provenance of its own."""
    obj = derived_object(data, MAPPING_KIND, _MAPPING_KEYS)
    return InferredClockMapping(
        id=parse_record_id(json_str(obj["id"], "id")),
        transform=parse_record_id(json_str(obj["transform"], "transform")),
        evidence=tuple(evidence_ref_from_json(r) for r in json_array(obj["evidence"], "evidence")),
        source=parse_record_id(json_str(obj["source"], "source")),
        target=parse_record_id(json_str(obj["target"], "target")),
        method=MappingMethod(json_str(obj["method"], "method")),
        anchor=_knowledge(obj["anchor"], clock_anchor_from_json),
        rate=_knowledge(obj["rate"], resolution_from_json),
        residual_bound=_knowledge(obj["residual_bound"], duration_from_json),
        validity=_knowledge(obj["validity"], validity_window_from_json),
    )


# --- Inferred timestamp domains -----------------------------------------------------------------


@dataclass(frozen=True)
class InferredTimestampDomain:
    """A clock found in a stream's values, not declared as one: ``TimestampDomain``'s fields.

    A GPS fix message carries the receiver's time beside the log's own clock; the format declares
    the fields, not that they are a clock, so the clock is inferred (from the message definition
    its producer publishes) and lives here. ``field`` names the value fields read, verbatim, in
    the order they combine; ``scope`` where they live, outermost first. Its ticks stay in the
    stream's value columns, untouched.
    """

    kind = DOMAIN_KIND
    id: RecordId
    transform: RecordId
    evidence: tuple[EvidenceRef, ...]
    field: str
    scope: tuple[str, ...]
    role: Knowledge[ClockRole]
    resolution: Knowledge[Fraction]
    epoch: Knowledge[Epoch]
    timescale: Knowledge[Timescale]
    declared_monotonic: Knowledge[bool]

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        _evidence(self.evidence, self.transform)
        check_text("field", self.field)
        if not isinstance(self.scope, tuple) or not all(isinstance(s, str) for s in self.scope):
            raise TypeError("scope must be a tuple of strings")
        check_type("role", self.role, ClockRole)
        check_type("resolution", self.resolution, Fraction)
        if any(value <= 0 for value in values_of(self.resolution)):
            raise ValueError(f"resolution must be positive: {self.resolution}")
        check_type("epoch", self.epoch, Epoch)
        check_type("timescale", self.timescale, Timescale)
        check_type("declared_monotonic", self.declared_monotonic, bool)

    @property
    def provenance(self) -> InferredProvenance:
        return InferredProvenance(self.evidence, self.transform)

    def to_json(self) -> JsonObject:
        return {
            **_envelope(self.kind, self.id, self.provenance),
            "declared_monotonic": to_json(self.declared_monotonic),
            "epoch": to_json(self.epoch, str),
            "field": self.field,
            "resolution": to_json(self.resolution, resolution_to_json),
            "role": to_json(self.role, str),
            "scope": list(self.scope),
            "timescale": to_json(self.timescale, str),
        }


def _bool(data: JsonValue) -> bool:
    if not isinstance(data, bool):
        raise ValueError(f"expected a boolean, got {data!r}")
    return data


def inferred_timestamp_domain_from_json(data: JsonValue) -> InferredTimestampDomain:
    """Parse strictly; no state may carry provenance of its own."""
    keys = {"declared_monotonic", "epoch", "evidence", "field", "id", "resolution", "role"}
    obj = derived_object(data, DOMAIN_KIND, keys | {"scope", "timescale", "transform"})
    return InferredTimestampDomain(
        id=parse_record_id(json_str(obj["id"], "id")),
        transform=parse_record_id(json_str(obj["transform"], "transform")),
        evidence=tuple(evidence_ref_from_json(r) for r in json_array(obj["evidence"], "evidence")),
        field=json_str(obj["field"], "field"),
        scope=tuple(json_str(s, "scope") for s in json_array(obj["scope"], "scope")),
        role=_knowledge(obj["role"], enum_decoder(ClockRole)),
        resolution=_knowledge(obj["resolution"], resolution_from_json),
        epoch=_knowledge(obj["epoch"], enum_decoder(Epoch)),
        timescale=_knowledge(obj["timescale"], enum_decoder(Timescale)),
        declared_monotonic=_knowledge(obj["declared_monotonic"], _bool),
    )


# --- Fitting ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """A fitted map in integer ticks: ``target = anchor_target + rate * (source - anchor_source)``.

    ``rate`` is ``None`` when every anchor is one source instant (``first == last``). ``residual``
    is the largest distance, in target ticks rounded up, between any anchor's target reading and
    the line at its source reading. ``first`` and ``last`` are the extreme source readings.
    """

    anchor_source: int
    anchor_target: int
    rate: Fraction | None
    residual: int
    first: int
    last: int
    count: int


class FitProblem(StrEnum):
    NO_ANCHORS = "no_anchors"  # no reading pair: the clocks stay unrelated
    NOT_INCREASING = "not_increasing"  # the target runs backward against the source
    RATE_TOO_SMALL = "rate_too_small"  # increasing, but below 1/max_denominator: rounds to zero
    OUT_OF_RANGE = "out_of_range"  # the anchor does not fit a signed 64-bit tick


def _round(value: Fraction) -> int:
    """Nearest integer, halves up (toward +infinity: 2.5 -> 3, -2.5 -> -2): a fixed rule,
    independent of the platform."""
    floor = math.floor(value)
    return floor + 1 if value - floor >= Fraction(1, 2) else floor


def fit_line(
    pairs: Callable[[], Iterable[tuple[int, int]]],
    max_denominator: int = MAX_RATE_DENOMINATOR,
) -> Line | FitProblem:
    """Fit a line through ``(source, target)`` tick pairs, deterministically.

    ``pairs`` is called twice (one pass for the sums, one for the residuals) and must give the
    same pairs both times, in any order. The slope is the exact least-squares one over integer
    sums, rounded to ``max_denominator``; the anchor is the source mean, floored, and the line's
    value there, rounded. The residual is measured against that rounded line, exactly, so it is a
    true bound over every anchor. No float is involved, so every platform gives the same bytes.
    """
    n = sx = sy = sxx = sxy = 0
    first = last = 0
    for x, y in pairs():
        if n == 0:
            first = last = x
        first, last = min(first, x), max(last, x)
        n, sx, sy, sxx, sxy = n + 1, sx + x, sy + y, sxx + x * x, sxy + x * y
    if n == 0:
        return FitProblem.NO_ANCHORS
    anchor_source = sx // n  # the floored mean: an integer source tick
    spread = n * sxx - sx * sx
    rate: Fraction | None = None
    if spread == 0:
        anchor_target = _round(Fraction(sy, n))
    else:
        slope = Fraction(n * sxy - sx * sy, spread)
        if slope <= 0:
            return FitProblem.NOT_INCREASING
        rate = slope.limit_denominator(max_denominator)
        if rate <= 0:  # a slope too small for the denominator rounds to zero
            return FitProblem.RATE_TOO_SMALL
        anchor_target = _round(Fraction(sy, n) + slope * (anchor_source - Fraction(sx, n)))
    if not INT64_MIN <= anchor_target <= INT64_MAX:
        return FitProblem.OUT_OF_RANGE
    worst = Fraction(0)
    for x, y in pairs():
        at = Fraction(anchor_target)
        if rate is not None:
            at += rate * (x - anchor_source)
        distance = abs(y - at)
        worst = distance if distance > worst else worst
    return Line(anchor_source, anchor_target, rate, math.ceil(worst), first, last, n)


def fitted_mapping(
    *,
    record_id: RecordId,
    transform: RecordId,
    evidence: Iterable[EvidenceRef],
    source: RecordId,
    target: RecordId,
    line: Line,
    slack: int | None,
) -> InferredClockMapping:
    """The mapping a fit gives, valid over the source instants its anchors span and no further.

    ``slack`` bounds, in target ticks, how far apart an anchor's two readings' instants may be;
    ``None`` where nothing bounds it, which leaves the residual bound ``Unknown``: the map is
    estimated, its error is not. A bound past a signed 64-bit tick count is ``Unknown`` too
    (``bound_representable`` says when; the caller reports it).
    """
    bound: Knowledge[Duration] = Unknown()
    if slack is not None and bound_representable(line, slack):
        bound = Known(Duration(line.residual + slack, target))
    end: Knowledge[Timestamp] = (
        Known(Timestamp(line.last + 1, source)) if line.last < INT64_MAX else Unknown()
    )
    window = ValidityWindow(source, Known(Timestamp(line.first, source)), end)
    anchor = ClockAnchor(
        Timestamp(line.anchor_source, source), Timestamp(line.anchor_target, target)
    )
    return InferredClockMapping(
        id=record_id,
        transform=transform,
        evidence=tuple(dict.fromkeys(evidence)),
        source=source,
        target=target,
        method=MappingMethod.CO_SAMPLED,
        anchor=Known(anchor),
        rate=Known(line.rate) if line.rate is not None else Unknown(),
        residual_bound=bound,
        validity=Known(window),
    )


def bound_representable(line: Line, slack: int | None) -> bool:
    """Whether a stated ``slack`` gives the fit a bound that fits a signed 64-bit tick count."""
    return slack is not None and line.residual + slack <= INT64_MAX


# --- Aligning across clocks ---------------------------------------------------------------------

AnyMapping = ClockMapping | InferredClockMapping


class Reason(StrEnum):
    """Why an instant on one clock has no instant on another."""

    UNSYNCHRONISED = "unsynchronised"  # no chain of mappings relates the two clocks
    OUTSIDE_VALIDITY = "outside_validity"  # mappings relate them, but not at this instant
    RATE_UNKNOWN = "rate_unknown"  # the only mappings that could apply state no rate here


@dataclass(frozen=True)
class Aligned:
    """``instant`` on the target clock, within ``bound`` (``None``: no bound is known).

    ``path`` is the mapping ids applied, in order; ``inferred`` says whether any of them was
    estimated rather than stated. The source's own timestamp is untouched; this is a new value.
    """

    instant: Timestamp
    bound: Duration | None
    path: tuple[RecordId, ...]
    inferred: bool

    def window(self) -> tuple[Timestamp, Timestamp] | None:
        """``[instant - bound, instant + bound]``, both ends included; ``None`` when unbounded or
        when an end falls outside the signed 64-bit tick range (no tick can name it)."""
        if self.bound is None:
            return None
        low, high = self.instant.ticks - self.bound.ticks, self.instant.ticks + self.bound.ticks
        if low < INT64_MIN or high > INT64_MAX:
            return None
        return Timestamp(low, self.instant.domain_id), Timestamp(high, self.instant.domain_id)


@dataclass(frozen=True)
class Unaligned:
    reason: Reason


@dataclass(frozen=True)
class _Edge:
    mapping: AnyMapping
    forward: bool  # source -> target; else target -> source through the inverse


def _inside(window: Knowledge[ValidityWindow], tick: Fraction) -> bool | None:
    """Whether ``tick`` lies in the window; ``None`` where a bound is not stated either way."""
    if not isinstance(window, Known):
        return None
    start, end = window.value.start, window.value.end
    if isinstance(start, Known):
        if tick < start.value.ticks:
            return False
    elif not isinstance(start, KnownAbsent):
        return None
    if isinstance(end, Known):
        if tick >= end.value.ticks:
            return False
    elif not isinstance(end, KnownAbsent):
        return None
    return True


class ClockGraph:
    """Clocks as nodes, mappings as edges usable both ways (an increasing affine map inverts)."""

    def __init__(self, mappings: Iterable[AnyMapping]) -> None:
        ordered = sorted(
            {m.id: m for m in mappings}.values(),
            key=lambda m: (isinstance(m, InferredClockMapping), m.id),
        )
        self._edges: dict[RecordId, list[_Edge]] = {}
        for mapping in ordered:  # stated before inferred, then by id: a fixed search order
            if not isinstance(mapping.anchor, Known):
                continue  # nothing to apply
            self._edges.setdefault(mapping.source, []).append(_Edge(mapping, True))
            self._edges.setdefault(mapping.target, []).append(_Edge(mapping, False))

    def groups(self, clocks: Iterable[RecordId]) -> list[list[RecordId]]:
        """``clocks`` and every clock a mapping names, in groups no mapping chain joins, each
        sorted, the groups by their first id."""
        nodes = sorted(set(clocks) | set(self._edges))
        seen: set[RecordId] = set()
        found: list[list[RecordId]] = []
        for node in nodes:
            if node in seen:
                continue
            group, queue = [], deque([node])
            seen.add(node)
            while queue:
                here = queue.popleft()
                group.append(here)
                for edge in self._edges.get(here, ()):
                    there = edge.mapping.target if edge.forward else edge.mapping.source
                    if there not in seen:
                        seen.add(there)
                        queue.append(there)
            found.append(sorted(group))
        return found

    def align(self, stamp: Timestamp, target: RecordId) -> Aligned | Unaligned:
        """``stamp`` as an instant on ``target``: breadth-first through the mappings that hold
        at the running estimate, stated ones first. The bound grows along the path: each map
        scales the error so far by its rate and adds its own residual bound. A window bound that
        is not stated is not assumed open: the mapping does not apply."""
        parse_record_id(target)
        if stamp.domain_id == target:
            return Aligned(stamp, Duration(0, target), (), False)
        start = (Fraction(stamp.ticks), Fraction(0), (), False)
        best: dict[RecordId, tuple[Fraction, Fraction | None, tuple[RecordId, ...], bool]] = {
            stamp.domain_id: start
        }
        queue = deque([stamp.domain_id])
        blocked: Reason | None = None
        while queue:
            here = queue.popleft()
            value, error, path, inferred = best[here]
            for edge in self._edges.get(here, ()):
                mapping = edge.mapping
                there = mapping.target if edge.forward else mapping.source
                if there in best:
                    continue
                step = _apply(edge, value, error)
                if isinstance(step, Reason):
                    blocked = blocked or step
                    continue
                more = isinstance(mapping, InferredClockMapping)
                best[there] = (step[0], step[1], (*path, mapping.id), inferred or more)
                if there == target:
                    return _result(target, *best[there])
                queue.append(there)
        return Unaligned(blocked or Reason.UNSYNCHRONISED)


def _apply(
    edge: _Edge, value: Fraction, error: Fraction | None
) -> tuple[Fraction, Fraction | None] | Reason:
    mapping = edge.mapping
    assert isinstance(mapping.anchor, Known)
    anchor = mapping.anchor.value
    bound = mapping.residual_bound
    residual = Fraction(bound.value.ticks) if isinstance(bound, Known) else None
    rate = mapping.rate.value if isinstance(mapping.rate, Known) else None
    if edge.forward:
        if _inside(mapping.validity, value) is not True:
            return Reason.OUTSIDE_VALIDITY
        if rate is None:
            if value != anchor.source.ticks:
                return Reason.RATE_UNKNOWN
            moved = Fraction(anchor.target.ticks)
            scaled = error  # at the anchor itself: no rate scales the error so far
        else:
            moved = anchor.target.ticks + rate * (value - anchor.source.ticks)
            scaled = None if error is None else rate * error
        out = None if scaled is None or residual is None else scaled + residual
        return moved, out
    if rate is None:
        if value != anchor.target.ticks:
            return Reason.RATE_UNKNOWN
        moved = Fraction(anchor.source.ticks)
        out = None if error is None or residual is None else error + residual
    else:
        moved = anchor.source.ticks + (value - anchor.target.ticks) / rate
        out = None if error is None or residual is None else (error + residual) / rate
    if _inside(mapping.validity, moved) is not True:
        return Reason.OUTSIDE_VALIDITY
    return moved, out


def _result(
    target: RecordId,
    value: Fraction,
    error: Fraction | None,
    path: tuple[RecordId, ...],
    inferred: bool,
) -> Aligned | Unaligned:
    ticks = _round(value)
    if not INT64_MIN <= ticks <= INT64_MAX:
        return Unaligned(Reason.OUTSIDE_VALIDITY)
    # A bound past a signed 64-bit tick count is no bound a tick can state: unbounded.
    width = None if error is None else math.ceil(error + abs(ticks - value))
    bound = None if width is None or width > INT64_MAX else Duration(width, target)
    return Aligned(Timestamp(ticks, target), bound, path, inferred)
