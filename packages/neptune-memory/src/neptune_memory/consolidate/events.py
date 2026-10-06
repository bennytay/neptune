"""Events as a deterministic consolidator (ADR 0013).

One ``event`` node per thing one record states happened: an ``incident_record`` (and each entry of
its timeline), an ``intervention``, a ``maintenance_event`` (and each action it states, ADR 0025
§1), a ``status_report`` a typed log stream's message makes (ADR 0025 §2), or a row of a table the
config declares an event table (Deploy's ROS 2 ``diagnostic events``, a PLC or safety-controller
log, a syslog export). The node is keyed by
its record (``record:<rec id>``; a timeline entry ``record:<rec id>/timeline/<i>``): two records
are two statements, and relating two reports of one incident is identity's, never this module's.

Every claim about an event holds over the event's own time on the clock its record names: an
instant ``t`` is ``[t, t + 1 tick)``, an interval ``[start, end)``. It holds again on each clock a
stated ``clock_mapping`` from that clock reaches directly, and on the civil clock a
``timestamp_domain`` that declares itself civil names (ADR 0009 §2 places runs the same way).

- ``event_kind``: a registered kind (``EVENT_KINDS``) through a vendor mapping the config declares;
  an unmapped kind is no claim (``Unknown``) and a finding. ``declared_kind``, ``stated_severity``
  and ``has_description``: verbatim; ``stated_cause``: an incident's root cause or a maintenance
  event's diagnosis, verbatim. ``involves``, ``at_site`` and ``in_zone`` from declared ids, a
  ``*_candidate`` claim per reading of an ``Ambiguous`` one; a maintenance event also involves the
  parts it removed and installed, by serial. ``declared_value``: a status report's key/values.
  ``evidenced_by``: the record.
- ``co_occurs_within``: two events from different sources whose onsets fall inside one window of
  the configured length on a clock both are placed on. The claim's valid interval *is* that window
  (so its length names the window and its clock the clock), it cites the mapping used, and it is
  never called a cause. Clocks no stated mapping relates are not compared (``clocks_unrelated``);
  a mapping too coarse to decide, or one that states no residual bound, gives a finding
  (``co_occurrence_undecided``, ``co_occurrence_unbounded``), never a claim.
- An end that is declared but not stated (blank, unreadable or ambiguous) leaves the event open,
  never an instant (``end_unstated``, ``end_unread``, ``end_ambiguous``).

Records are parsed by ``consolidate.event_records``; this module decides. Malformed or
contradictory input is a finding and never a claim, and the rest of the build is unaffected.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING, Final

from neptune.model.alignment import ClockMapping
from neptune.model.finding import Severity
from neptune.model.ids import LogicalId, RecordId, parse_record_id
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    Unknown,
)
from neptune.model.lifecycle import IncidentRecord, Intervention, MaintenanceEvent
from neptune.model.provenance import Provenance
from neptune.model.status import StatusReport
from neptune.model.time import INT64_MAX, Timestamp
from neptune.model.world import StructuredRecord, StructuredTable
from neptune_memory.consolidate import event_records as parse
from neptune_memory.consolidate.base import (
    EVENTS_CONSOLIDATOR_ID as EVENTS_CONSOLIDATOR_ID,
)
from neptune_memory.consolidate.base import ClaimDraft, ConsolidationFinding, ConsolidatorOutput
from neptune_memory.consolidate.identity import node_ref
from neptune_memory.consolidate.identity_records import declared
from neptune_memory.consolidate.runs import EVIDENCED_BY, _covers, _definite
from neptune_memory.consolidate.runs import Placement as RunPlacement
from neptune_memory.schema.claim import (
    DeclaredType,
    DeclaredValue,
    LedgerRecordRef,
    TypedLiteral,
    ValueType,
)
from neptune_memory.schema.interval import OPEN, CivilClock, Open
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import (
    AT_SITE,
    CANDIDATE_OF,
    CO_OCCURS_WITHIN,
    DECLARED_KIND,
    DECLARED_VALUE,
    EVENT_KIND,
    HAS_DESCRIPTION,
    IN_ZONE,
    INVOLVES,
    STATED_CAUSE,
    STATED_SEVERITY,
    is_declared_value,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge, ProvenanceSlot
    from neptune.model.provenance import EvidenceRef
    from neptune.model.world import CellValue
    from neptune_memory.consolidate.base import ModelRef
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim, ClaimObject

OBSERVED: Final = AssertionKind.OBSERVED
STATED: Final = AssertionKind.STATED
RECORD_NAMESPACE: Final = "record"
HAS_NAME: Final = "has_name"
MAX_LISTED: Final = 16  # record ids an aggregated finding lists; its details give the count


def _finding(
    code: str,
    message: str,
    records: Iterable[RecordId] = (),
    severity: Severity = Severity.WARNING,
    **details: JsonValue,
) -> ConsolidationFinding:
    return ConsolidationFinding(
        code=f"events.{code}",
        severity=severity,
        message=message[:1000],
        records=tuple(records),
        details=details,
    )


def _cited(slot: ProvenanceSlot) -> tuple[EvidenceRef, ...]:
    """The evidence a value's own provenance cites; nothing when it inherits its record's."""
    return (slot.evidence,) if isinstance(slot, Provenance) else ()


def _kind(slot: ProvenanceSlot, default: AssertionKind) -> AssertionKind:
    return slot.assertion_kind if isinstance(slot, Provenance) else default


def event_node(record: RecordId, *path: str | int) -> NodeRef:
    """An event's node: its record, and the place inside it for an entry of a record's list."""
    value = "/".join((record, *(str(step) for step in path)))
    return node_ref(NodeType.EVENT, LogicalId(RECORD_NAMESPACE, value))


# --- Reading the Ledger -------------------------------------------------------------------------


@dataclass
class _View:
    incidents: dict[RecordId, IncidentRecord] = field(default_factory=dict)
    interventions: dict[RecordId, Intervention] = field(default_factory=dict)
    maintenance: dict[RecordId, MaintenanceEvent] = field(default_factory=dict)
    statuses: dict[RecordId, StatusReport] = field(default_factory=dict)
    tables: dict[RecordId, StructuredTable] = field(default_factory=dict)
    rows: dict[RecordId, StructuredRecord] = field(default_factory=dict)
    domains: dict[RecordId, parse.Clock] = field(default_factory=dict)
    mappings: dict[RecordId, list[ClockMapping]] = field(default_factory=dict)  # by source clock
    resolutions: dict[RecordId, Fraction] = field(default_factory=dict)  # by placed clock id
    unbounded: set[RecordId] = field(default_factory=set)  # mappings used that state no bound
    findings: list[ConsolidationFinding] = field(default_factory=list)

    def place(self, stamp: Timestamp) -> Timestamp:
        """A stamp on a clock that declares itself civil, on that ``CivilClock``; any other
        stamp as declared."""
        known = self.domains.get(stamp.domain_id)
        if known is None or known.civil is None:
            return stamp
        return known.civil.at(stamp.ticks)

    def civil(self, domain: RecordId) -> CivilClock | None:
        known = self.domains.get(domain)
        return None if known is None else known.civil


_PARSERS: Final[Mapping[str, Callable[[Mapping[str, object]], object]]] = {
    parse.TIMESTAMP_DOMAIN: parse.clock,
    parse.CLOCK_MAPPING: parse.mapping,
    parse.INCIDENT: parse.incident_record,
    parse.INTERVENTION: parse.intervention_record,
    parse.MAINTENANCE: parse.maintenance_record,
    parse.STATUS_REPORT: parse.status_report,
    parse.STRUCTURED_TABLE: parse.table,
    parse.STRUCTURED_RECORD: parse.row,
}


def _record_id(parsed: object) -> RecordId:
    if isinstance(parsed, parse.Clock):
        return parsed.record
    assert isinstance(
        parsed,
        ClockMapping
        | IncidentRecord
        | Intervention
        | MaintenanceEvent
        | StatusReport
        | StructuredTable
        | StructuredRecord,
    )
    return parsed.id


def _read(ledger: LedgerReader) -> _View:
    view = _View()
    seen: dict[tuple[str, RecordId], object] = {}
    conflicted: set[tuple[str, RecordId]] = set()
    for ref in ledger.list_packages():
        for kind, parser in _PARSERS.items():
            for index, record in enumerate(ledger.read_records(ref.package_id, kind) or ()):
                try:
                    parsed = parser(record)
                except parse.Inferred:
                    view.findings.append(
                        _finding(
                            "inferred_record",
                            f"{kind} record {index} in package {ref.package_id!r} is inferred: a"
                            " derived/ record, never a ground for an event claim",
                            severity=Severity.INFO,
                            index=index,
                            kind=kind,
                            package_id=ref.package_id,
                        )
                    )
                    continue
                except parse.Malformed as exc:
                    reason = str(exc)
                    try:
                        reason.encode("utf-8")
                    except UnicodeEncodeError:
                        reason = "unrepresentable text"
                    view.findings.append(
                        _finding(
                            "malformed_record",
                            f"{kind} record {index} in package {ref.package_id!r} is malformed:"
                            f" {reason}",
                            severity=Severity.ERROR,
                            index=index,
                            kind=kind,
                            package_id=ref.package_id,
                        )
                    )
                    continue
                key = (kind, _record_id(parsed))
                if key in conflicted:
                    continue
                if seen.setdefault(key, parsed) != parsed:
                    conflicted.add(key)
                    view.findings.append(
                        _finding(
                            "record_conflict",
                            "one record id carries different content in two places; record not"
                            " used",
                            (key[1],),
                            Severity.ERROR,
                        )
                    )
    for key in sorted(seen):
        if key in conflicted:
            continue
        parsed = seen[key]
        if isinstance(parsed, parse.Clock):
            view.domains[parsed.record] = parsed
        elif isinstance(parsed, ClockMapping):
            view.mappings.setdefault(parsed.source, []).append(parsed)
        elif isinstance(parsed, IncidentRecord):
            view.incidents[parsed.id] = parsed
        elif isinstance(parsed, Intervention):
            view.interventions[parsed.id] = parsed
        elif isinstance(parsed, MaintenanceEvent):
            view.maintenance[parsed.id] = parsed
        elif isinstance(parsed, StatusReport):
            view.statuses[parsed.id] = parsed
        elif isinstance(parsed, StructuredTable):
            view.tables[parsed.id] = parsed
        elif isinstance(parsed, StructuredRecord):
            view.rows[parsed.id] = parsed
    for found in view.mappings.values():
        found.sort(key=lambda m: m.id)
    for domain in view.domains.values():
        if domain.resolution is not None:
            view.resolutions[domain.record] = domain.resolution
        if domain.civil is not None:
            view.resolutions[domain.civil.domain_id] = domain.civil.resolution
    return view


# --- What one record states ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Fact:
    """One claim about an event, before it is placed in time."""

    predicate: str
    obj: ClaimObject
    kind: AssertionKind
    evidence: tuple[EvidenceRef, ...] = ()
    records: tuple[RecordId, ...] = ()


@dataclass
class _Event:
    """One event: what grounds it, when it began (and ended) as declared, and its facts."""

    node: NodeRef
    source: str  # the bytes its record cites: events of one source are never compared
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]
    start: Timestamp
    end: Timestamp | Open | None  # None: an instant; OPEN: an end that is declared but not stated
    time_evidence: tuple[EvidenceRef, ...]
    facts: list[_Fact] = field(default_factory=list)


def _text(value: str) -> TypedLiteral:
    return TypedLiteral(ValueType.TEXT, value)


def _verbatim(value: CellValue) -> TypedLiteral | None:
    """A cell's text or integer, as declared; any other value is no verbatim kind or severity."""
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return TypedLiteral(ValueType.INTEGER, value)
    return None


