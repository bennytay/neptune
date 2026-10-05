"""Calibration history and drift as a deterministic consolidator (ADR 0014).

Four things, each from declared records only, each cited:

- **Placement.** A ``Calibration`` names a machine (a declared id: the identity chain) and a
  subject (a declared name). The machine's hardware configurations are those that declare it,
  and those its configuration chain (``memory.configuration``'s ``has_configuration`` and
  ``configuration_candidate`` claims) places on it at the calibration's instant. Within them, the
  sensor components the subject names are the sensor; their declared identifiers are its nodes.
  Anything less than one sensor, reached only through stated values, is a candidate or a finding.
- **Frame-graph consistency.** A calibration whose ``FrameBinding`` names an edge of the sensor's
  configuration graph that the graph does not declare is ``Ambiguous``: a ``calibration_candidate``
  citing both the binding and the description's own edge.
- **History.** Per sensor and shape (its parameter names and bound edges), calibrations are ordered
  by their instant on one clock. Each is ``calibrated_with`` from its stated ``valid_from`` to its
  stated ``valid_until``, or else until the next calibration of the series; ties are candidates.
- **Drift.** Between consecutive calibrations of a series, the component-wise differences of the
  numbers both declare in the same form, interpretation and declared unit: ``drift`` claims, with
  both records as evidence. Different units, unstated units or interpretations are findings; no
  value is converted, and no threshold is applied.

``calibrated_by`` links a calibration to the maintenance or requalification record whose
``configuration`` names it. Records are parsed by ``consolidate.calibration_records``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final, Literal, TypeAlias

from neptune.identity import canonical_json
from neptune.model.finding import Severity
from neptune.model.frames import (
    EulerAngles,
    HomogeneousMatrix,
    MatrixLayout,
    Pose,
    Quaternion,
    RotationMatrix,
    RotationVector,
)
from neptune.model.knowledge import Ambiguous, AssertionKind, Known, KnownAbsent, NotApplicable
from neptune.model.time import Timestamp
from neptune_memory.consolidate import calibration_records as parse
from neptune_memory.consolidate.base import (
    ClaimDraft,
    ConsolidationFinding,
    ConsolidatorOutput,
    ModelRef,
)
from neptune_memory.consolidate.calibration_records import (
    CALIBRATION,
    FRAME_BINDING,
    FRAME_TRANSFORM,
    HARDWARE_COMPONENT,
    HARDWARE_CONFIGURATION,
    PRODUCER_KINDS,
    Binding,
    CalibrationRecord,
    Component,
    Configuration,
    Producer,
    Readings,
    Transform,
)
from neptune_memory.consolidate.configuration import (
    CONFIGURATION_CONSOLIDATOR_ID,
    HAS_CONFIGURATION,
)
from neptune_memory.consolidate.identity import node_ref, node_threads
from neptune_memory.consolidate.identity_records import (
    TIMESTAMP_DOMAIN,
    Clock,
    Inferred,
    Malformed,
    clock,
)
from neptune_memory.schema.claim import (
    MAX_DELTA_VALUES,
    Delta,
    DeltaQuantity,
    LedgerRecordRef,
    TypedLiteral,
    ValueType,
)
from neptune_memory.schema.interval import OPEN, CivilClock, Interval, Open
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import (
    CALIBRATED_BY,
    CALIBRATED_WITH,
    CALIBRATION_CANDIDATE,
    CONFIGURATION_CANDIDATE,
    DRIFT,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from neptune.model.frames import FrameRef, Rotation, TransformValue
    from neptune.model.ids import LogicalId, RecordId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune.model.machine import CalibrationParameter
    from neptune.model.provenance import EvidenceRef
    from neptune.model.units import Unit
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim

CALIBRATION_CONSOLIDATOR_ID: Final = "memory.calibration"

_STATED, _OBSERVED = AssertionKind.STATED, AssertionKind.OBSERVED
# A homogeneous matrix's entries, by declared layout: the rotation block is the same nine
# positions in either layout (in that layout's order); the translation is the last column.
_MATRIX_ROTATION: Final = (0, 1, 2, 4, 5, 6, 8, 9, 10)
_MATRIX_TRANSLATION: Final = {
    MatrixLayout.ROW_MAJOR: (3, 7, 11),
    MatrixLayout.COLUMN_MAJOR: (12, 13, 14),
}

Edge = tuple["FrameRef", "FrameRef"]


def _finding(
    code: str,
    message: str,
    records: Iterable[RecordId] = (),
    severity: Severity = Severity.WARNING,
    **details: JsonValue,
) -> ConsolidationFinding:
    return ConsolidationFinding(
        code=f"calibration.{code}",
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


def _key(value: object) -> bytes:
    return canonical_json.dumps(value.to_json())  # type: ignore[attr-defined]


def _edge_key(edge: Edge) -> bytes:
    return canonical_json.dumps([edge[0].to_json(), edge[1].to_json()])


def _edge_json(edge: Edge) -> JsonValue:
    return {"child": edge[1].to_json(), "parent": edge[0].to_json()}


# --- Reading the Ledger -------------------------------------------------------------------------


def _producer_parser(kind: str) -> Callable[[Mapping[str, object]], object]:
    return partial(parse.producer, kind)


# Every kind this consolidator reads, in the order it reads them from each package.
_PARSERS: Final[Mapping[str, Callable[[Mapping[str, object]], object]]] = {
    TIMESTAMP_DOMAIN: clock,
    CALIBRATION: parse.calibration,
    HARDWARE_CONFIGURATION: parse.hardware_configuration,
    HARDWARE_COMPONENT: parse.hardware_component,
    FRAME_TRANSFORM: parse.frame_transform,
    FRAME_BINDING: parse.frame_binding,
    **{kind: _producer_parser(kind) for kind in PRODUCER_KINDS},
}


@dataclass(frozen=True)
class _Placed:
    """A machine's configuration over an interval, as ``memory.configuration`` claimed it."""

    configuration: NodeRef
    interval: Interval
    decided: bool  # has_configuration; a configuration_candidate is one reading
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]


