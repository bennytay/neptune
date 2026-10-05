"""Configuration lineage as a deterministic consolidator (ADR 0010).

Three things, each from declared records only, each cited:

- **Machine chains.** Commissioning, maintenance, change and requalification records place a
  configuration on a machine at an instant on the record's own clock. Ordered per machine and clock,
  they give ``has_configuration`` spans (each until the next placement, or open) and a ``succeeds``
  claim at every decided change of configuration. Records that disagree on one instant, or a field
  that is ``Ambiguous``, give ``configuration_candidate`` claims and a ``chain_overlap`` finding; a
  record that leaves the configuration unknown gives ``configuration_unknown`` and a ``chain_gap``
  finding. Nothing bridges a gap: no ``succeeds`` is claimed across one.
- **Runs.** ``configuration_active_during`` from each compiler ``SnapshotBinding``, over the bound
  part of the run on the run's clock. A run no binding names is ``configuration_unknown`` over the
  run, never the nearest configuration in time.
- **Authorisation.** ``authorised_configuration`` from each ``AuthorisationEnvelope``; the part of
  a bound run window no envelope naming its configuration covers is an observed
  ``not_covered_by_authorisation`` claim. Windows are compared on one clock only; otherwise the
  coverage is a finding, not a claim.

Nodes are the identity consolidator's: ``identity.node_threads`` keys them, so a declared id with no
Ledger thread is a finding, never a node. Records are parsed by
``consolidate.configuration_records``; this module decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Final, Literal

from neptune.identity import canonical_json
from neptune.model.finding import Severity
from neptune.model.knowledge import AssertionKind
from neptune.model.time import Timestamp
from neptune_memory.consolidate import configuration_records as parse
from neptune_memory.consolidate.base import (
    ClaimDraft,
    ConsolidationFinding,
    ConsolidatorOutput,
    ModelRef,
)
from neptune_memory.consolidate.configuration_records import (
    AUTHORISATION_ENVELOPE,
    CHAIN_KINDS,
    RUN,
    SNAPSHOT_BINDING,
    SNAPSHOT_KINDS,
    Binding,
    Bound,
    Envelope,
    Event,
    RunRecord,
    Snapshot,
)
from neptune_memory.consolidate.identity import node_ref, node_threads
from neptune_memory.consolidate.identity_records import (
    TIMESTAMP_DOMAIN,
    Clock,
    Inferred,
    Malformed,
    clock,
)
from neptune_memory.schema.claim import LedgerRecordRef
from neptune_memory.schema.interval import OPEN, CivilClock, Interval, Open
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import (
    AUTHORISED_CONFIGURATION,
    CONFIGURATION_ACTIVE_DURING,
    CONFIGURATION_CANDIDATE,
    CONFIGURATION_UNKNOWN,
    NOT_COVERED_BY_AUTHORISATION,
    SUCCEEDS,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from neptune.model.ids import LogicalId, RecordId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.provenance import EvidenceRef
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim, ClaimObject

CONFIGURATION_CONSOLIDATOR_ID: Final = "memory.configuration"
HAS_CONFIGURATION: Final = "has_configuration"

_STATED, _OBSERVED = AssertionKind.STATED, AssertionKind.OBSERVED


def _finding(
    code: str,
    message: str,
    records: Iterable[RecordId] = (),
    severity: Severity = Severity.WARNING,
    **details: JsonValue,
) -> ConsolidationFinding:
    return ConsolidationFinding(
        code=f"configuration.{code}",
        severity=severity,
        message=message,
        records=tuple(records),
        details=details,
    )


def _safe(reason: str) -> str:
    try:
        reason.encode("utf-8")
    except UnicodeEncodeError:
        return "unrepresentable text"
    return reason if reason.isprintable() else "unprintable text"


def _ref_key(ref: EvidenceRef) -> bytes:
    return canonical_json.dumps(ref.to_json())


def _span(start: Timestamp, end: Timestamp | Open) -> JsonValue:
    return {"end": end.to_json(), "start": start.to_json()}


# --- Reading the Ledger -------------------------------------------------------------------------


def _chain_parser(kind: str) -> Callable[[Mapping[str, object]], object]:
    return partial(parse.event, kind)


def _snapshot_parser(kind: str) -> Callable[[Mapping[str, object]], object]:
    return partial(parse.snapshot, kind)


# Every kind this consolidator reads, in the order it reads them from each package.
_PARSERS: Final[Mapping[str, Callable[[Mapping[str, object]], object]]] = {
    TIMESTAMP_DOMAIN: clock,
    **{kind: _chain_parser(kind) for kind in CHAIN_KINDS},
    AUTHORISATION_ENVELOPE: parse.envelope,
    RUN: parse.run,
    **{kind: _snapshot_parser(kind) for kind in SNAPSHOT_KINDS},
    SNAPSHOT_BINDING: parse.binding,
}


@dataclass
class _View:
    events: list[Event] = field(default_factory=list)  # each list sorted by record id
    envelopes: list[Envelope] = field(default_factory=list)
    runs: list[RunRecord] = field(default_factory=list)
    bindings: list[Binding] = field(default_factory=list)
    snapshots: dict[RecordId, Snapshot] = field(default_factory=dict)
    clocks: dict[RecordId, CivilClock] = field(default_factory=dict)
    nodes: set[NodeRef] = field(default_factory=set)
    anchors: dict[tuple[NodeType, bytes], set[NodeRef]] = field(default_factory=dict)
    starts: dict[NodeRef, Timestamp] = field(default_factory=dict)
    findings: list[ConsolidationFinding] = field(default_factory=list)

    def place(self, stamp: Timestamp) -> Timestamp:
        """A stamp on a clock that declares itself civil, on that ``CivilClock`` (ADR 0002 §3)."""
        civil = self.clocks.get(stamp.domain_id)
        return stamp if civil is None else civil.at(stamp.ticks)

    def declared(self, node_type: NodeType, node: LogicalId, record: RecordId) -> NodeRef | None:
        """The node a declared id names; a finding if no Ledger thread declares it."""
        ref = node_ref(node_type, node)
        if ref in self.nodes:
            return ref
        self.findings.append(
            _finding(
                "unthreaded_id",
                f"a record names a {node_type} id no Ledger thread declares; not placed",
                (record,),
                node_type=str(node_type),
                logical_id=node.to_json(),
            )
        )
        return None

    def anchored(self, node_type: NodeType, anchor: EvidenceRef) -> tuple[NodeRef, ...]:
        """The nodes whose threads cite ``anchor``: a record's thread when it declares no id."""
        return tuple(
            sorted(self.anchors.get((node_type, _ref_key(anchor)), ()), key=lambda n: n.node_id)
        )