def _declared(knowledge: Knowledge[LogicalId]) -> bool:
    """Whether every id a field states is a declared value (ADR 0006 §9): never blank or padded.
    A field that states one that is not is no field: none of its readings is used."""
    values = (
        (knowledge.value,)
        if isinstance(knowledge, Known)
        else tuple(c.value for c in knowledge.candidates)
        if isinstance(knowledge, Ambiguous)
        else ()
    )
    return all(is_declared_value(v.value) for v in values)


def _ids(
    knowledge: Knowledge[LogicalId],
    predicate: str,
    node_type: NodeType,
    default: AssertionKind,
) -> list[_Fact]:
    """``predicate`` for a ``Known`` id, a candidate claim per reading of an ``Ambiguous`` one,
    and nothing for any other state or for a field stating an undeclared id."""
    if not _declared(knowledge):
        return []
    if isinstance(knowledge, Known):
        return [
            _Fact(
                predicate,
                node_ref(node_type, knowledge.value),
                _kind(knowledge.provenance, default),
                _cited(knowledge.provenance),
            )
        ]
    if isinstance(knowledge, Ambiguous):
        return [
            _Fact(
                CANDIDATE_OF[predicate],
                node_ref(node_type, candidate.value),
                _kind(candidate.provenance, default),
                _cited(candidate.provenance),
            )
            for candidate in knowledge.candidates
        ]
    return []


def _listed(
    listed: Knowledge[tuple[Knowledge[LogicalId], ...]], node_type: NodeType
) -> list[_Fact]:
    """``involves`` for each item of a ``Known`` list; an unstated list states nothing."""
    if not isinstance(listed, Known):
        return []
    cited = _cited(listed.provenance)
    return [
        _Fact(fact.predicate, fact.obj, fact.kind, (*fact.evidence, *cited))
        for item in listed.value
        for fact in _ids(item, INVOLVES, node_type, STATED)
    ]


def _stated_text(
    knowledge: Knowledge[str], predicate: str, default: AssertionKind = STATED
) -> list[_Fact]:
    if isinstance(knowledge, Known):
        return [
            _Fact(
                predicate,
                _text(knowledge.value),
                _kind(knowledge.provenance, default),
                _cited(knowledge.provenance),
            )
        ]
    return []


def _parts(record: MaintenanceEvent) -> list[_Fact]:
    """``involves`` for each part unit a maintenance event removed or installed, by its declared
    id (a serial); a list not stated names none."""
    if not isinstance(record.parts, Known):
        return []
    cited = _cited(record.parts.provenance)
    return [
        _Fact(fact.predicate, fact.obj, fact.kind, (*fact.evidence, *cited))
        for part in record.parts.value
        for listed in (part.removed, part.installed)
        for fact in _listed(listed, NodeType.ASSET)
    ]