@dataclass
class _View:
    calibrations: list[CalibrationRecord] = field(default_factory=list)  # sorted by record id
    configurations: list[Configuration] = field(default_factory=list)
    anchor_keys: dict[RecordId, bytes] = field(default_factory=dict)  # by configuration record
    components: dict[RecordId, list[Component]] = field(default_factory=dict)  # by configuration
    transforms: dict[RecordId, Transform] = field(default_factory=dict)
    bindings: dict[RecordId, list[Binding]] = field(default_factory=dict)  # by calibration
    producers: list[Producer] = field(default_factory=list)
    clocks: dict[RecordId, CivilClock] = field(default_factory=dict)
    nodes: set[NodeRef] = field(default_factory=set)
    anchors: dict[tuple[NodeType, bytes], set[NodeRef]] = field(default_factory=dict)
    cites: dict[NodeRef, set[bytes]] = field(default_factory=dict)  # evidence its threads cite
    graph_frames: dict[RecordId, set[FrameRef]] = field(default_factory=dict)
    parents: dict[FrameRef, list[Transform]] = field(default_factory=dict)  # by declared child
    chains: dict[NodeRef, list[_Placed]] = field(default_factory=dict)
    findings: list[ConsolidationFinding] = field(default_factory=list)

    def place(self, stamp: Timestamp) -> Timestamp:
        """A stamp on a clock that declares itself civil, on that ``CivilClock`` (ADR 0002 §3)."""
        civil = self.clocks.get(stamp.domain_id)
        return stamp if civil is None else civil.at(stamp.ticks)

    def node(self, node_type: NodeType, logical_id: LogicalId) -> NodeRef | None:
        ref = node_ref(node_type, logical_id)
        return ref if ref in self.nodes else None

    def anchored(self, node_type: NodeType, anchor: EvidenceRef) -> tuple[NodeRef, ...]:
        found = self.anchors.get((node_type, _key(anchor)), ())
        return tuple(sorted(found, key=lambda n: n.node_id))


def _read(ledger: LedgerReader, previous: Sequence[Claim]) -> _View:
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
                if parsed is None:  # a binding of another basis
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
        _admit(view, seen[rid])
    for node, threads in node_threads(ledger).items():
        view.nodes.add(node)
        for thread in threads:
            for cited in thread.evidence:
                view.anchors.setdefault((node.node_type, _key(cited)), set()).add(node)
                view.cites.setdefault(node, set()).add(_key(cited))
    _chains(view, previous)
    return view


def _admit(view: _View, parsed: object) -> None:
    if isinstance(parsed, CalibrationRecord):
        view.calibrations.append(parsed)
    elif isinstance(parsed, Configuration):
        view.configurations.append(parsed)
        view.anchor_keys[parsed.record] = _key(parsed.anchor)
    elif isinstance(parsed, Component):
        if parsed.frame is not None:  # any part's frame is a frame its graph declares
            view.graph_frames.setdefault(parsed.frame.frame_graph_id, set()).add(parsed.frame)
        if parsed.sensor:
            view.components.setdefault(parsed.configuration, []).append(parsed)
    elif isinstance(parsed, Transform):
        view.transforms[parsed.record] = parsed
        graph = parsed.parent.frame_graph_id
        view.graph_frames.setdefault(graph, set()).update((parsed.parent, parsed.child))
        view.parents.setdefault(parsed.child, []).append(parsed)
    elif isinstance(parsed, Binding):
        for calibration in parsed.calibrations:
            view.bindings.setdefault(calibration, []).append(parsed)
    elif isinstance(parsed, Producer):
        view.producers.append(parsed)
    elif isinstance(parsed, Clock) and parsed.civil is not None:
        view.clocks[parsed.record] = parsed.civil


def _chains(view: _View, previous: Sequence[Claim]) -> None:
    """Each machine's configuration chain, from ``memory.configuration``'s claims (ADR 0010 §2)."""
    for claim in previous:
        if (
            claim.provenance.consolidator_id != CONFIGURATION_CONSOLIDATOR_ID
            or claim.predicate not in (HAS_CONFIGURATION, CONFIGURATION_CANDIDATE)
            or claim.subject.node_type is not NodeType.MACHINE
            or not isinstance(claim.object, NodeRef)
        ):
            continue
        view.chains.setdefault(claim.subject, []).append(
            _Placed(
                configuration=claim.object,
                interval=claim.valid,
                decided=claim.predicate == HAS_CONFIGURATION,
                evidence=claim.provenance.evidence,
                records=claim.provenance.records,
            )
        )


# --- Placement: which sensor a calibration is of ------------------------------------------------


@dataclass(frozen=True)
class _Route:
    """One way a calibration reaches a sensor component; ``ambiguous`` when any step of it is one
    reading of an ``Ambiguous`` value or a chain candidate."""

    component: Component
    nodes: tuple[NodeRef, ...]  # the sensor threads its declared identifiers key
    ambiguous: bool
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]


@dataclass(frozen=True)
class _Placement:
    """Where a calibration is placed: on ``nodes`` (one sensor) when ``definite``, else each node
    is one reading. ``evidence`` and ``records`` are the routes' citations."""

    nodes: tuple[NodeRef, ...]
    definite: bool
    frames: tuple[FrameRef, ...]  # the sensor's declared frames, in its configurations' graphs
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]