def _read(ledger: LedgerReader) -> _View:
    """Parse every record this consolidator reads, in every package, de-duplicated by id."""
    view = _View()
    seen: dict[RecordId, object] = {}
    conflicted: set[RecordId] = set()
    for ref in ledger.list_packages():
        for kind, parser in _PARSERS.items():
            for index, record in enumerate(ledger.read_records(ref.package_id, kind) or ()):
                try:
                    parsed = parser(record)
                except Inferred:
                    view.findings.append(
                        _finding(
                            "inferred_record",
                            f"{kind} record {index} in package {ref.package_id!r} is inferred: a"
                            " derived/ record, never a ground",
                            severity=Severity.INFO,
                            index=index,
                            kind=kind,
                            package_id=ref.package_id,
                        )
                    )
                    continue
                except Malformed as exc:
                    view.findings.append(
                        _finding(
                            "malformed_record",
                            f"{kind} record {index} in package {ref.package_id!r} is malformed: "
                            f"{_safe(str(exc))}"[:1000],
                            severity=Severity.ERROR,
                            index=index,
                            kind=kind,
                            package_id=ref.package_id,
                        )
                    )
                    continue
                rid: RecordId = parsed.record  # type: ignore[attr-defined]
                if rid in conflicted:
                    continue
                if seen.setdefault(rid, parsed) != parsed:
                    conflicted.add(rid)
                    view.findings.append(
                        _finding(
                            "record_conflict",
                            "one record id carries different content in two places; not used",
                            (rid,),
                            Severity.ERROR,
                        )
                    )
    for rid in sorted(seen.keys() - conflicted):
        parsed = seen[rid]
        if isinstance(parsed, Event):
            view.events.append(parsed)
        elif isinstance(parsed, Envelope):
            view.envelopes.append(parsed)
        elif isinstance(parsed, RunRecord):
            view.runs.append(parsed)
        elif isinstance(parsed, Binding):
            view.bindings.append(parsed)
        elif isinstance(parsed, Snapshot):
            view.snapshots[rid] = parsed
        elif isinstance(parsed, Clock) and parsed.civil is not None:
            view.clocks[rid] = parsed.civil
    for node, threads in node_threads(ledger).items():
        view.nodes.add(node)
        view.starts[node] = threads[0].valid_from
        for thread in threads:
            for cited in thread.evidence:
                view.anchors.setdefault((node.node_type, _ref_key(cited)), set()).add(node)
    return view