def _named_by(identifiers: Knowledge[tuple[Knowledge[LogicalId], ...]]) -> list[_Fact]:
    """``has_name``: the one id value a lifecycle record declares for itself (a work order or
    incident number), verbatim, as the name people use for the event; never the node's key
    (ADR 0026). Several distinct values, or none stated, name nothing."""
    if not isinstance(identifiers, Known):
        return []
    known = [
        i for i in identifiers.value if isinstance(i, Known) and is_declared_value(i.value.value)
    ]
    values = {i.value.value for i in known}
    if len(values) != 1:
        return []
    first = known[0]
    return [
        _Fact(
            HAS_NAME,
            _text(first.value.value),
            _kind(first.provenance, STATED),
            (*_cited(first.provenance), *_cited(identifiers.provenance)),
        )
    ]


def _status_values(record: StatusReport) -> list[_Fact]:
    """``declared_value`` for each key/value a status states, in order: text, or an integer
    with no unit stated (``Unknown``). A list not stated states none."""
    if not isinstance(record.values, Known):
        return []
    kind = _kind(record.values.provenance, OBSERVED)
    cited = _cited(record.values.provenance)
    facts: list[_Fact] = []
    for item in record.values.value:
        if isinstance(item.value, str):
            literal = TypedLiteral(
                ValueType.DECLARED_VALUE,
                DeclaredValue((item.key,), DeclaredType.TEXT, item.value),
            )
        else:
            literal = TypedLiteral(
                ValueType.DECLARED_VALUE,
                DeclaredValue((item.key,), DeclaredType.INTEGER, item.value),
                Unknown(),
            )
        facts.append(_Fact(DECLARED_VALUE, literal, kind, cited))
    return facts