def _instant(view: _View, calibration: CalibrationRecord) -> Timestamp | None:
    """Its world time, as the Ledger orders it: ``valid_from``, else ``performed`` (Ledger ADR
    0003 §3). Only a ``Known`` instant counts; anything else leaves it untimed."""
    for knowledge in (calibration.valid_from, calibration.performed):
        if isinstance(knowledge, Known):
            return view.place(knowledge.value)
    return None


def _machines(view: _View, calibration: CalibrationRecord) -> list[tuple[NodeRef, bool]]:
    ambiguous = calibration.machine == "ambiguous"
    found = [(view.node(NodeType.MACHINE, m), ambiguous) for m in calibration.machines]
    return [(node, flag) for node, flag in found if node is not None]


def _revision_fits(calibration: Readings, configuration: Readings) -> bool | None:
    """Whether a configuration's revision fits a calibration's: ``None`` (only some readings fit),
    ``False`` (no reading fits) or ``True``. A revision either side does not state fits."""
    if not calibration.values or not configuration.values:
        return True
    shared = set(calibration.values) & set(configuration.values)
    if not shared:
        return False
    if calibration.ambiguous or configuration.ambiguous:
        return None
    return True


_Found: TypeAlias = (
    "dict[RecordId, tuple[Configuration, bool, tuple[EvidenceRef, ...], tuple[RecordId, ...]]]"
)


def _configurations(
    view: _View, calibration: CalibrationRecord, machine: NodeRef, at: Timestamp | None
) -> _Found:
    """The hardware configurations of ``machine`` the calibration may apply to, each with whether
    it is only one reading, and the chain citations that place it.

    The configuration chain at the calibration's instant states which configuration the machine
    was in then; only where it places none that a hardware configuration is anchored on are the
    configurations that declare the machine (which state no time) read instead (ADR 0014 §2).
    """

    def add(
        found: _Found,
        configuration: Configuration,
        ambiguous: bool,
        evidence: tuple[EvidenceRef, ...] = (),
        records: tuple[RecordId, ...] = (),
    ) -> None:
        fits = _revision_fits(calibration.revision, configuration.revision)
        if fits is False:
            return
        ambiguous = ambiguous or fits is None
        held = found.get(configuration.record)
        if held is None or (held[1] and not ambiguous):
            found[configuration.record] = (configuration, ambiguous, evidence, records)

    chained: _Found = {}
    if at is not None:  # the configuration chain at the calibration's instant
        for placed in view.chains.get(machine, ()):
            if placed.interval.domain_id != at.domain_id or not placed.interval.contains(at):
                continue
            cited = view.cites.get(placed.configuration, set())
            for configuration in view.configurations:
                if view.anchor_keys[configuration.record] in cited:
                    add(chained, configuration, not placed.decided, placed.evidence, placed.records)
    if chained:
        return chained
    declared: _Found = {}
    for configuration in view.configurations:  # the identity chain: it declares the machine
        for candidate in configuration.machines:
            if node_ref(NodeType.MACHINE, candidate) == machine:
                add(declared, configuration, configuration.machine == "ambiguous")
    return declared


def _named(subject: Readings, name: Readings) -> bool | None:
    """Whether a component's name is the subject: ``True``, ``None`` (one reading), ``False``."""
    if not set(subject.values) & set(name.values):
        return False
    return None if subject.ambiguous or name.ambiguous else True


def _routes(view: _View, calibration: CalibrationRecord, at: Timestamp | None) -> list[_Route]:
    record = (calibration.record,)
    if not calibration.machines:
        view.findings.append(
            _finding("unplaced", "a calibration names no Known machine; not placed", record)
        )
        return []
    if not calibration.subject.values:
        view.findings.append(
            _finding("unplaced", "a calibration names no subject; not placed on a sensor", record)
        )
        return []
    machines = _machines(view, calibration)
    if not machines:
        view.findings.append(
            _finding(
                "unplaced",
                "a calibration names a machine no Ledger thread declares; not placed",
                record,
                machines=[m.to_json() for m in calibration.machines],
            )
        )
        return []
    routes: list[_Route] = []
    configured = False
    for machine, machine_ambiguous in machines:
        for configuration, ambiguous, evidence, records in _configurations(
            view, calibration, machine, at
        ).values():
            configured = True
            for component in view.components.get(configuration.record, ()):
                named = _named(calibration.subject, component.name)
                if named is False:
                    continue
                nodes = tuple(
                    node
                    for node in (view.node(NodeType.SENSOR, i) for i in component.identifiers)
                    if node is not None
                )
                routes.append(
                    _Route(
                        component=component,
                        nodes=tuple(sorted(set(nodes), key=lambda n: n.node_id)),
                        ambiguous=machine_ambiguous or ambiguous or named is None,
                        evidence=(*evidence, *configuration.evidence, *component.evidence),
                        records=(*records, configuration.record, component.record),
                    )
                )
    if not configured:
        view.findings.append(
            _finding(
                "no_configuration",
                "no hardware configuration of the calibration's machine is stated, by the machine"
                " or by its configuration chain at the calibration's instant; not placed",
                record,
                machines=[m.node_id for m, _ in machines],
                instant="unstated" if at is None else at.to_json(),
            )
        )
    elif not routes:
        view.findings.append(
            _finding(
                "sensor_not_in_configuration",
                "no sensor of the machine's hardware configurations has the calibration's"
                " subject as its declared name; not placed",
                record,
                subject=list(calibration.subject.values),
            )
        )
    return routes