# --- Machine chains -----------------------------------------------------------------------------

StateKind = Literal["decided", "candidates", "unknown"]


@dataclass(frozen=True)
class _State:
    """What a machine's configuration is from an instant: one node, several readings, or unknown."""

    kind: StateKind
    configurations: tuple[NodeRef, ...] = ()  # one when decided; sorted readings otherwise


_UNKNOWN: Final = _State("unknown")


@dataclass(frozen=True)
class _Step:
    event: Event
    at: Timestamp  # placed on a civil clock where its domain declares one
    state: _State


@dataclass
class _Span:
    state: _State
    start: Timestamp
    instants: list[list[_Step]]
    end: Timestamp | Open = OPEN

    @property
    def steps(self) -> list[_Step]:
        return [step for instant in self.instants for step in instant]


def _event_state(view: _View, event: Event) -> _State | None:
    """The state an event states, or ``None`` when it states no configuration at all."""
    match event.outcome:
        case "absent":
            return None
        case "unknown":
            return _UNKNOWN
        case "known":
            node = view.declared(NodeType.CONFIGURATION, event.configuration[0], event.record)
            return _UNKNOWN if node is None else _State("decided", (node,))
        case "ambiguous":
            found = [
                view.declared(NodeType.CONFIGURATION, c, event.record) for c in event.configuration
            ]
            readings = sorted({n for n in found if n is not None}, key=lambda n: n.node_id)
            return _State("candidates", tuple(readings)) if readings else _UNKNOWN


def _steps(view: _View) -> dict[NodeRef, list[_Step]]:
    """Each machine's steps, from every placeable event."""
    steps: dict[NodeRef, list[_Step]] = {}
    for event in view.events:
        record = (event.record,)
        if event.at is None:
            view.findings.append(
                _finding("untimed_record", f"a {event.kind} states no instant; not placed", record)
            )
            continue
        if event.machines is None or not (event.machines or event.unread_machines):
            view.findings.append(
                _finding(
                    "unplaced_record",
                    f"a {event.kind} names no Known machine; not placed on a chain",
                    record,
                )
            )
            continue
        if event.unread_machines:
            view.findings.append(
                _finding(
                    "unplaced_record",
                    f"a {event.kind} names {event.unread_machines} machine(s) not stated as Known;"
                    " those are not placed",
                    record,
                    unread=event.unread_machines,
                )
            )
        state = _event_state(view, event)
        if state is None:
            continue
        at = view.place(event.at)
        for machine_id in event.machines:
            machine = view.declared(NodeType.MACHINE, machine_id, event.record)
            if machine is not None:
                steps.setdefault(machine, []).append(_Step(event, at, state))
    return steps


def _instant_state(view: _View, machine: NodeRef, instant: list[_Step]) -> _State:
    """What one instant's records state together; disagreement is ambiguity, never a pick."""
    decided = {s.state.configurations[0] for s in instant if s.state.kind == "decided"}
    readings = {c for s in instant if s.state.kind == "candidates" for c in s.state.configurations}
    unknown = any(s.state.kind == "unknown" for s in instant)
    if len(decided) == 1 and not readings and not unknown:
        return _State("decided", tuple(decided))
    if not decided and not readings:
        return _UNKNOWN
    if len({(s.state.kind, s.state.configurations) for s in instant}) > 1:
        view.findings.append(
            _finding(
                "chain_overlap",
                "records place different configurations on one machine at one instant; each is"
                " a candidate",
                (s.event.record for s in instant),
                machine=machine.node_id,
                at=instant[0].at.to_json(),
            )
        )
    return _State("candidates", tuple(sorted(decided | readings, key=lambda n: n.node_id)))