class _Builder:
    """Turns admitted records into events, collecting the findings they give."""

    def __init__(self, view: _View, config: parse.EventConfig) -> None:
        self.view = view
        self.config = config
        self.ambiguous: dict[str, list[RecordId]] = defaultdict(list)
        self.unmapped: dict[tuple[str, parse.Key], list[RecordId]] = defaultdict(list)
        self.unstated: dict[str, list[RecordId]] = defaultdict(list)

    @property
    def findings(self) -> list[ConsolidationFinding]:
        return self.view.findings

    def open_end(self, record: RecordId, code: str, why: str, **details: JsonValue) -> Open:
        """An end the event declares but does not state as one instant: the event is placed with
        an open end (it may still have been going on), never as an instant, and a finding says
        so; an ambiguous end's readings are in the finding's details."""
        self.findings.append(
            _finding(
                code,
                f"{why}; its end is left open and no reading is chosen",
                (record,),
                Severity.INFO,
                event=event_node(record).node_id,
                **details,
            )
        )
        return OPEN

    def untimed(self, record: RecordId, why: str, *path: str | int) -> None:
        self.findings.append(
            _finding(
                "untimed_event",
                f"{why}; no claim is placed on the event",
                (record,),
                event=event_node(record, *path).node_id,
            )
        )

    def lifecycle_kind(
        self, vendor: str, declared_as: Knowledge[str], record: RecordId
    ) -> list[_Fact]:
        """A lifecycle record's kind: its vendor mapping's target for the stated text, else the
        record kind's own (``incident``, ``intervention``). An ``Ambiguous`` text with a mapped
        reading leaves the kind undecided: no claim."""
        mapping = self.config.vendors.get(vendor, {})
        default = parse.LIFECYCLE_DEFAULT_KIND[vendor]
        if isinstance(declared_as, Known) and (parse.TEXT, declared_as.value) in mapping:
            target = mapping[(parse.TEXT, declared_as.value)]
            assert target is not None  # parse_config refuses "not an event" for a lifecycle kind
            kind = _kind(declared_as.provenance, STATED)
            return [_Fact(EVENT_KIND, _text(target), kind, _cited(declared_as.provenance))]
        if isinstance(declared_as, Ambiguous) and any(
            (parse.TEXT, c.value) in mapping for c in declared_as.candidates
        ):
            self.ambiguous["event_kind"].append(record)
            return []
        return [_Fact(EVENT_KIND, _text(default), STATED)]

    def vet(self, record: RecordId, **fields: object) -> None:
        """An ``id_unusable`` finding for each id field (or list item) stating an undeclared id."""
        for name, value in sorted(fields.items()):
            items = (
                value.value
                if isinstance(value, Known) and isinstance(value.value, tuple)
                else (value,)
            )
            if not all(_declared(item) for item in items):
                self.findings.append(
                    _finding(
                        "id_unusable",
                        f"{name} states a blank or padded id; it is not used",
                        (record,),
                        Severity.INFO,
                    )
                )

    def note_ambiguous(self, record: RecordId, **fields: object) -> None:
        for name, value in sorted(fields.items()):
            if isinstance(value, Ambiguous):
                self.ambiguous[name].append(record)

    # -- lifecycle records ------------------------------------------------------------------

    def incident(self, record: IncidentRecord) -> list[_Event]:
        events: list[_Event] = []
        evidence = (record.provenance.evidence,)
        source = str(record.provenance.evidence.source)
        self.note_ambiguous(
            record.id,
            severity=record.severity,
            description=record.description,
            root_cause=record.root_cause,
        )
        self.vet(
            record.id,
            machines=record.machines,
            assets=record.assets,
            site=record.site,
            zone=record.zone,
        )
        occurred = self.instant(record.id, record.occurred, "the incident states no time")
        if occurred is not None:
            event = _Event(event_node(record.id), source, evidence, (record.id,), *occurred)
            event.facts += [
                _Fact(EVIDENCED_BY, LedgerRecordRef(record.id), STATED),
                *self.lifecycle_kind(parse.INCIDENT, record.severity, record.id),
                *_stated_text(record.severity, STATED_SEVERITY),
                *_stated_text(record.description, HAS_DESCRIPTION),
                *_stated_text(record.root_cause, STATED_CAUSE),
                *_named_by(record.identifiers),
                *_listed(record.machines, NodeType.MACHINE),
                *_listed(record.assets, NodeType.ASSET),
                *_ids(record.site, AT_SITE, NodeType.SITE, STATED),
                *_ids(record.zone, IN_ZONE, NodeType.ZONE, STATED),
            ]
            events.append(event)
        if isinstance(record.timeline, Known):
            for index, entry in enumerate(record.timeline.value):
                at = self.instant(
                    record.id, entry.time, "a timeline entry states no time", "timeline", index
                )
                if at is None:
                    continue
                self.note_ambiguous(record.id, timeline_text=entry.text)
                entry_event = _Event(
                    event_node(record.id, "timeline", index), source, evidence, (record.id,), *at
                )
                entry_event.facts += [
                    _Fact(EVIDENCED_BY, LedgerRecordRef(record.id), STATED),
                    *_stated_text(entry.text, HAS_DESCRIPTION),
                ]
                events.append(entry_event)
        return events

    def intervention(self, record: Intervention) -> list[_Event]:
        self.note_ambiguous(record.id, mode=record.mode, reason=record.reason)
        self.vet(record.id, machines=record.machines, site=record.site)
        began = self.instant(record.id, record.start, "the intervention states no start")
        if began is None:
            return []
        start, _, time_evidence = began
        end: Timestamp | Open | None = None  # NotApplicable: an instantaneous intervention
        if isinstance(record.end, Known):
            end = record.end.value
            time_evidence += _cited(record.end.provenance)
        elif isinstance(record.end, Ambiguous):
            end = self.open_end(
                record.id,
                "end_ambiguous",
                "the intervention states its end ambiguously",
                readings=[c.value.to_json() for c in record.end.candidates],
            )
        elif isinstance(record.end, KnownAbsent):
            end = OPEN  # stated as having no end yet: still in progress
        elif not isinstance(record.end, NotApplicable):  # Unknown, NotCovered
            end = self.open_end(record.id, "end_unstated", "the intervention states no end")
        event = _Event(
            event_node(record.id),
            str(record.provenance.evidence.source),
            (record.provenance.evidence,),
            (record.id,),
            start,
            end,
            time_evidence,
        )
        event.facts += [
            _Fact(EVIDENCED_BY, LedgerRecordRef(record.id), STATED),
            *self.lifecycle_kind(parse.INTERVENTION, record.mode, record.id),
            *_stated_text(record.mode, DECLARED_KIND),
            *_stated_text(record.reason, HAS_DESCRIPTION),
            *_named_by(record.identifiers),
            *_listed(record.machines, NodeType.MACHINE),
            *_ids(record.site, AT_SITE, NodeType.SITE, STATED),
        ]
        return [event]

    def maintenance(self, record: MaintenanceEvent) -> list[_Event]:
        """The event a maintenance record states, and one event per action it lists, in order
        (``record:<id>/actions/<i>``), each with its text verbatim. Its diagnosis is its stated
        cause; it involves its machines and the part units it removed or installed."""
        self.note_ambiguous(record.id, diagnosis=record.diagnosis)
        self.vet(record.id, machines=record.machines, site=record.site)
        performed = self.instant(record.id, record.performed, "the maintenance states no time")
        if performed is None:
            return []
        evidence = (record.provenance.evidence,)
        source = str(record.provenance.evidence.source)
        machines = _listed(record.machines, NodeType.MACHINE)
        if not isinstance(record.machines, Known):
            self.findings.append(
                _finding(
                    "machine_unstated",
                    "the maintenance names no machine; its events involve none",
                    (record.id,),
                    Severity.INFO,
                    event=event_node(record.id).node_id,
                )
            )
        event = _Event(event_node(record.id), source, evidence, (record.id,), *performed)
        event.facts += [
            _Fact(EVIDENCED_BY, LedgerRecordRef(record.id), STATED),
            *self.lifecycle_kind(parse.MAINTENANCE, Unknown(), record.id),
            *_stated_text(record.diagnosis, STATED_CAUSE),
            *_named_by(record.identifiers),
            *machines,
            *_parts(record),
            *_ids(record.site, AT_SITE, NodeType.SITE, STATED),
        ]
        events = [event]
        if isinstance(record.actions, Known):
            cited = _cited(record.actions.provenance)
            for index, action in enumerate(record.actions.value):
                self.note_ambiguous(record.id, action=action)
                if not isinstance(action, Known):
                    continue
                step = _Event(
                    event_node(record.id, "actions", index),
                    source,
                    evidence,
                    (record.id,),
                    *performed,
                )
                step.facts += [
                    _Fact(EVIDENCED_BY, LedgerRecordRef(record.id), STATED),
                    *(
                        _Fact(f.predicate, f.obj, f.kind, (*f.evidence, *cited))
                        for f in _stated_text(action, HAS_DESCRIPTION)
                    ),
                    *machines,
                ]
                events.append(step)
        return events

    def status(self, record: StatusReport) -> list[_Event]:
        """The status a message reports, at its first stated time in its stream's clock order
        (other clocks are reached through stated mappings). Its declared kind is its level's one
        name, else its level; its event kind is the vendor mapping's for its ``convention``."""
        self.note_ambiguous(record.id, level=record.level, message=record.message)
        times = [t for t in record.times if isinstance(t, Known)]
        if not times:
            self.untimed(record.id, "the status states no time on any clock")
            return []
        names = record.level_names
        name = names.value[0] if isinstance(names, Known) and len(names.value) == 1 else None
        level_state = record.level
        level = level_state.value if isinstance(level_state, Known) else None
        declared_as: list[_Fact] = []
        if name is not None:
            assert isinstance(names, Known)
            declared_as = [
                _Fact(
                    DECLARED_KIND,
                    _text(name),
                    _kind(names.provenance, OBSERVED),
                    _cited(names.provenance),
                )
            ]
        elif isinstance(level_state, Known):
            declared_as = [
                _Fact(
                    DECLARED_KIND,
                    TypedLiteral(ValueType.INTEGER, level_state.value),
                    _kind(level_state.provenance, OBSERVED),
                    _cited(level_state.provenance),
                )
            ]
        vendor = str(record.convention)
        mapping = self.config.vendors.get(vendor, {})
        keys = [
            *([(parse.TEXT, name)] if name is not None else []),
            *([(parse.INTEGER, str(level))] if level is not None else []),
        ]
        hit = next((key for key in keys if key in mapping), None)
        kind_facts: list[_Fact] = []
        if hit is not None:
            target = mapping[hit]
            if target is None:  # the vendor declares this level is not an event
                return []
            kind_facts = [_Fact(EVENT_KIND, _text(target), STATED)]
        elif keys:
            self.unmapped[(vendor, keys[0])].append(record.id)
        first = times[0]
        event = _Event(
            event_node(record.id),
            str(record.provenance.evidence.source),
            (record.provenance.evidence,),
            (record.id,),
            first.value,
            None,
            _cited(first.provenance),
        )
        event.facts += [
            _Fact(EVIDENCED_BY, LedgerRecordRef(record.id), OBSERVED),
            *kind_facts,
            *declared_as,
            *_stated_text(record.message, HAS_DESCRIPTION, OBSERVED),
            *_status_values(record),
        ]
        return [event]

    def instant(
        self, record: RecordId, knowledge: Knowledge[Timestamp], why: str, *path: str | int
    ) -> tuple[Timestamp, None, tuple[EvidenceRef, ...]] | None:
        """``(start, no end, the evidence of the time)`` for a stated instant; otherwise a
        finding and ``None``."""
        if isinstance(knowledge, Known):
            return knowledge.value, None, _cited(knowledge.provenance)
        if isinstance(knowledge, Ambiguous):
            self.findings.append(
                _finding(
                    "ambiguous_time",
                    "the record states its time ambiguously; no reading is chosen and no claim is"
                    " placed on the event",
                    (record,),
                    event=event_node(record, *path).node_id,
                    readings=len(knowledge.candidates),
                )
            )
            return None
        self.untimed(record, why, *path)
        return None

    # -- event tables ------------------------------------------------------------------------

    def tables(self) -> list[_Event]:
        specs = {spec.name: spec for spec in self.config.tables}
        columns: dict[RecordId, dict[str, int] | None] = {}
        events: list[_Event] = []
        for rid in sorted(self.view.rows):
            row = self.view.rows[rid]
            table = self.view.tables.get(row.table)
            if table is None or not isinstance(table.name, Known):
                continue
            spec = specs.get(table.name.value)
            if spec is None:
                continue
            if table.id not in columns:
                columns[table.id] = self.header(table, spec)
            found = columns[table.id]
            if found is None:
                continue
            event = self.row_event(spec, table, row, found)
            if event is not None:
                events.append(event)
        return events

    def header(self, table: StructuredTable, spec: parse.TableSpec) -> dict[str, int] | None:
        """Each column the spec names, by position; ``None`` (and a finding) when the table's
        header is not stated, lacks a column or names one twice."""
        if not isinstance(table.header, Known):
            problem = "the table states no header row, so no column can be found by name"
        else:
            names = list(table.header.value)
            missing = [c for c in spec.columns() if c not in names]
            repeated = [c for c in spec.columns() if names.count(c) > 1]
            if not missing and not repeated:
                return {c: names.index(c) for c in spec.columns()}
            problem = (
                f"the table lacks columns {missing}" if missing else f"columns {repeated} repeat"
            )
        self.findings.append(
            _finding(
                "table_unusable",
                f"event table {spec.name!r}: {problem}; none of its rows is read",
                (table.id,),
                table=spec.name,
            )
        )
        return None

    def row_event(
        self,
        spec: parse.TableSpec,
        table: StructuredTable,
        row: StructuredRecord,
        columns: Mapping[str, int],
    ) -> _Event | None:
        kind = row.provenance.assertion_kind

        def cell(name: str) -> tuple[Knowledge[CellValue], tuple[EvidenceRef, ...]]:
            index = columns[name]
            if index >= len(row.cells):  # a short row keeps its length: the cell is not there
                return Unknown(), ()
            return row.cells[index], (row.cell_evidence(table, index),)

        facts: list[_Fact] = [_Fact(EVIDENCED_BY, LedgerRecordRef(row.id), kind)]
        declared_kind, kind_evidence = cell(spec.kind)
        literal: TypedLiteral | None = None
        if isinstance(declared_kind, Known):
            literal = _verbatim(declared_kind.value)
            declared_as = _kind(declared_kind.provenance, kind)
        if literal is None:
            self.unstated[spec.name].append(row.id)
        else:
            facts.append(_Fact(DECLARED_KIND, literal, declared_as, kind_evidence))
            # Keyed by the declared type and value: the level 2 is never the text "2".
            section = parse.INTEGER if literal.datatype is ValueType.INTEGER else parse.TEXT
            key = (section, str(literal.value))
            mapping = self.config.vendors[spec.vendor]
            if key in mapping:
                target = mapping[key]
                if target is None:
                    return None  # the vendor mapping declares this kind is not an event
                facts.append(_Fact(EVENT_KIND, _text(target), declared_as, kind_evidence))
            else:
                self.unmapped[(spec.vendor, key)].append(row.id)
        clock, clock_evidence = self.clock(spec, cell)
        if clock is None:
            self.untimed(row.id, "the row names no clock its time is on")
            return None
        start, start_evidence = self.stamp(spec.at, clock, cell, row.id)
        if start is None:
            return None
        end: Timestamp | Open | None = None
        end_evidence: tuple[EvidenceRef, ...] = ()
        if spec.end is not None:
            end, end_evidence = self.stamp(spec.end, clock, cell, row.id, required=False)
            if end is None:
                states = [cell(c)[0] for c in spec.end.columns()]
                if any(isinstance(state, Ambiguous) for state in states):
                    end = self.open_end(
                        row.id, "end_ambiguous", "the row states its end ambiguously"
                    )
                elif all(not isinstance(state, Known) for state in states):
                    end = self.open_end(row.id, "end_unstated", "the row's end cell is blank")
                else:
                    end = self.open_end(
                        row.id, "end_unread", "the row's end is not a readable time"
                    )
        for spec_ids, predicate, node_type in (
            (spec.machine, INVOLVES, NodeType.MACHINE),
            (spec.site, AT_SITE, NodeType.SITE),
            (spec.zone, IN_ZONE, NodeType.ZONE),
        ):
            if spec_ids is None:
                continue
            value, evidence = cell(spec_ids.column)
            ids = self.cell_ids(value, spec_ids.namespace, row.id, spec_ids.column)
            facts.extend(
                _Fact(f.predicate, f.obj, f.kind, (*f.evidence, *evidence))
                for f in _ids(ids, predicate, node_type, kind)
            )
        # A severity is text or an integer level as declared; a description is text.
        for column, predicate, types in (
            (spec.severity, STATED_SEVERITY, {ValueType.TEXT, ValueType.INTEGER}),
            (spec.description, HAS_DESCRIPTION, {ValueType.TEXT}),
        ):
            if column is None:
                continue
            value, evidence = cell(column)
            if isinstance(value, Ambiguous):
                self.ambiguous[column].append(row.id)
            if not isinstance(value, Known):
                continue
            literal = _verbatim(value.value)
            if literal is not None and literal.datatype in types:
                facts.append(_Fact(predicate, literal, _kind(value.provenance, kind), evidence))
        event = _Event(
            event_node(row.id),
            str(row.provenance.evidence.source),
            (row.provenance.evidence,),
            (row.id, table.id),
            start,
            end,
            (*start_evidence, *end_evidence, *clock_evidence),
        )
        event.facts = facts
        return event

    def clock(
        self,
        spec: parse.TableSpec,
        cell: Callable[[str], tuple[Knowledge[CellValue], tuple[EvidenceRef, ...]]],
    ) -> tuple[RecordId | None, tuple[EvidenceRef, ...]]:
        if spec.clock.record is not None:
            return spec.clock.record, ()
        assert spec.clock.column is not None
        value, evidence = cell(spec.clock.column)
        if not isinstance(value, Known) or not isinstance(value.value, str):
            return None, ()
        try:
            return parse_record_id(value.value), evidence
        except ValueError:
            return None, ()

    def stamp(
        self,
        at: parse.TimeSpec,
        clock: RecordId,
        cell: Callable[[str], tuple[Knowledge[CellValue], tuple[EvidenceRef, ...]]],
        record: RecordId,
        *,
        required: bool = True,
    ) -> tuple[Timestamp | None, tuple[EvidenceRef, ...]]:
        """An instant from its cells: integer ticks as declared, or integer seconds and
        nanoseconds scaled exactly by the clock's stated resolution. Anything else is no time."""

        def integer(name: str) -> tuple[int | None, tuple[EvidenceRef, ...]]:
            value, evidence = cell(name)
            if isinstance(value, Known) and type(value.value) is int:
                return value.value, evidence
            return None, evidence

        why: str
        if at.ticks is not None:
            ticks, evidence = integer(at.ticks)
            if ticks is not None:
                try:
                    return Timestamp(ticks, clock), evidence
                except ValueError:
                    why = f"column {at.ticks!r} is outside the clock's tick range"
            else:
                why = f"column {at.ticks!r} holds no integer ticks"
        else:
            assert at.seconds is not None and at.nanoseconds is not None
            seconds, s_evidence = integer(at.seconds)
            nanos, n_evidence = integer(at.nanoseconds)
            evidence = (*s_evidence, *n_evidence)
            resolution = self.view.resolutions.get(clock)
            if seconds is None or nanos is None or not 0 <= nanos < 10**9:
                why = f"columns {at.seconds!r} and {at.nanoseconds!r} hold no seconds/nanoseconds"
            elif resolution is None:
                why = "the clock states no resolution, so seconds cannot become its ticks"
            else:
                ticks_exact = (Fraction(seconds) + Fraction(nanos, 10**9)) / resolution
                if ticks_exact.denominator != 1:
                    why = "the stamp is finer than the clock's stated resolution"
                else:
                    try:
                        return Timestamp(int(ticks_exact), clock), evidence
                    except ValueError:
                        why = "the stamp is outside the clock's tick range"
        if required:
            self.untimed(record, why)
        return None, ()

    def cell_ids(
        self, value: Knowledge[CellValue], namespace: str, record: RecordId, column: str
    ) -> Knowledge[LogicalId]:
        """A cell's id under the config's namespace; a blank, padded or non-text id is none."""

        def one(item: CellValue) -> LogicalId | None:
            if isinstance(item, bool) or not isinstance(item, str | int):
                return None
            try:
                return declared(LogicalId(namespace, str(item)))
            except (ValueError, TypeError):
                return None

        if isinstance(value, Known):
            found = one(value.value)
            if found is not None:
                return Known(found, value.provenance)
        elif isinstance(value, Ambiguous):
            candidates = [(one(c.value), c.provenance) for c in value.candidates]
            if all(c is not None for c, _ in candidates):
                return Ambiguous(tuple(Candidate(c, p) for c, p in candidates))  # type: ignore[arg-type]
        else:
            return Unknown()
        self.findings.append(
            _finding(
                "id_unusable",
                f"column {column!r} holds no usable declared id (blank, padded or not text)",
                (record,),
                Severity.INFO,
            )
        )
        return Unknown()

    def summarise(self) -> None:
        """One finding per unmapped kind, unstated kind and ambiguous field, with a count."""
        for (vendor, (section, declared_as)), rows in sorted(self.unmapped.items()):
            self.findings.append(
                _finding(
                    "kind_unmapped",
                    f"vendor {vendor!r} declares no event kind for the {section} {declared_as!r};"
                    " the events keep their declared kind and their event kind is Unknown",
                    sorted(rows)[:MAX_LISTED],
                    Severity.INFO,
                    vendor=vendor,
                    declared_kind=declared_as,
                    declared_type=section,
                    count=len(rows),
                )
            )
        for table, rows in sorted(self.unstated.items()):
            self.findings.append(
                _finding(
                    "kind_unstated",
                    f"rows of event table {table!r} state no kind (blank, ambiguous or not text);"
                    " their event kind is Unknown",
                    sorted(rows)[:MAX_LISTED],
                    Severity.INFO,
                    table=table,
                    count=len(rows),
                )
            )
        for name, records in sorted(self.ambiguous.items()):
            self.findings.append(
                _finding(
                    "value_ambiguous",
                    f"{name} is stated ambiguously; no reading is claimed",
                    sorted(set(records))[:MAX_LISTED],
                    Severity.INFO,
                    field=name,
                    count=len(set(records)),
                )
            )