def _placement(
    view: _View, calibration: CalibrationRecord, at: Timestamp | None
) -> _Placement | None:
    routes = _routes(view, calibration, at)
    if not routes:
        return None
    record = (calibration.record,)
    unthreaded = [r.component.record for r in routes if not r.nodes]
    if unthreaded:
        view.findings.append(
            _finding(
                "unthreaded_sensor",
                "a sensor the calibration names declares no identifier a Ledger sensor thread"
                " keys; it has no node",
                (*record, *unthreaded),
            )
        )
    nodes = tuple(sorted({n for r in routes for n in r.nodes}, key=lambda n: n.node_id))
    if not nodes:
        return None
    definite = not unthreaded and not any(r.ambiguous for r in routes)
    definite = definite and len({r.nodes for r in routes}) == 1
    if not definite:
        view.findings.append(
            _finding(
                "ambiguous_sensor",
                "the calibration could be of more than one sensor, or reaches its sensor only"
                " through an Ambiguous value; each is a candidate",
                (*record, *(r.component.record for r in routes)),
                sensors=[n.node_id for n in nodes],
            )
        )
    frames = {r.component.frame for r in routes if r.component.frame is not None}
    return _Placement(
        nodes=nodes,
        definite=definite,
        frames=tuple(sorted(frames, key=_key)),
        evidence=tuple(ref for r in routes for ref in r.evidence),
        records=tuple(rid for r in routes for rid in r.records),
    )


# --- Frame-graph consistency --------------------------------------------------------------------


def _known_bindings(view: _View, calibration: CalibrationRecord) -> list[Binding]:
    """The bindings that name this calibration as ``Known``; an ``Ambiguous`` one is a finding."""
    bindings = view.bindings.get(calibration.record, [])
    ambiguous = [b.record for b in bindings if b.ambiguous]
    if ambiguous:
        view.findings.append(
            _finding(
                "ambiguous_binding",
                "a frame binding names this calibration as one candidate; it is not read as the"
                " calibration's",
                (calibration.record, *ambiguous),
            )
        )
    return [b for b in bindings if not b.ambiguous]


def _disagreement(view: _View, binding: Binding, frame: FrameRef) -> list[Transform] | None:
    """The description's own edges a binding in the sensor's graph contradicts, or ``None``.

    It contradicts the graph when its edge does not touch the sensor's frame, when the graph
    declares no such frame on either end, or when the graph declares the child's parent and it is
    another frame. A graph that states nothing about the edge does not contradict it.
    """
    parent, child = binding.parent, binding.child
    frames = view.graph_frames.get(frame.frame_graph_id, set())
    declared = view.parents.get(child, [])
    if frame not in (parent, child):
        return view.parents.get(frame, [])
    if parent not in frames or child not in frames:
        return declared
    if declared and parent not in {t.parent for t in declared}:
        return declared
    return None


def _frame_check(
    view: _View, calibration: CalibrationRecord, placement: _Placement, bindings: list[Binding]
) -> tuple[list[EvidenceRef], list[RecordId]] | None:
    """What disagreeing bindings and the description's edges cite; ``None`` if none disagrees."""
    evidence: list[EvidenceRef] = []
    records: list[RecordId] = []
    for frame in placement.frames:
        for binding in bindings:
            if binding.parent.frame_graph_id != frame.frame_graph_id:
                continue  # an edge of another graph says nothing about this one (root ADR 0007)
            against = _disagreement(view, binding, frame)
            if against is None:
                continue
            evidence.extend((*binding.evidence, *(ref for t in against for ref in t.evidence)))
            records.extend((binding.record, *(t.record for t in against)))
            view.findings.append(
                _finding(
                    "frame_disagreement",
                    "a calibration's frame binding names an edge its sensor's configuration graph"
                    " does not declare; the calibration is a candidate, citing both",
                    (calibration.record, binding.record, *(t.record for t in against)),
                    edge=_edge_json((binding.parent, binding.child)),
                    sensor_frame=frame.to_json(),
                )
            )
    return (evidence, records) if records else None


# --- History ------------------------------------------------------------------------------------

# What follows a calibration in its series: the next definite calibration's stated valid_from,
# "unstated" when that one states none, or None when no definite calibration follows.
_Next: TypeAlias = "Timestamp | Literal['unstated'] | None"


@dataclass(frozen=True)
class _Entry:
    """A placed calibration: its instant, node and the citations that place it."""

    calibration: CalibrationRecord
    at: Timestamp | None
    configuration: NodeRef | None  # its anchored configuration thread's node
    placement: _Placement
    bindings: tuple[Binding, ...]  # Known, to edges of its sensor's configuration graphs
    frames_agree: bool
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]

    @property
    def candidate(self) -> bool:
        """Only one reading of where it applies: an ambiguous sensor or a contradicted frame."""
        return not self.placement.definite or not self.frames_agree

    @property
    def shape(self) -> tuple[str, ...]:
        """Its kind: the parameter names it declares (ADR 0014 §3). Edges are not part of it: a
        contradicted binding names another edge, and is still a calibration of the same kind."""
        return tuple(p.name for p in self.calibration.parameters)