def _spans(view: _View, machine: NodeRef, steps: list[_Step]) -> list[_Span]:
    """One clock's steps as spans of equal state, each until the next span starts."""
    ordered = sorted(steps, key=lambda s: (s.at.ticks, s.event.record))
    instants: list[list[_Step]] = []
    for step in ordered:
        if instants and instants[-1][0].at == step.at:
            instants[-1].append(step)
        else:
            instants.append([step])
    spans: list[_Span] = []
    for instant in instants:
        state = _instant_state(view, machine, instant)
        if spans and spans[-1].state == state:
            spans[-1].instants.append(instant)
            continue
        if spans:
            spans[-1].end = instant[0].at
        spans.append(_Span(state, instant[0].at, [instant]))
    return spans


def _draft(
    subject: NodeRef,
    predicate: str,
    obj: ClaimObject,
    start: Timestamp,
    end: Timestamp | Open,
    kind: AssertionKind,
    evidence: Iterable[EvidenceRef],
    records: Iterable[RecordId],
) -> ClaimDraft:
    return ClaimDraft(
        subject=subject,
        predicate=predicate,
        object=obj,
        valid_from=start,
        valid_to=end,
        assertion_kind=kind,
        evidence=tuple(evidence),
        records=tuple(records),
    )


def _cites(steps: Iterable[_Step]) -> tuple[list[EvidenceRef], list[RecordId]]:
    listed = list(steps)
    return [ref for s in listed for ref in s.event.evidence], [s.event.record for s in listed]


def _chain_drafts(view: _View, machine: NodeRef, spans: list[_Span]) -> list[ClaimDraft]:
    drafts: list[ClaimDraft] = []
    previous: _Span | None = None
    for span in spans:
        evidence, records = _cites(span.steps)
        if span.state.kind == "decided":
            (configuration,) = span.state.configurations
            drafts.append(
                _draft(
                    machine,
                    HAS_CONFIGURATION,
                    configuration,
                    span.start,
                    span.end,
                    _STATED,
                    evidence,
                    records,
                )
            )
            if previous is not None and previous.state.kind == "decided":
                cited, cited_records = _cites([*previous.instants[-1], *span.instants[0]])
                drafts.append(
                    _draft(
                        configuration,
                        SUCCEEDS,
                        previous.state.configurations[0],
                        span.start,
                        span.end,
                        _STATED,
                        cited,
                        cited_records,
                    )
                )
        elif span.state.kind == "unknown":
            view.findings.append(
                _finding(
                    "chain_gap",
                    "a record leaves a machine's configuration unknown from its instant; no"
                    " succession is claimed across it",
                    records,
                    machine=machine.node_id,
                    valid=_span(span.start, span.end),
                )
            )
            drafts.append(
                _draft(
                    machine,
                    CONFIGURATION_UNKNOWN,
                    LedgerRecordRef(min(records)),
                    span.start,
                    span.end,
                    _STATED,
                    evidence,
                    records,
                )
            )
        else:
            for reading in span.state.configurations:
                naming = [s for s in span.steps if reading in s.state.configurations]
                cited, cited_records = _cites(naming)
                drafts.append(
                    _draft(
                        machine,
                        CONFIGURATION_CANDIDATE,
                        reading,
                        span.start,
                        span.end,
                        _STATED,
                        cited,
                        cited_records,
                    )
                )
        previous = span
    return drafts


def _chains(view: _View) -> list[ClaimDraft]:
    drafts: list[ClaimDraft] = []
    for machine, steps in sorted(_steps(view).items(), key=lambda item: item[0].node_id):
        clocks: dict[RecordId, list[_Step]] = {}
        for step in steps:
            clocks.setdefault(step.at.domain_id, []).append(step)
        if len(clocks) > 1:
            view.findings.append(
                _finding(
                    "clock_split",
                    "a machine's records state instants on clocks Memory cannot compare; each"
                    " clock is a chain of its own",
                    (s.event.record for s in steps),
                    machine=machine.node_id,
                    clocks=sorted(clocks),
                )
            )
        for domain in sorted(clocks):
            drafts.extend(_chain_drafts(view, machine, _spans(view, machine, clocks[domain])))
    return drafts


