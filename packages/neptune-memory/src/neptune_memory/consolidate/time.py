"""The time-domain registry as a deterministic consolidator (ADR 0011).

Three kinds of claim, never a re-timed one (ADR 0007 §4):

- **``has_clock``**: per run whose record declares its machine, one claim per clock its records
  carry (each stream's clocks and the run's own), over the interval the records observe that clock:
  the instants stated on the clock itself, else the run's ``[first, last]`` on the clock the run
  states it on. A clock no record places in time grounds no claim (a finding).
- **``maps_to`` + ``clock_map``**: per declared compiler ``ClockMapping`` (root ADR 0050 §5), an
  edge between the two clock nodes and the mapping's parameters as stated, both over its validity
  on the source clock, ``assertion_kind`` as the record states it. A validity side the evidence
  does not state grounds no claim: it is never assumed open (root ADR 0060 §7).
- **Revision**: of two mappings of one clock pair whose windows overlap, the one that starts later
  holds from its start; the earlier keeps only what no later one covers, citing the records that
  closed it. Two that start together and say different things both stand, with a finding.
- **Chains**: every chain of two to ``MAX_CHAIN_HOPS`` mappings, followed source to target, whose
  hops each state an anchor and a rate, is a separate ``maps_to`` + ``clock_map`` pair over the
  instants where every hop applies. Its ``clock_map`` names the chain and nothing else: the offset
  is the hops' arithmetic (``schema.clocks.convert``), never an estimate of Memory's.

Estimated mappings (``derived/`` lines, ``inferred``) are not read here: they are the input of
``neptune_memory.derived.clocks``, which shares this module's policy. Malformed or contradictory
input is a finding and never a claim, and the rest of the build is unaffected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING, Final

from neptune.derived.provenance import INFERRED
from neptune.model.finding import Severity
from neptune.model.knowledge import NotApplicable
from neptune.model.time import INT64_MAX, INT64_MIN, Timestamp
from neptune_memory.consolidate import time_records as parse
from neptune_memory.consolidate.base import ClaimDraft, ConsolidationFinding, ConsolidatorOutput
from neptune_memory.consolidate.identity import node_ref
from neptune_memory.consolidate.time_records import (
    CLOCK_MAPPING,
    RUN,
    STREAM,
    Hop,
    RunClocks,
    StreamClocks,
    weakest,
)
from neptune_memory.schema.claim import TypedLiteral, ValueType
from neptune_memory.schema.clock_map import ClockMap, MapMethod
from neptune_memory.schema.interval import OPEN, Interval, Open
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CLOCK_MAP, HAS_CLOCK, MAPS_TO

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from neptune.model.ids import RecordId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune.model.provenance import EvidenceRef
    from neptune_memory.consolidate.base import ModelRef
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim, ClaimAssertionKind

TIME_CONSOLIDATOR_ID: Final = "memory.time"
# The longest chain composed into one claim. Every chain of 2..4 hops is one claim, so a fleet
# whose boot clocks each map to a site clock, and site clocks to GPS time, composes boot -> GPS.
MAX_CHAIN_HOPS: Final = 4


def clock_node(clock: RecordId) -> NodeRef:
    """A clock's node is keyed by its ``TimestampDomain`` record id, the clock as declared."""
    return NodeRef(NodeType.CLOCK, clock)


def finding(
    code: str,
    message: str,
    records: Iterable[RecordId] = (),
    severity: Severity = Severity.WARNING,
    **details: JsonValue,
) -> ConsolidationFinding:
    return ConsolidationFinding(
        code=f"time.{code}",
        severity=severity,
        message=message[:1000],
        records=tuple(records),
        details=details,
    )


def _malformed(kind: str, package_id: str, index: int, reason: str) -> ConsolidationFinding:
    try:
        reason.encode("utf-8")
    except UnicodeEncodeError:
        reason = "unrepresentable text"
    return finding(
        "malformed_record",
        f"{kind} record {index} in package {package_id!r} is malformed: {reason}",
        severity=Severity.ERROR,
        index=index,
        kind=kind,
        package_id=package_id,
    )