def _windows(
    view: _View, entry: _Entry, after: _Next
) -> tuple[list[tuple[Timestamp, Timestamp | Open, tuple[EvidenceRef, ...]]], bool]:
    """The intervals a calibration is stated over, and whether they are readings of an
    ``Ambiguous`` end. ``after`` is the next definite calibration of its series: its stated
    ``valid_from``, ``"unstated"`` when it states none, or ``None`` when none follows.

    From a stated ``valid_from`` only; to its stated ``valid_until``, open where it states none
    (``KnownAbsent``), or else until the next calibration's ``valid_from`` (open when none
    follows). A successor that states no ``valid_from`` leaves the end unstated: no interval.
    """
    calibration = entry.calibration
    record = (calibration.record,)
    start_knowledge = calibration.valid_from
    if not isinstance(start_knowledge, Known):
        ambiguous = isinstance(start_knowledge, Ambiguous)
        view.findings.append(
            _finding(
                "ambiguous_validity" if ambiguous else "validity_unstated",
                "a calibration's valid_from is "
                + ("Ambiguous" if ambiguous else "not stated")
                + "; it is ordered by its instant, but no interval is claimed for it",
                record,
                Severity.WARNING if ambiguous else Severity.INFO,
            )
        )
        return [], False
    start = view.place(start_knowledge.value)
    until = calibration.valid_until
    readings: list[tuple[Timestamp | Open, tuple[EvidenceRef, ...]]]
    if isinstance(until, Known):
        readings = [(view.place(until.value), ())]
    elif isinstance(until, Ambiguous):
        view.findings.append(
            _finding(
                "ambiguous_validity",
                "a calibration's valid_until is Ambiguous; each reading is a candidate",
                record,
                readings=len(until.candidates),
            )
        )
        readings = [(view.place(c.value), parse.cited(c)) for c in until.candidates]
    elif isinstance(until, KnownAbsent):
        readings = [(OPEN, ())]
    elif after == "unstated":
        view.findings.append(
            _finding(
                "end_unstated",
                "a calibration states no valid_until and the next calibration of its series"
                " states no valid_from; where it ended is not stated, so no interval is claimed",
                record,
            )
        )
        return [], False
    else:
        readings = [(OPEN if after is None else after, ())]
    windows: list[tuple[Timestamp, Timestamp | Open, tuple[EvidenceRef, ...]]] = []
    for end, cited in readings:
        if isinstance(end, Timestamp) and (end.domain_id != start.domain_id or not start < end):
            view.findings.append(
                _finding(
                    "untimeable_window",
                    "a calibration's valid_until is not after its valid_from on one clock; that"
                    " reading is not placed",
                    record,
                )
            )
            continue
        windows.append((start, end, cited))
    return windows, isinstance(until, Ambiguous)


def _claims(view: _View, entry: _Entry, after: _Next, candidate: bool) -> list[ClaimDraft]:
    """``calibrated_with`` (or ``calibration_candidate``) claims on each of its sensor's nodes."""
    if entry.configuration is None:
        return []
    windows, ambiguous_end = _windows(view, entry, after)
    predicate = CALIBRATION_CANDIDATE if candidate or ambiguous_end else CALIBRATED_WITH
    return [
        ClaimDraft(
            subject=node,
            predicate=predicate,
            object=entry.configuration,
            valid_from=start,
            valid_to=end,
            assertion_kind=entry.calibration.assertion_kind,
            evidence=(*entry.evidence, *cited),
            records=entry.records,
        )
        for node in entry.placement.nodes
        for start, end, cited in windows
    ]


def _entry(view: _View, calibration: CalibrationRecord) -> _Entry | None:
    at = _instant(view, calibration)
    placement = _placement(view, calibration, at)
    if placement is None:
        return None
    configurations = view.anchored(NodeType.CONFIGURATION, calibration.anchor)
    configuration = configurations[0] if len(configurations) == 1 else None
    if configuration is None:
        view.findings.append(
            _finding(
                "unthreaded_id" if not configurations else "ambiguous_anchor",
                "a calibration has "
                + ("no configuration thread" if not configurations else "several threads")
                + "; no calibrated_with claim names it",
                (calibration.record,),
                nodes=[n.node_id for n in configurations],
            )
        )
    bindings = _known_bindings(view, calibration)
    disagreement = _frame_check(view, calibration, placement, bindings)
    # Only edges of the sensor's configuration graphs identify what an extrinsic measures across
    # recalibrations; an edge of a calibration file's own graph is that file's (root ADR 0007).
    graphs = {frame.frame_graph_id for frame in placement.frames}
    bindings = [b for b in bindings if b.parent.frame_graph_id in graphs]
    evidence = [*calibration.evidence, *placement.evidence]
    records = [calibration.record, *placement.records]
    if disagreement is not None:
        evidence.extend(disagreement[0])
        records.extend(disagreement[1])
    return _Entry(
        calibration=calibration,
        at=at,
        configuration=configuration,
        placement=placement,
        bindings=tuple(bindings),
        frames_agree=disagreement is None,
        evidence=tuple(evidence),
        records=tuple(records),
    )


def _history(view: _View, entries: list[_Entry]) -> list[ClaimDraft]:
    """Series per sensor node and kind; each ordered on one clock, ties never broken."""
    series: dict[tuple[NodeRef, tuple[str, ...]], list[_Entry]] = {}
    for entry in entries:
        if entry.at is None:
            view.findings.append(
                _finding(
                    "untimed",
                    "a calibration states neither valid_from nor performed; it is in no ordered"
                    " history and no drift is computed for it",
                    (entry.calibration.record,),
                )
            )
            # It states no valid_from: the finding says whether that is unstated or Ambiguous.
            _windows(view, entry, None)
            continue
        for node in entry.placement.nodes:
            series.setdefault((node, entry.shape), []).append(entry)
    drafts: list[ClaimDraft] = []
    deltas: dict[tuple[RecordId, RecordId], list[_Delta]] = {}
    for (node, _), members in sorted(series.items(), key=lambda item: item[0][0].node_id):
        clocks: dict[RecordId, list[_Entry]] = {}
        for entry in members:
            clocks.setdefault(entry.at.domain_id, []).append(entry)  # type: ignore[union-attr]
        if len(clocks) > 1:
            view.findings.append(
                _finding(
                    "clock_split",
                    "a sensor's calibrations state instants on clocks Memory cannot compare; each"
                    " clock is a history of its own, and no drift is computed across them",
                    (e.calibration.record for e in members),
                    sensor=node.node_id,
                    clocks=sorted(clocks),
                )
            )
        for domain in sorted(clocks):
            drafts.extend(_series(view, node, clocks[domain], deltas))
    return drafts