# --- Where an event is in time ------------------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    """An event on one clock: claims hold over ``[start, end)``; its onset lies somewhere in
    ``[start, onset_end)`` (one tick when exact, wider through a mapping's rounding and residual
    bound). ``mapped`` is how many clock mappings placed it there; ``bounded`` is false when a
    mapping that placed it states no residual bound, so its error is unknown."""

    start: Timestamp
    end: Timestamp | Open
    onset_end: int
    mapped: int
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]
    bounded: bool = True


def _primary(view: _View, event: _Event) -> Placement | None:
    start = view.place(event.start)
    end: Timestamp | Open = OPEN
    if start.ticks < INT64_MAX:
        end = Timestamp(start.ticks + 1, start.domain_id)
    if isinstance(event.end, Open):
        end = OPEN
    elif event.end is not None and event.end != event.start:
        after = view.place(event.end)
        if after.domain_id != start.domain_id:
            view.findings.append(
                _finding(
                    "end_on_other_clock",
                    "the event's end is on another clock than its start; its end is open",
                    event.records[:1],
                    Severity.INFO,
                    event=event.node.node_id,
                )
            )
            end = OPEN
        elif not start < after:
            view.findings.append(
                _finding(
                    "inverted_interval",
                    "the event ends before it starts; nothing is placed on it",
                    event.records[:1],
                    event=event.node.node_id,
                )
            )
            return None
        else:
            end = after
    return Placement(
        start, end, start.ticks + 1, 0, (*event.evidence, *event.time_evidence), event.records
    )


def _projections(view: _View, event: _Event, primary: Placement) -> list[Placement]:
    """The event on each clock a stated mapping from its own clock reaches directly, computed
    exactly, rounded out to whole ticks and widened by the residual bound. A mapping that states
    no bound places the event unwidened, citing the mapping, and the placement is ``bounded=False``:
    its error is unknown, so nothing is decided by comparing it (co-occurrence is undecided and
    the mapping gets one ``bound_unstated`` finding). Chains are MVL-130's."""
    out: list[Placement] = []
    start = primary.start.ticks
    end = primary.end.ticks if isinstance(primary.end, Timestamp) else None
    span = RunPlacement(primary.start, primary.end, (), ())
    for mapping in view.mappings.get(event.start.domain_id, ()):
        anchor, rate = mapping.anchor, mapping.rate
        if not isinstance(anchor, Known) or not isinstance(rate, Known):
            continue
        # A window that does not cover the event: not used. An ambiguous window counts only
        # where every reading agrees (ADR 0009 §2, as runs read it).
        window = _definite(mapping.validity)
        if window is not None and window.ambiguous:
            view.findings.append(
                _finding(
                    "ambiguous_window",
                    "the clock mapping states its window ambiguously; only the part every reading"
                    " agrees on is used",
                    (*event.records[:1], mapping.id),
                    Severity.INFO,
                )
            )
        if window is not None and not _covers(window, span):
            continue
        civil = view.civil(mapping.target)
        target = mapping.target if civil is None else civil.domain_id
        if target == primary.start.domain_id:
            continue
        # An ambiguous bound widens by its largest reading: every reading stays inside. An
        # unstated one is unknown, never zero: the placement is marked unbounded.
        residual = mapping.residual_bound
        bounded = isinstance(residual, Known | Ambiguous)
        bound = (
            residual.value.ticks
            if isinstance(residual, Known)
            else max(c.value.ticks for c in residual.candidates)
            if isinstance(residual, Ambiguous)
            else 0
        )
        if not bounded:
            view.unbounded.add(mapping.id)
        origin, offset, slope = anchor.value.source.ticks, anchor.value.target.ticks, rate.value

        def to(
            ticks: int, origin: int = origin, offset: int = offset, slope: Fraction = slope
        ) -> Fraction:
            return offset + slope * (ticks - origin)

        lo = math.floor(to(start)) - bound
        onset_end = math.ceil(to(start + 1)) + bound
        try:
            placed_end: Timestamp | Open = (
                OPEN
                if end is None
                else Timestamp(max(math.ceil(to(end)) + bound, onset_end), target)
            )
            placed = Placement(
                Timestamp(lo, target),
                placed_end,
                onset_end,
                1,
                (*primary.evidence, mapping.provenance.evidence),
                (*primary.records, mapping.id, mapping.target),
                bounded,
            )
        except ValueError:
            view.findings.append(
                _finding(
                    "projection_out_of_range",
                    "the clock mapping places the event outside the target clock's range",
                    (*event.records[:1], mapping.id),
                    event=event.node.node_id,
                )
            )
            continue
        out.append(placed)
    return out