# --- Reading the Ledger -------------------------------------------------------------------------


@dataclass
class View:
    """What one build reads, de-duplicated by record id across packages and sorted by it."""

    runs: dict[RecordId, RunClocks] = field(default_factory=dict)
    streams: dict[RecordId, StreamClocks] = field(default_factory=dict)
    hops: list[Hop] = field(default_factory=list)
    findings: list[ConsolidationFinding] = field(default_factory=list)


def read(ledger: LedgerReader, *, timing: bool, estimated: bool) -> View:
    """Parse runs and streams (``timing``) and clock mappings: the declared ones always, the
    estimated ones when ``estimated``. A record seen twice with two contents is used nowhere.

    Findings name only what this caller reads: ``memory.time`` reports runs, streams and declared
    mappings; ``memory.time_estimates`` only estimated mappings, so nothing is reported twice.
    """
    view = View()
    seen: dict[RecordId, object] = {}
    conflicted: set[RecordId] = set()
    parsed: dict[RecordId, object] = {}
    parsers: list[tuple[str, Callable[[Mapping[str, object]], object]]] = [
        *([(RUN, parse.run), (STREAM, parse.stream)] if timing else []),
        (CLOCK_MAPPING, parse.mapping),
    ]
    for ref in ledger.list_packages():
        for kind, parser in parsers:
            for index, record in enumerate(ledger.read_records(ref.package_id, kind) or ()):
                guessed = kind == CLOCK_MAPPING and parse.is_estimate(record)
                reports = timing if kind != CLOCK_MAPPING else guessed == estimated
                if guessed and not estimated:
                    if timing:
                        view.findings.append(_estimate_skipped(ref.package_id, index))
                    continue
                try:
                    value = parser(record)
                except parse.Unstated as exc:
                    if reports:
                        view.findings.append(_unstated(exc, ref.package_id, index))
                    continue
                except parse.Malformed as exc:
                    if reports:
                        view.findings.append(_malformed(kind, ref.package_id, index, str(exc)))
                    continue
                rid: RecordId = value.record  # type: ignore[attr-defined]
                if rid in conflicted:
                    continue
                if seen.setdefault(rid, value) != value:
                    conflicted.add(rid)
                    if reports:
                        view.findings.append(
                            finding(
                                "record_conflict",
                                "one record id carries different content in two places; record"
                                " not used",
                                (rid,),
                                Severity.ERROR,
                            )
                        )
                    continue
                parsed[rid] = value
    for rid in sorted(parsed.keys() - conflicted):
        value = parsed[rid]
        if isinstance(value, RunClocks):
            view.runs[rid] = value
        elif isinstance(value, StreamClocks):
            view.streams[rid] = value
        elif isinstance(value, Hop):
            view.hops.append(value)
    return view


def _estimate_skipped(package_id: str, index: int) -> ConsolidationFinding:
    return finding(
        "estimated_mapping",
        f"clock_mapping record {index} in package {package_id!r} is an estimate (derived/): "
        "memory.time_estimates reads it, as an inferred claim",
        severity=Severity.INFO,
        index=index,
        package_id=package_id,
    )


def _unstated(exc: parse.Unstated, package_id: str, index: int) -> ConsolidationFinding:
    return finding(
        "validity_unstated",
        f"clock_mapping record {index} in package {package_id!r} grounds no claim: {exc}; a"
        " window the evidence does not bound is never assumed open",
        () if exc.record is None else (exc.record,),
        index=index,
        package_id=package_id,
    )


# --- has_clock ----------------------------------------------------------------------------------