def _ticks(entry: _Entry) -> int:
    return entry.at.ticks  # type: ignore[union-attr]


def _series(
    view: _View,
    node: NodeRef,
    members: list[_Entry],
    deltas: dict[tuple[RecordId, RecordId], list[_Delta]],
) -> list[ClaimDraft]:
    """One series on one clock. Definite calibrations are ordered by instant; a candidate is
    placed in time but orders nothing: it ends no calibration and is never a drift end."""
    instants: list[list[_Entry]] = []
    for entry in sorted((e for e in members if not e.candidate), key=_ticks):
        if instants and instants[-1][0].at == entry.at:
            instants[-1].append(entry)
        else:
            instants.append([entry])
    candidates = [e for e in members if e.candidate]
    drafts: list[ClaimDraft] = []
    for entry in sorted(members, key=lambda e: (_ticks(e), e.calibration.record)):
        later = [group for group in instants if _ticks(group[0]) > _ticks(entry)]
        after: _Next = None
        if later:
            stated = any(isinstance(e.calibration.valid_from, Known) for e in later[0])
            after = later[0][0].at if stated else "unstated"
        tie = any(entry in group and len(group) > 1 for group in instants)
        claims = _claims(view, entry, after, candidate=entry.candidate or tie)
        drafts.extend(d for d in claims if d.subject == node)
    for group in instants:
        if len(group) > 1:
            view.findings.append(
                _finding(
                    "same_instant",
                    "calibrations of one sensor and kind state the same instant; neither is"
                    " ordered before the other, so each is a candidate and no drift is computed"
                    " to or from them",
                    (e.calibration.record for e in group),
                    sensor=node.node_id,
                    at=group[0].at.to_json(),  # type: ignore[union-attr]
                )
            )
    for first, second in pairwise(instants):
        if len(first) > 1 or len(second) > 1:
            continue
        earlier, later_entry = first[0], second[0]
        between = [
            c.calibration.record
            for c in candidates
            if _ticks(earlier) <= _ticks(c) <= _ticks(later_entry)
        ]
        if between:
            view.findings.append(
                _finding(
                    "drift_undecided",
                    "a calibration that may be this sensor's lies between two of its calibrations;"
                    " whether they are consecutive is not decided, so no drift is claimed",
                    (earlier.calibration.record, later_entry.calibration.record, *between),
                    sensor=node.node_id,
                )
            )
            continue
        key = (earlier.calibration.record, later_entry.calibration.record)
        if key not in deltas:
            deltas[key] = _deltas(view, earlier, later_entry)
        drafts.extend(_drift(node, earlier, later_entry, deltas[key]))
    return drafts


# --- Drift --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Delta:
    delta: Delta
    unit: Knowledge[Unit]
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]


@dataclass
class _Refusals:
    """Why some compared values gave no delta, by reason, for one finding per reason."""

    reasons: dict[str, list[JsonValue]] = field(default_factory=dict)

    def add(self, reason: str, what: JsonValue) -> None:
        self.reasons.setdefault(reason, []).append(what)


_REASONS: Final[Mapping[str, str]] = {
    "unit_mismatch": "the two calibrations declare different units; the compiler records no"
    " declared conversion, so no delta (never converted)",
    "unit_unstated": "a unit is not stated as Known on both sides; no delta",
    "shape_changed": "the two calibrations declare different numbers of components; no delta",
    "too_many_values": "a value has more components than a delta holds; no delta",
    "value_unstated": "a value is not stated as Known numbers on both sides; no delta",
    "non_finite": "a value, or its difference, is not finite; no delta",
    "setting_changed": "a declared setting (text) differs, so the numbers may follow another"
    " model; no parameter delta for this pair",
    "incomparable_transform": "the bound transforms are not declared in the same form and"
    " interpretation; no delta",
    "ambiguous_transform": "a calibration binds an edge to more than one transform; no delta",
    "dangling_transform": "a binding names a transform the Ledger does not hold; no delta",
}


def _same(a: Knowledge[Any], b: Knowledge[Any]) -> bool:
    return isinstance(a, Known) and isinstance(b, Known) and a.value == b.value


def _unit(
    a: Knowledge[Unit], b: Knowledge[Unit], refusals: _Refusals, what: JsonValue
) -> Knowledge[Unit] | None:
    if isinstance(a, Known) and isinstance(b, Known):
        if a.value == b.value:
            return Known(a.value)
        refusals.add(
            "unit_mismatch",
            {"earlier": a.value.to_json(), "later": b.value.to_json(), "of": what},
        )
        return None
    refusals.add("unit_unstated", what)
    return None


def _difference(earlier: Sequence[object], later: Sequence[object]) -> tuple[float, ...] | None:
    """``later - earlier`` component by component; ``None`` if any value is not finite."""
    out: list[float] = []
    for a, b in zip(earlier, later, strict=True):
        if not isinstance(a, float) or not isinstance(b, float):
            return None
        difference = b - a
        if difference != difference or difference in (float("inf"), float("-inf")):
            return None
        out.append(difference)
    return tuple(out)


def _finite(
    values: tuple[float, ...] | None, refusals: _Refusals, what: JsonValue
) -> tuple[float, ...] | None:
    if values is None:
        refusals.add("non_finite", what)
    return values