# --- Authorisation ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Window:
    """An envelope's placed window: ``None`` when it cannot be placed. ``stated`` is ``False``
    when ``valid_until`` is not stated: the window is then ``[valid_from, open)`` as far as it may
    reach, never as far as it is known to (ADR 0010 §5)."""

    envelope: Envelope
    interval: Interval | None
    stated: bool = True


@dataclass
class _Authorisations:
    """Envelope windows by the configuration they name, by each candidate of an ``Ambiguous``
    one, and those naming none."""

    naming: dict[NodeRef, list[_Window]] = field(default_factory=dict)
    maybe: dict[NodeRef, list[_Window]] = field(default_factory=dict)
    unbound: list[_Window] = field(default_factory=list)


def _envelope_window(view: _View, envelope: Envelope) -> _Window:
    """``[valid_from, valid_until)`` placed; ``None`` (and a finding) if it cannot be."""
    if envelope.valid_from is None:
        view.findings.append(
            _finding(
                "envelope_unplaced",
                "an authorisation envelope states no valid_from; it places nothing in time",
                (envelope.record,),
            )
        )
        return _Window(envelope, None)
    start = view.place(envelope.valid_from)
    until = envelope.valid_until
    if until == "unstated":
        view.findings.append(
            _finding(
                "envelope_unplaced",
                "an authorisation envelope states no valid_until (nor that it has none); where it"
                " ends is not known, so no site claim and coverage after valid_from is undecided",
                (envelope.record,),
            )
        )
        return _Window(envelope, Interval(start, OPEN), stated=False)
    end = OPEN if until == "open" else view.place(until)
    if isinstance(end, Timestamp) and (end.domain_id != start.domain_id or not start < end):
        view.findings.append(
            _finding(
                "untimeable_window",
                "an authorisation envelope's valid_until is not after its valid_from on its clock",
                (envelope.record,),
            )
        )
        return _Window(envelope, None)
    return _Window(envelope, Interval(start, end))


def _authorisations(view: _View) -> tuple[list[ClaimDraft], _Authorisations]:
    drafts: list[ClaimDraft] = []
    found = _Authorisations()
    for envelope in view.envelopes:
        record = (envelope.record,)
        window = _envelope_window(view, envelope)
        if envelope.outcome == "ambiguous":
            for candidate in envelope.configuration:
                node = view.declared(NodeType.CONFIGURATION, candidate, envelope.record)
                if node is not None:
                    found.maybe.setdefault(node, []).append(window)
            view.findings.append(
                _finding(
                    "envelope_unplaced",
                    "an authorisation envelope's configuration is Ambiguous; it authorises no"
                    " one configuration",
                    record,
                    candidates=parse.ids_json(envelope.configuration),
                )
            )
            continue
        if envelope.outcome != "known":
            found.unbound.append(window)
            view.findings.append(
                _finding(
                    "envelope_unplaced",
                    "an authorisation envelope names no configuration; coverage over its window"
                    " is undecided",
                    record,
                )
            )
            continue
        configuration = view.declared(
            NodeType.CONFIGURATION, envelope.configuration[0], envelope.record
        )
        if configuration is None:
            continue
        found.naming.setdefault(configuration, []).append(window)
        if envelope.site is None:
            view.findings.append(
                _finding(
                    "envelope_unplaced",
                    "an authorisation envelope names no Known site; no site claim",
                    record,
                    Severity.INFO,
                )
            )
            continue
        site = view.declared(NodeType.SITE, envelope.site, envelope.record)
        if site is None or window.interval is None or not window.stated:
            continue
        drafts.append(
            _draft(
                site,
                AUTHORISED_CONFIGURATION,
                configuration,
                window.interval.start,
                window.interval.end,
                _STATED,
                envelope.evidence,
                record,
            )
        )
    return drafts, found