def _observed(
    clock: RecordId, run: RunClocks, carrying: Sequence[StreamClocks]
) -> tuple[Timestamp, Timestamp | Open] | None:
    """The interval the records observe ``clock``: on the clock itself where they state an
    instant on it, else the run's own interval on the clock the run is stated on."""
    firsts = [t for t in (run.first, *(s.first for s in carrying)) if t is not None]
    lasts = [t for t in (run.last, *(s.last for s in carrying)) if t is not None]
    starts = [t for t in firsts if t.domain_id == clock]
    if starts:
        ends = [t for t in lasts if t.domain_id == clock]
        return min(starts), _after(max(ends)) if ends else OPEN
    if run.first is None:
        return None
    last = run.last if run.last is not None and run.last.domain_id == run.first.domain_id else None
    return run.first, OPEN if last is None else _after(last)


def _after(last: Timestamp) -> Timestamp | Open:
    """An inclusive last instant as an exclusive end: the next tick, or open past int64."""
    return OPEN if last.ticks == INT64_MAX else Timestamp(last.ticks + 1, last.domain_id)


def has_clock(view: View) -> list[ClaimDraft]:
    by_run: dict[RecordId, list[StreamClocks]] = {}
    for stream in view.streams.values():
        by_run.setdefault(stream.run, []).append(stream)
    drafts: list[ClaimDraft] = []
    for run in view.runs.values():
        if run.machine is None:
            continue  # the run states no machine (explicitly, in the Ledger); nothing to attach
        streams = by_run.get(run.record, [])
        clocks = {c for s in streams for c in s.clocks}
        clocks.update(t.domain_id for t in (run.first, run.last) if t is not None)
        for clock in sorted(clocks):
            carrying = [s for s in streams if clock in s.clocks]
            records = (run.record, *(s.record for s in carrying))
            span = _observed(clock, run, carrying)
            if span is None:
                view.findings.append(
                    finding(
                        "clock_unobserved",
                        "a clock of the run's machine that no record places in time; no claim",
                        records,
                        Severity.INFO,
                        clock=clock,
                    )
                )
                continue
            start, end = span
            if isinstance(end, Timestamp) and not start < end:
                view.findings.append(
                    finding(
                        "untimeable_clock",
                        "the records state a clock's last instant before its first; no claim",
                        records,
                        clock=clock,
                    )
                )
                continue
            drafts.append(
                ClaimDraft(
                    subject=node_ref(NodeType.MACHINE, run.machine),
                    predicate=HAS_CLOCK,
                    object=clock_node(clock),
                    valid_from=start,
                    valid_to=end,
                    assertion_kind=weakest((*run.kinds, *(s.kind for s in carrying))),
                    evidence=(*run.evidence, *(s.evidence for s in carrying)),
                    records=records,
                )
            )
    return drafts


# --- Mappings and their revision ----------------------------------------------------------------


@dataclass(frozen=True)
class Piece:
    """Where one mapping holds once later-starting mappings of its pair have taken over."""

    hop: Hop
    interval: Interval
    closed_by: tuple[Hop, ...]  # the later-starting mappings of its pair that overlap it

    @property
    def source(self) -> RecordId:
        return self.hop.source

    @property
    def target(self) -> RecordId:
        return self.hop.target

    @property
    def evidence(self) -> tuple[EvidenceRef, ...]:
        return (*self.hop.evidence, *(e for h in self.closed_by for e in h.evidence))

    @property
    def records(self) -> tuple[RecordId, ...]:
        return (*self.hop.records, *(r for h in self.closed_by for r in h.records))