# --- Co-occurrence ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Reading:
    """Every placement of one event on one clock, merged: its onset lies in ``[lo, hi)``."""

    lo: int
    hi: int
    mapped: int
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]
    bounded: bool


def _readings(placements: Sequence[Placement]) -> dict[RecordId, _Reading]:
    by_clock: dict[RecordId, list[Placement]] = defaultdict(list)
    for place in placements:
        by_clock[place.start.domain_id].append(place)
    return {
        clock: _Reading(
            min(p.start.ticks for p in found),
            max(p.onset_end for p in found),
            max(p.mapped for p in found),
            tuple(ref for p in found for ref in p.evidence),
            tuple(rec for p in found for rec in p.records),
            all(p.bounded for p in found),
        )
        for clock, found in by_clock.items()
    }


def _window_ticks(view: _View, clock: RecordId, window: Fraction) -> int | str:
    """The window in the clock's ticks, rounded down; or why it cannot be measured there."""
    resolution = view.resolutions.get(clock)
    if resolution is None:
        return "window_unscaled"
    ticks = math.floor(window / resolution)
    return ticks if ticks >= 1 else "window_below_resolution"


_UNMEASURED: Final[Mapping[str, str]] = {
    "window_unscaled": "the clock states no resolution, so the window cannot be measured on it",
    "window_below_resolution": "the window is shorter than one tick of the clock",
}