def _undecided(
    view: _View,
    found: _Authorisations,
    run: RunRecord,
    node: NodeRef,
    binding: Binding,
    configuration: NodeRef,
    reason: str,
    envelopes: Iterable[RecordId] = (),
) -> None:
    naming = [w.envelope.record for w in found.naming.get(configuration, [])]
    view.findings.append(
        _finding(
            "authorisation_undecided",
            f"whether an authorisation envelope covers this run's configuration is not decided:"
            f" {reason}; nothing is claimed",
            (binding.record, run.record, *envelopes, *naming),
            run=node.node_id,
            configuration=configuration.node_id,
            envelopes_naming_it=len(naming),
        )
    )


def _coverage(
    view: _View,
    found: _Authorisations,
    run: RunRecord,
    node: NodeRef,
    binding: Binding,
    configuration: NodeRef,
    window: Interval,
) -> list[ClaimDraft]:
    """The parts of ``window`` (stated bounds only) no envelope naming ``configuration`` covers,
    as observed claims.

    Compared only on one clock. Where an envelope naming it cannot be compared (another clock, no
    start), one that might cover an uncovered part (``Ambiguous``, naming none, or with no stated
    end) exists, or an open run window is only partly covered, the coverage is undecided: a
    finding, never a claim (ADR 0010 §5).
    """
    naming = found.naming.get(configuration, [])
    comparable: list[Interval] = []
    undecided: list[RecordId] = []
    maybe = [*found.maybe.get(configuration, []), *found.unbound]
    for item in naming:
        if item.interval is None or item.interval.domain_id != window.domain_id:
            undecided.append(item.envelope.record)
        elif item.stated:
            comparable.append(item.interval)
        else:
            maybe.append(item)  # it covers from valid_from up to an end it does not state
    pieces = window.minus(comparable)
    if not pieces:
        return []
    for item in maybe:
        if (
            item.interval is None
            or item.interval.domain_id != window.domain_id
            or any(item.interval.overlaps(piece) for piece in pieces)
        ):
            undecided.append(item.envelope.record)
    if undecided:
        _undecided(
            view,
            found,
            run,
            node,
            binding,
            configuration,
            "an envelope that may cover it cannot be compared on one clock or states no end",
            undecided,
        )
        return []
    # An open run window may end at any instant, so only a run no envelope covers at any part of
    # it is surely uncovered; a closed piece of an open window would overstate it.
    if isinstance(window.end, Open) and pieces != (window,):
        _undecided(view, found, run, node, binding, configuration, "the run's end is not stated")
        return []
    envelopes = [item.envelope for item in naming]
    return [
        _draft(
            node,
            NOT_COVERED_BY_AUTHORISATION,
            configuration,
            piece.start,
            piece.end,
            _OBSERVED,
            (*binding.evidence, *(ref for e in envelopes for ref in e.evidence)),
            (binding.record, run.record, *(e.record for e in envelopes)),
        )
        for piece in pieces
    ]


# --- Runs ---------------------------------------------------------------------------------------


def _run_node(view: _View, run: RunRecord) -> NodeRef | None:
    """A run's node: by its declared logical id, else by its anchored thread (Ledger ADR 0003)."""
    if run.logical_id is not None:
        return view.declared(NodeType.RUN, run.logical_id, run.record)
    nodes = view.anchored(NodeType.RUN, run.anchor)
    if len(nodes) == 1:
        return nodes[0]
    view.findings.append(
        _finding(
            "unthreaded_id" if not nodes else "ambiguous_anchor",
            "a run declares no logical id and "
            + ("no run thread cites its evidence" if not nodes else "several run threads cite it"),
            (run.record,),
            nodes=[n.node_id for n in nodes],
        )
    )
    return None


def _run_window(view: _View, run: RunRecord, node: NodeRef) -> Interval:
    """The run's own ``[first, last + 1 tick)``: ``first`` else its thread's start (a convention,
    ADR 0008 §2); open where ``last`` is not stated on the start's clock."""
    start = view.place(run.first) if run.first is not None else view.place(view.starts[node])
    if run.last is not None:
        last = view.place(run.last)
        end = Timestamp(last.ticks + 1, last.domain_id)
        if end.domain_id == start.domain_id:
            if start < end:
                return Interval(start, end)
            view.findings.append(
                _finding(
                    "untimeable_window",
                    "a run states its last instant before its first; its end is not placed",
                    (run.record,),
                )
            )
    return Interval(start, OPEN)