def revise(
    hops: Sequence[Hop],
    findings: list[ConsolidationFinding],
    report: Callable[[Hop], bool] = lambda hop: True,
) -> list[Piece]:
    """Each mapping's pieces: its window minus every overlapping window of its pair (same source,
    target and declared-or-estimated) that starts later. Equal starts with different parameters
    are a finding (for the mappings ``report`` selects) and both stand. Pieces sorted by
    (source, target, start, record)."""
    groups: dict[tuple[RecordId, RecordId, bool], list[Hop]] = {}
    for hop in hops:
        groups.setdefault((hop.source, hop.target, hop.estimated), []).append(hop)
    pieces: list[Piece] = []
    for key in sorted(groups):
        group = sorted(groups[key], key=lambda h: (h.start.ticks, h.record))
        for i, hop in enumerate(group):
            later = [g for g in group if g.start > hop.start and g.interval.overlaps(hop.interval)]
            for g in group[i + 1 :]:
                if g.start == hop.start and g.clock_map != hop.clock_map and report(hop):
                    findings.append(
                        finding(
                            "conflicting_mappings",
                            "two mappings of one clock pair from one instant say different"
                            " things; both stand, and a conversion through them is ambiguous",
                            (*hop.records, *g.records),
                            source=hop.source,
                            target=hop.target,
                        )
                    )
            kept = hop.interval.minus(g.interval for g in later)
            pieces.extend(Piece(hop, interval, tuple(later)) for interval in kept)
    return sorted(pieces, key=lambda p: (p.source, p.target, p.interval.start.ticks, p.hop.record))


def _literal(clock_map: ClockMap) -> TypedLiteral:
    return TypedLiteral(ValueType.CLOCK_MAP, clock_map)


def mapping_drafts(
    subject: RecordId,
    clock_map: ClockMap,
    interval: Interval,
    kind: ClaimAssertionKind,
    evidence: Sequence[EvidenceRef],
    records: Sequence[RecordId],
    confidence: Knowledge[float] | None = None,
) -> list[ClaimDraft]:
    """The ``maps_to`` edge and its ``clock_map``: one subject, one window, one provenance."""
    common: dict[str, object] = {
        "subject": clock_node(subject),
        "valid_from": interval.start,
        "valid_to": interval.end,
        "assertion_kind": kind,
        "evidence": tuple(evidence),
        "records": tuple(records),
    }
    if confidence is not None:
        common["confidence"] = confidence
    return [
        ClaimDraft(predicate=MAPS_TO, object=clock_node(clock_map.target), **common),  # type: ignore[arg-type]
        ClaimDraft(predicate=CLOCK_MAP, object=_literal(clock_map), **common),  # type: ignore[arg-type]
    ]


def piece_drafts(piece: Piece, confidence: Knowledge[float] | None = None) -> list[ClaimDraft]:
    hop = piece.hop
    return mapping_drafts(
        hop.source,
        hop.clock_map,
        piece.interval,
        hop.assertion_kind,
        piece.evidence,
        piece.records,
        confidence,
    )


# --- Chains -------------------------------------------------------------------------------------


def _clamp(ticks: int) -> int:
    return max(INT64_MIN, min(INT64_MAX, ticks))


def _intersect(a: Interval, b: Interval) -> Interval | None:
    start = max(a.start, b.start)
    ends = [e for e in (a.end, b.end) if isinstance(e, Timestamp)]
    end: Timestamp | Open = min(ends) if ends else OPEN
    return Interval(start, end) if isinstance(end, Open) or start < end else None


def preimage(piece: Piece, window: Interval) -> Interval | None:
    """The source instants where ``piece`` applies and lands inside ``window`` (on its target).

    Exact: for an increasing map ``f`` and integer ``t``, ``s <= f(t) < e`` iff
    ``ceil(f⁻¹(s)) <= t < ceil(f⁻¹(e))``. ``None`` when there are none, or the piece has no
    stated anchor and rate."""
    affine = piece.hop.clock_map.affine()
    if affine is None:
        return None
    rate, offset = affine
    source = piece.source

    def back(ticks: int) -> int:
        return math.ceil((Fraction(ticks) - offset) / rate)

    lower = back(window.start.ticks)
    if window.start.ticks == INT64_MIN or lower < INT64_MIN:
        lower = INT64_MIN  # open below, or below the earliest instant the source can write
    if lower > INT64_MAX:
        return None
    end: Timestamp | Open = OPEN
    if isinstance(window.end, Timestamp):
        upper = back(window.end.ticks)
        if upper <= INT64_MIN:
            return None
        if upper <= INT64_MAX:
            end = Timestamp(upper, source)
    if isinstance(end, Timestamp) and lower >= end.ticks:
        return None
    return _intersect(piece.interval, Interval(Timestamp(lower, source), end))