def _parameter_deltas(
    earlier: CalibrationRecord, later: CalibrationRecord, refusals: _Refusals
) -> list[_Delta]:
    mine = {p.name: p for p in earlier.parameters}
    theirs = {p.name: p for p in later.parameters}
    names = sorted(mine.keys() & theirs.keys())
    changed = [
        name
        for name in names
        if isinstance(mine[name].value, Known)
        and isinstance(theirs[name].value, Known)
        and isinstance(mine[name].value.value, str)  # type: ignore[union-attr]
        and mine[name].value.value != theirs[name].value.value  # type: ignore[union-attr]
    ]
    if changed:
        for name in changed:
            refusals.add("setting_changed", name)
        return []
    out: list[_Delta] = []
    for name in names:
        a, b = mine[name], theirs[name]
        delta = _parameter_delta(earlier, later, a, b, refusals)
        if delta is not None:
            out.append(delta)
    return out


def _parameter_delta(
    earlier: CalibrationRecord,
    later: CalibrationRecord,
    a: CalibrationParameter,
    b: CalibrationParameter,
    refusals: _Refusals,
) -> _Delta | None:
    if not isinstance(a.value, Known) or not isinstance(b.value, Known):
        refusals.add("value_unstated", a.name)
        return None
    values_a, values_b = a.value.value, b.value.value
    if isinstance(values_a, str) or isinstance(values_b, str):
        if not (isinstance(values_a, str) and isinstance(values_b, str)):
            refusals.add("value_unstated", a.name)
        return None  # an unchanged setting has no delta
    if len(values_a) != len(values_b):
        refusals.add("shape_changed", a.name)
        return None
    if not values_a:
        return None  # two empty lists: nothing to compare
    if len(values_a) > MAX_DELTA_VALUES:
        refusals.add("too_many_values", a.name)
        return None
    values = _difference(values_a, values_b)
    if values is None:
        refusals.add("non_finite", a.name)
        return None
    unit = _unit(a.unit, b.unit, refusals, a.name)
    if unit is None:
        return None
    cited = (*parse.cited(a.value), *parse.cited(b.value))
    cited += (*parse.cited(a.unit), *parse.cited(b.unit))
    return _Delta(
        Delta(earlier.record, later.record, DeltaQuantity.PARAMETER, "values", values, name=a.name),
        unit,
        (*earlier.evidence, *later.evidence, *cited),
        (earlier.record, later.record),
    )


def _bound(
    view: _View, entry: _Entry, edge: Edge, refusals: _Refusals
) -> tuple[Binding, Transform] | None:
    bindings = [b for b in entry.bindings if (b.parent, b.child) == edge]
    transforms = {b.transform for b in bindings}
    if len(transforms) != 1:
        refusals.add("ambiguous_transform", _edge_json(edge))
        return None
    binding = bindings[0]
    transform = view.transforms.get(binding.transform)
    if transform is None:
        refusals.add("dangling_transform", _edge_json(edge))
        return None
    return binding, transform


def _rotation_form(a: Rotation, b: Rotation) -> tuple[str, Knowledge[Unit] | None] | None:
    """The form both rotations share with the same Known interpretation, and its unit (``None``
    for a form without one); ``None`` when they are not comparable as declared."""
    match a, b:
        case Quaternion(), Quaternion():
            if _same(a.order, b.order) and _same(a.convention, b.convention):
                return "quaternion", None
        case RotationMatrix(), RotationMatrix():
            if _same(a.layout, b.layout):
                return "rotation_matrix", None
        case EulerAngles(), EulerAngles():
            if _same(a.sequence, b.sequence) and _same(a.mode, b.mode):
                return "euler_angles", a.unit
        case RotationVector(), RotationVector():
            return "rotation_vector", a.unit
    return None


def _transform_deltas(
    view: _View, earlier: _Entry, later: _Entry, refusals: _Refusals
) -> list[_Delta]:
    out: list[_Delta] = []
    shared = {(b.parent, b.child) for b in earlier.bindings} & {
        (b.parent, b.child) for b in later.bindings
    }
    edges = sorted(shared, key=_edge_key)
    for edge in edges:
        first, second = _bound(view, earlier, edge, refusals), _bound(view, later, edge, refusals)
        if first is None or second is None:
            continue
        (binding_a, a), (binding_b, b) = first, second
        what = _edge_json(edge)
        if (
            a.parent.frame_id != b.parent.frame_id
            or a.child.frame_id != b.child.frame_id
            or not _same(a.direction, b.direction)
        ):
            refusals.add("incomparable_transform", what)
            continue
        cited = (*earlier.calibration.evidence, *later.calibration.evidence)
        cited += (*binding_a.evidence, *binding_b.evidence, *a.evidence, *b.evidence)
        records = (
            earlier.calibration.record,
            later.calibration.record,
            binding_a.record,
            binding_b.record,
            a.record,
            b.record,
        )
        for quantity, form, values, unit in _parts(a.value, b.value, refusals, what):
            delta = Delta(
                earlier.calibration.record,
                later.calibration.record,
                quantity,
                form,
                values,
                edge=edge,
            )
            out.append(_Delta(delta, unit, cited, records))
    return out