def _binding_window(
    view: _View, binding: Binding, run: RunRecord, run_window: Interval, bounds: tuple[Bound, Bound]
) -> tuple[Interval, bool] | ConsolidationFinding:
    """One window of a binding over its run, and whether its bounds are stated.

    A bound the binding states is used as stated; a stated open bound (``KnownAbsent``) is the
    run's own; an unstated one is the run's own too, so the claim names the run, but coverage is
    never decided over it (ADR 0010 §3). The run's start counts as stated only if ``first`` is.
    """
    start_bound, end_bound = bounds
    if isinstance(start_bound, Timestamp):
        start, start_stated = view.place(start_bound), True
    else:
        start, start_stated = run_window.start, start_bound == "open" and run.first is not None
    end: Timestamp | Open
    if isinstance(end_bound, Timestamp):
        end, end_stated = view.place(end_bound), True
    else:
        end, end_stated = run_window.end, end_bound == "open"
        if isinstance(end, Timestamp) and end.domain_id != start.domain_id:
            end = OPEN  # the run's end is on another clock: until further notice
    if isinstance(end, Timestamp) and (end.domain_id != start.domain_id or not start < end):
        return _finding(
            "untimeable_window",
            "a snapshot binding's window cannot be placed: its end is not after its start on"
            " one clock",
            (binding.record,),
        )
    return Interval(start, end), start_stated and end_stated


@dataclass(frozen=True)
class _Bound:
    binding: Binding
    configuration: NodeRef
    window: Interval
    stated: bool  # both bounds stated (or stated open): coverage may be decided over it


def _overlapping(bound: list[_Bound]) -> set[RecordId]:
    """Bindings of one kind naming different configurations over overlapping windows."""
    flagged: set[RecordId] = set()
    for i, a in enumerate(bound):
        for b in bound[i + 1 :]:
            if (
                a.binding.snapshot_kind == b.binding.snapshot_kind
                and a.configuration != b.configuration
                and a.window.domain_id == b.window.domain_id
                and a.window.overlaps(b.window)
            ):
                flagged |= {a.binding.record, b.binding.record}
    return flagged


def _runs(view: _View, found: _Authorisations) -> list[ClaimDraft]:
    drafts: list[ClaimDraft] = []
    by_run: dict[RecordId, list[Binding]] = {}
    for binding in view.bindings:
        by_run.setdefault(binding.run, []).append(binding)
    runs = {run.record: run for run in view.runs}
    for rid in sorted(by_run.keys() - runs.keys()):
        view.findings.append(
            _finding(
                "dangling_binding",
                "a snapshot binding names a run record the Ledger does not hold",
                (b.record for b in by_run[rid]),
                run=rid,
            )
        )
    for run in view.runs:
        node = _run_node(view, run)
        if node is None:
            continue
        run_window = _run_window(view, run, node)
        bindings = by_run.get(run.record, [])
        if not bindings:
            drafts.append(
                _draft(
                    node,
                    CONFIGURATION_UNKNOWN,
                    LedgerRecordRef(run.record),
                    run_window.start,
                    run_window.end,
                    _OBSERVED,
                    run.evidence,
                    (run.record,),
                )
            )
            continue
        bound: list[_Bound] = []
        for binding in bindings:
            drafts.extend(_bind(view, run, node, binding, run_window, bound))
        flagged = _overlapping(bound)
        if flagged:
            view.findings.append(
                _finding(
                    "binding_overlap",
                    "bindings of one snapshot kind name different configurations over"
                    " overlapping parts of a run; each is a candidate",
                    sorted(flagged),
                    run=node.node_id,
                )
            )
        for item in bound:
            binding = item.binding
            snapshot = view.snapshots[binding.snapshot]
            drafts.append(
                _draft(
                    node,
                    CONFIGURATION_CANDIDATE
                    if binding.record in flagged
                    else CONFIGURATION_ACTIVE_DURING,
                    item.configuration,
                    item.window.start,
                    item.window.end,
                    binding.assertion_kind,
                    binding.evidence,
                    (binding.record, run.record, snapshot.record),
                )
            )
            if binding.record in flagged:
                continue
            if item.stated:
                drafts.extend(
                    _coverage(view, found, run, node, binding, item.configuration, item.window)
                )
            else:
                _undecided(
                    view,
                    found,
                    run,
                    node,
                    binding,
                    item.configuration,
                    "the binding does not state the part of the run it applies to",
                )
    return drafts