def window(chain: Sequence[Piece]) -> Interval | None:
    """Where every hop of ``chain`` applies, on its first clock: folded from the last hop back."""
    held: Interval | None = chain[-1].interval
    for piece in reversed(chain[:-1]):
        if held is None:
            return None
        held = preimage(piece, held)
    return held


@dataclass(frozen=True)
class Chain:
    pieces: tuple[Piece, ...]
    interval: Interval

    @property
    def estimated(self) -> bool:
        return any(p.hop.estimated for p in self.pieces)

    def clock_map(self) -> ClockMap:
        na = NotApplicable()
        return ClockMap(
            target=self.pieces[-1].target,
            method=MapMethod.COMPOSED,
            anchor=na,
            rate=na,
            residual_bound=na,
            chain=tuple(p.hop.record for p in self.pieces),
            via=tuple(p.target for p in self.pieces[:-1]),
        )

    def kind(self) -> ClaimAssertionKind:
        if self.estimated:
            return INFERRED
        return weakest(tuple(p.hop.assertion_kind for p in self.pieces))  # type: ignore[misc]

    def drafts(self, confidence: Knowledge[float] | None = None) -> list[ClaimDraft]:
        return mapping_drafts(
            self.pieces[0].source,
            self.clock_map(),
            self.interval,
            self.kind(),
            [e for p in self.pieces for e in p.evidence],
            [r for p in self.pieces for r in p.records],
            confidence,
        )


def chains(pieces: Sequence[Piece]) -> list[Chain]:
    """Every chain of 2..``MAX_CHAIN_HOPS`` pieces, source to target, visiting no clock twice, that
    holds somewhere; in source order, then depth-first in piece order (deterministic)."""
    onward: dict[RecordId, list[Piece]] = {}
    for piece in pieces:
        if piece.hop.clock_map.affine() is not None:
            onward.setdefault(piece.source, []).append(piece)
    found: list[Chain] = []

    def extend(path: tuple[Piece, ...], visited: frozenset[RecordId]) -> None:
        for piece in onward.get(path[-1].target, ()):
            if piece.target in visited:
                continue
            longer = (*path, piece)
            held = window(longer)
            if held is None:
                continue  # an extension only narrows where a chain holds
            found.append(Chain(longer, held))
            if len(longer) < MAX_CHAIN_HOPS:
                extend(longer, visited | {piece.target})

    for source in sorted(onward):
        for first in onward[source]:
            extend((first,), frozenset({first.source, first.target}))
    return found


# --- The consolidator ---------------------------------------------------------------------------


def unknown_config(config: Mapping[str, JsonValue], consolidator_id: str) -> ConsolidationFinding:
    return finding(
        "unknown_config",
        f"{consolidator_id} takes no configuration but its model; keys ignored",
        keys=sorted(config),
    )


class TimeDomainConsolidator:
    """Deterministic ``has_clock``, ``maps_to`` and ``clock_map`` claims. Takes no configuration."""

    consolidator_id: Final = TIME_CONSOLIDATOR_ID
    version: Final = "1"
    model: Final[ModelRef | None] = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        view = read(ledger, timing=True, estimated=False)
        if config:
            view.findings.append(unknown_config(config, self.consolidator_id))
        pieces = revise(view.hops, view.findings)
        drafts = has_clock(view)
        for piece in pieces:
            drafts.extend(piece_drafts(piece))
        for chain in chains(pieces):
            drafts.extend(chain.drafts())
        return ConsolidatorOutput(tuple(drafts), tuple(view.findings))