@dataclass(frozen=True)
class _Pair:
    """Two events decided to co-occur on ``clock``: the window ``[lo, lo + size)`` there."""

    seconds: Fraction  # how far apart their onsets are: the nearest are kept first
    first: int
    second: int
    clock: RecordId
    lo: int
    size: int

    def key(self, events: Sequence[_Event]) -> tuple[Fraction, str, str]:
        a, b = events[self.first].node.node_id, events[self.second].node.node_id
        return (self.seconds, min(a, b), max(a, b))


def _candidates(
    events: Sequence[_Event],
    entries: Sequence[tuple[int, int, int]],
    size: int,
    limit: int,
) -> tuple[set[tuple[int, int]], set[int]]:
    """Pairs of events from different sources whose onset ranges may fall in one window, and the
    events that had more than ``limit`` such later partners on this clock.

    ``entries`` are ``(lo, hi, event)`` sorted by onset. A later onset ``b`` can share a window with
    ``a`` only while ``b.lo < a.hi - 1 + size``, so the scan stops there; a run of ``a``'s own
    source is skipped in one step, and at most ``limit`` partners are taken per event (nearest
    first), so the work is bounded by events times limit, never by events squared."""
    sources = [events[i].source for _, _, i in entries]
    skip = list(range(1, len(entries) + 1))  # the next entry after a run of one source
    for k in range(len(entries) - 2, -1, -1):
        if sources[k + 1] == sources[k]:
            skip[k] = skip[k + 1]
    pairs: set[tuple[int, int]] = set()
    capped: set[int] = set()
    for a, (_, hi_a, i) in enumerate(entries):
        taken, b = 0, a + 1
        while b < len(entries):
            lo_b, _, j = entries[b]
            if lo_b >= hi_a - 1 + size:
                break
            if sources[b] == sources[a]:
                b = skip[b]
                continue
            if taken == limit:
                capped.add(i)
                break
            pairs.add((min(i, j), max(i, j)))
            taken += 1
            b += 1
    return pairs, capped


def _co_occurrence(
    view: _View,
    config: parse.EventConfig,
    events: Sequence[_Event],
    placements: Sequence[Sequence[Placement]],
) -> list[ClaimDraft]:
    """``co_occurs_within`` both ways between events of different sources whose onsets fit in one
    window on the clock that relates them with the fewest mappings (ties: the clock's id)."""
    window = config.window
    if window is None:
        return []
    readings = [_readings(found) for found in placements]
    by_clock: dict[RecordId, list[tuple[int, int, int]]] = defaultdict(list)
    for index, found in enumerate(readings):
        for clock, reading in found.items():
            by_clock[clock].append((reading.lo, reading.hi, index))
    ticks = {clock: _window_ticks(view, clock, window) for clock in by_clock}
    pairs: set[tuple[int, int]] = set()
    capped: set[int] = set()
    for clock in sorted(by_clock):
        entries = sorted(by_clock[clock])
        size = ticks[clock]
        if isinstance(size, str):
            if len({events[i].source for _, _, i in entries}) > 1:
                view.findings.append(
                    _finding(
                        size,
                        f"{_UNMEASURED[size]}; events from several sources on it are not compared"
                        " there",
                        severity=Severity.INFO,
                        clock=clock,
                    )
                )
            continue
        near, over = _candidates(events, entries, size, config.max_partners)
        pairs |= near
        capped |= over
    _unrelated(view, events, readings)
    decided: list[_Pair] = []
    undecided: dict[RecordId, list[tuple[int, int]]] = defaultdict(list)
    unbounded: dict[RecordId, list[tuple[int, int]]] = defaultdict(list)
    for i, j in sorted(pairs):
        shared = [c for c in set(readings[i]) & set(readings[j]) if isinstance(ticks[c], int)]

        def rank(c: RecordId, i: int = i, j: int = j) -> tuple[bool, int, RecordId]:
            a, b = readings[i][c], readings[j][c]
            return (not (a.bounded and b.bounded), a.mapped + b.mapped, c)

        clock = min(shared, key=rank)
        one, two, size = readings[i][clock], readings[j][clock], ticks[clock]
        assert isinstance(size, int)
        lo, hi = min(one.lo, two.lo), max(one.hi, two.hi)
        overlap = one.lo < two.hi and two.lo < one.hi
        nearest = 0 if overlap else max(one.lo, two.lo) - (min(one.hi, two.hi) - 1)
        if not (one.bounded and two.bounded):
            # A mapping with no stated bound: its error is unknown, never zero (non-negotiable 4).
            if nearest < size:
                unbounded[clock].append((i, j))
            continue
        if hi - lo <= size:
            seconds = abs(one.lo - two.lo) * view.resolutions[clock]
            decided.append(_Pair(seconds, i, j, clock, lo, size))
            continue
        if nearest < size:
            undecided[clock].append((i, j))
    for clock, found_pairs in sorted(undecided.items()):
        involved = sorted({events[k].records[0] for pair in found_pairs for k in pair})
        view.findings.append(
            _finding(
                "co_occurrence_undecided",
                f"on this clock {len(found_pairs)} pairs of events may or may not fall in one"
                " window: the mapping's rounding or residual bound is too coarse to decide; no"
                " co-occurrence is claimed for them",
                involved[:MAX_LISTED],
                Severity.INFO,
                clock=clock,
                pairs=len(found_pairs),
            )
        )
    for clock, found_pairs in sorted(unbounded.items()):
        involved = sorted({events[k].records[0] for pair in found_pairs for k in pair})
        view.findings.append(
            _finding(
                "co_occurrence_unbounded",
                f"on this clock {len(found_pairs)} pairs of events may fall in one window on the"
                " clock mapping's own reading, but it states no residual bound, so its error is"
                " unknown; no co-occurrence is claimed for them",
                involved[:MAX_LISTED],
                Severity.INFO,
                clock=clock,
                pairs=len(found_pairs),
            )
        )
    return _emit_pairs(view, config, events, readings, decided, capped)