def _bind(
    view: _View,
    run: RunRecord,
    node: NodeRef,
    binding: Binding,
    run_window: Interval,
    bound: list[_Bound],
) -> list[ClaimDraft]:
    """One binding: its configuration node into ``bound``, or what it leaves unknown."""
    windows: list[tuple[Interval, bool]] = []
    for bounds in binding.windows:
        placed = _binding_window(view, binding, run, run_window, bounds)
        if isinstance(placed, ConsolidationFinding):
            view.findings.append(placed)
        else:
            windows.append(placed)
    if not windows:
        return []
    snapshot = view.snapshots.get(binding.snapshot)
    configurations: tuple[NodeRef, ...] = ()
    if snapshot is None or snapshot.kind != binding.snapshot_kind:
        details: dict[str, JsonValue] = {"snapshot": binding.snapshot}
        held = ""
        if snapshot is not None:
            details["held_as"] = snapshot.kind
            held = f"; the Ledger holds it as a {snapshot.kind}"
        view.findings.append(
            _finding(
                "dangling_binding",
                f"a snapshot binding names a {binding.snapshot_kind} record the Ledger does not"
                f" hold{held}",
                (binding.record,),
                Severity.WARNING,
                **details,
            )
        )
    else:
        configurations = view.anchored(NodeType.CONFIGURATION, snapshot.anchor)
        if len(configurations) != 1:
            view.findings.append(
                _finding(
                    "unthreaded_id" if not configurations else "ambiguous_anchor",
                    f"a bound {snapshot.kind} has "
                    + ("no configuration thread" if not configurations else "several threads"),
                    (binding.record, snapshot.record),
                    nodes=[n.node_id for n in configurations],
                )
            )
    if binding.ambiguous:
        view.findings.append(
            _finding(
                "ambiguous_window",
                "a snapshot binding's window is Ambiguous: each reading is a candidate, and"
                " coverage is not decided",
                (binding.record,),
                readings=len(binding.windows),
            )
        )
    elif len(configurations) == 1:
        window, stated = windows[0]
        bound.append(_Bound(binding, configurations[0], window, stated))
        return []
    records = (binding.record, run.record, *(() if snapshot is None else (snapshot.record,)))
    if configurations:
        return [
            _draft(
                node,
                CONFIGURATION_CANDIDATE,
                configuration,
                window.start,
                window.end,
                binding.assertion_kind,
                binding.evidence,
                records,
            )
            for configuration in configurations
            for window, _ in windows
        ]
    return [
        _draft(
            node,
            CONFIGURATION_UNKNOWN,
            LedgerRecordRef(binding.record),
            window.start,
            window.end,
            binding.assertion_kind,
            binding.evidence,
            records,
        )
        for window, _ in windows
    ]


# --- The consolidator ---------------------------------------------------------------------------


class ConfigurationLineageConsolidator:
    """Machine chains, run configurations and authorisation coverage. Takes no configuration."""

    consolidator_id: Final = CONFIGURATION_CONSOLIDATOR_ID
    version: Final = "1"
    model: Final[ModelRef | None] = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        view = _read(ledger)
        if config:
            view.findings.append(
                _finding(
                    "unknown_config",
                    "the configuration lineage consolidator takes no configuration; keys ignored",
                    keys=sorted(config),
                )
            )
        drafts = _chains(view)
        authorised, found = _authorisations(view)
        drafts.extend(authorised)
        drafts.extend(_runs(view, found))
        return ConsolidatorOutput(tuple(drafts), tuple(view.findings))