def _parts(
    a: TransformValue, b: TransformValue, refusals: _Refusals, what: JsonValue
) -> list[tuple[DeltaQuantity, str, tuple[float, ...], Knowledge[Unit]]]:
    """The translation and rotation deltas of two transform values declared alike."""
    parts: list[tuple[DeltaQuantity, str, tuple[float, ...], Knowledge[Unit]]] = []
    if isinstance(a, Pose) and isinstance(b, Pose):
        unit = _unit(a.translation.unit, b.translation.unit, refusals, what)
        values = _finite(_difference(a.translation.values, b.translation.values), refusals, what)
        if unit is not None and values is not None:
            parts.append((DeltaQuantity.TRANSLATION, "translation", values, unit))
        form = _rotation_form(a.rotation, b.rotation)
        if form is None:
            refusals.add("incomparable_transform", what)
            return parts
        name, declared = form
        rotation_unit: Knowledge[Unit] | None = NotApplicable()
        if declared is not None:
            rotation_unit = _unit(declared, b.rotation.unit, refusals, what)  # type: ignore[union-attr]
        values = _finite(_difference(a.rotation.values, b.rotation.values), refusals, what)
        if rotation_unit is not None and values is not None:
            parts.append((DeltaQuantity.ROTATION, name, values, rotation_unit))
        return parts
    if isinstance(a, HomogeneousMatrix) and isinstance(b, HomogeneousMatrix):
        if not _same(a.layout, b.layout):
            refusals.add("incomparable_transform", what)
            return parts
        layout: MatrixLayout = a.layout.value  # type: ignore[union-attr]
        rotation = _finite(
            _difference(
                [a.values[i] for i in _MATRIX_ROTATION], [b.values[i] for i in _MATRIX_ROTATION]
            ),
            refusals,
            what,
        )
        if rotation is not None:
            parts.append((DeltaQuantity.ROTATION, "homogeneous_matrix", rotation, NotApplicable()))
        indices = _MATRIX_TRANSLATION[layout]
        unit = _unit(a.translation_unit, b.translation_unit, refusals, what)
        translation = _finite(
            _difference([a.values[i] for i in indices], [b.values[i] for i in indices]),
            refusals,
            what,
        )
        if unit is not None and translation is not None:
            parts.append((DeltaQuantity.TRANSLATION, "homogeneous_matrix", translation, unit))
        return parts
    refusals.add("incomparable_transform", what)
    return parts


def _deltas(view: _View, earlier: _Entry, later: _Entry) -> list[_Delta]:
    refusals = _Refusals()
    found = _parameter_deltas(earlier.calibration, later.calibration, refusals)
    found.extend(_transform_deltas(view, earlier, later, refusals))
    for reason in sorted(refusals.reasons):
        view.findings.append(
            _finding(
                reason,
                _REASONS[reason],
                (earlier.calibration.record, later.calibration.record),
                Severity.INFO if reason == "value_unstated" else Severity.WARNING,
                compared=refusals.reasons[reason],
            )
        )
    return found


def _drift(node: NodeRef, earlier: _Entry, later: _Entry, deltas: list[_Delta]) -> list[ClaimDraft]:
    """``drift`` over ``[earlier's instant, later's instant)``: observed, a fact about the two
    records (as ``not_covered_by_authorisation`` is, ADR 0010 §5), never a judgement."""
    return [
        ClaimDraft(
            subject=node,
            predicate=DRIFT,
            object=TypedLiteral(ValueType.DELTA, item.delta, item.unit),
            valid_from=earlier.at,  # type: ignore[arg-type]
            valid_to=later.at,  # type: ignore[arg-type]
            assertion_kind=_OBSERVED,
            evidence=item.evidence,
            records=item.records,
        )
        for item in deltas
    ]


# --- calibrated_by ------------------------------------------------------------------------------


def _calibrated_by(view: _View) -> list[ClaimDraft]:
    """A calibration whose configuration node a maintenance record states resulted (or a
    requalification is bound to) is ``calibrated_by`` that record, from when it was performed."""
    calibrations: dict[NodeRef, list[RecordId]] = {}
    for calibration in view.calibrations:
        nodes = view.anchored(NodeType.CONFIGURATION, calibration.anchor)
        if len(nodes) == 1:
            calibrations.setdefault(nodes[0], []).append(calibration.record)
    drafts: list[ClaimDraft] = []
    for producer in view.producers:
        named = [node_ref(NodeType.CONFIGURATION, c) for c in producer.configuration]
        hits = [n for n in named if n in calibrations]
        if not hits:
            continue
        cited = sorted({r for n in hits for r in calibrations[n]})
        if producer.outcome == "ambiguous":
            view.findings.append(
                _finding(
                    "ambiguous_producer",
                    f"a {producer.kind}'s configuration is Ambiguous and one reading names a"
                    " calibration; no calibrated_by is claimed",
                    (producer.record, *cited),
                )
            )
            continue
        if producer.performed is None:
            view.findings.append(
                _finding(
                    "untimed_producer",
                    f"a {producer.kind} names a calibration's configuration but states no"
                    " performed instant; no calibrated_by is claimed",
                    (producer.record, *cited),
                )
            )
            continue
        (node,) = hits
        drafts.append(
            ClaimDraft(
                subject=node,
                predicate=CALIBRATED_BY,
                object=LedgerRecordRef(producer.record),
                valid_from=view.place(producer.performed),
                valid_to=OPEN,
                assertion_kind=_STATED,
                evidence=producer.evidence,
                records=(producer.record, *cited),
            )
        )
    return drafts


# --- The consolidator ---------------------------------------------------------------------------


class CalibrationHistoryConsolidator:
    """Calibration history, drift and producers per sensor. Takes no configuration.

    Run it after ``memory.configuration`` (ADR 0003 §4): it reads that consolidator's machine
    chains to find a machine's configuration at a calibration's instant.
    """

    consolidator_id: Final = CALIBRATION_CONSOLIDATOR_ID
    version: Final = "1"
    model: Final[ModelRef | None] = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        view = _read(ledger, previous)
        if config:
            view.findings.append(
                _finding(
                    "unknown_config",
                    "the calibration history consolidator takes no configuration; keys ignored",
                    keys=sorted(config),
                )
            )
        entries = [e for c in view.calibrations if (e := _entry(view, c)) is not None]
        drafts = _history(view, entries)
        drafts.extend(_calibrated_by(view))
        return ConsolidatorOutput(tuple(drafts), tuple(view.findings))