def _emit_pairs(
    view: _View,
    config: parse.EventConfig,
    events: Sequence[_Event],
    readings: Sequence[Mapping[RecordId, _Reading]],
    decided: list[_Pair],
    capped: set[int],
) -> list[ClaimDraft]:
    """The decided pairs nearest first (in seconds), at most ``max_partners`` per event."""
    drafts: list[ClaimDraft] = []
    partners: dict[int, int] = defaultdict(int)
    for pair in sorted(decided, key=lambda p: p.key(events)):
        i, j = pair.first, pair.second
        full = [k for k in (i, j) if partners[k] >= config.max_partners]
        if full:
            capped.update(full)
            continue
        try:
            start = Timestamp(pair.lo, pair.clock)
            end = Timestamp(pair.lo + pair.size, pair.clock)
        except ValueError:
            view.findings.append(
                _finding(
                    "projection_out_of_range",
                    "the co-occurrence window ends outside the clock's range",
                    (*events[i].records[:1], *events[j].records[:1]),
                )
            )
            continue
        partners[i] += 1
        partners[j] += 1
        a, b = readings[i][pair.clock], readings[j][pair.clock]
        evidence = (*a.evidence, *b.evidence)
        records = (*a.records, *b.records)
        for subject, other in ((i, j), (j, i)):
            drafts.append(
                ClaimDraft(
                    subject=events[subject].node,
                    predicate=CO_OCCURS_WITHIN,
                    object=events[other].node,
                    valid_from=start,
                    valid_to=end,
                    assertion_kind=OBSERVED,
                    evidence=evidence,
                    records=records,
                )
            )
    for index in sorted(capped):
        view.findings.append(
            _finding(
                "co_occurrence_capped",
                f"the event may co-occur with more than {config.max_partners} events; the"
                " nearest are claimed and the rest are not",
                events[index].records[:1],
                event=events[index].node.node_id,
            )
        )
    return drafts


def _unrelated(
    view: _View, events: Sequence[_Event], readings: Sequence[Mapping[RecordId, _Reading]]
) -> None:
    """A finding per pair of event clocks with events no stated mapping places on a shared clock:
    those events are never compared, so whether they co-occur is Unknown.

    Events are grouped by their own clock and the clocks they reach, so a mapping whose window
    covers some events of a clock and not others still names the uncovered ones (``partial``),
    and the usual case (every event reaches one civil clock) costs one comparison. The first
    ``MAX_LISTED`` clock pairs are named; one more finding says that others exist."""
    groups: dict[tuple[RecordId, frozenset[RecordId]], list[int]] = defaultdict(list)
    for index, (event, found) in enumerate(zip(events, readings, strict=True)):
        groups[(view.place(event.start).domain_id, frozenset(found))].append(index)
    keys = sorted(groups, key=lambda g: (g[0], sorted(g[1])))
    uncompared: dict[tuple[RecordId, RecordId], list[set[int]]] = {}
    related: set[tuple[RecordId, RecordId]] = set()
    for n, one in enumerate(keys):
        for two in keys[n + 1 :]:
            if one[0] == two[0]:
                continue
            pair = (min(one[0], two[0]), max(one[0], two[0]))
            if one[1] & two[1]:
                related.add(pair)
                continue
            first, second = groups[one], groups[two]
            if len({events[k].source for k in (*first, *second)}) < 2:
                continue
            if pair not in uncompared and len(uncompared) == MAX_LISTED:
                view.findings.append(
                    _finding(
                        "clocks_unrelated",
                        f"more than {MAX_LISTED} pairs of event clocks share no stated mapping;"
                        " the rest are not listed",
                        severity=Severity.INFO,
                    )
                )
                return _report_unrelated(view, uncompared, related)
            sides = uncompared.setdefault(pair, [set(), set()])
            a, b = (first, second) if one[0] == pair[0] else (second, first)
            sides[0].update(a)
            sides[1].update(b)
    _report_unrelated(view, uncompared, related)


def _report_unrelated(
    view: _View,
    uncompared: Mapping[tuple[RecordId, RecordId], list[set[int]]],
    related: set[tuple[RecordId, RecordId]],
) -> None:
    for (a, b), (left, right) in sorted(uncompared.items()):
        partial = (a, b) in related
        view.findings.append(
            _finding(
                "clocks_unrelated",
                (
                    "a stated clock mapping relates only some events on these clocks (its window"
                    " does not cover the others); the uncovered events are never compared, so"
                    " whether they co-occur is Unknown"
                    if partial
                    else "no stated clock mapping relates these clocks; events on one are never"
                    " compared with events on the other, so whether they co-occur is Unknown"
                ),
                severity=Severity.INFO,
                clocks=[a, b],
                events=[len(left), len(right)],
                partial=partial,
            )
        )


# --- The consolidator ---------------------------------------------------------------------------


class EventConsolidator:
    """Deterministic event claims (ADR 0013). Its config is ``event_records.resolve_config``'s."""

    consolidator_id: Final = EVENTS_CONSOLIDATOR_ID
    # 2: maintenance events and their actions, status reports, stated causes (ADR 0025).
    # 3: a lifecycle record's one declared id value names its event (has_name, ADR 0026).
    version: Final = "3"
    model: Final[ModelRef | None] = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        view = _read(ledger)
        parsed = parse.parse_config(config)
        for problem in parsed.problems:
            view.findings.append(_finding("invalid_config", problem, severity=Severity.ERROR))
        builder = _Builder(view, parsed)
        events: list[_Event] = []
        for rid in sorted(view.incidents):
            events.extend(builder.incident(view.incidents[rid]))
        for rid in sorted(view.interventions):
            events.extend(builder.intervention(view.interventions[rid]))
        for rid in sorted(view.maintenance):
            events.extend(builder.maintenance(view.maintenance[rid]))
        for rid in sorted(view.statuses):
            events.extend(builder.status(view.statuses[rid]))
        events.extend(builder.tables())
        builder.summarise()
        drafts: list[ClaimDraft] = []
        placed: list[_Event] = []
        placements: list[list[Placement]] = []
        for event in sorted(events, key=lambda e: e.node.node_id):
            primary = _primary(view, event)
            if primary is None:
                continue
            found = [primary, *_projections(view, event, primary)]
            placed.append(event)
            placements.append(found)
            for place in found:
                for fact in event.facts:
                    drafts.append(
                        ClaimDraft(
                            subject=event.node,
                            predicate=fact.predicate,
                            object=fact.obj,
                            valid_from=place.start,
                            valid_to=place.end,
                            assertion_kind=fact.kind,
                            evidence=(*event.evidence, *fact.evidence, *place.evidence),
                            records=(*event.records, *fact.records, *place.records),
                        )
                    )
        drafts.extend(_co_occurrence(view, parsed, placed, placements))
        for mapping in sorted(view.unbounded):
            view.findings.append(
                _finding(
                    "bound_unstated",
                    "the clock mapping states no residual bound: events it places are not widened"
                    " (the claims cite it), and nothing is decided by comparing them",
                    (mapping,),
                    Severity.INFO,
                )
            )
        return ConsolidatorOutput(tuple(drafts), tuple(view.findings))
