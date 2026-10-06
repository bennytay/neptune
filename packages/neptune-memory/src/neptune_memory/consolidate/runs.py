"""Run threads as a deterministic consolidator (ADR 0009).

One ``run`` node per run thread: a compiler ``Run`` keyed by its declared logical id, else by its
record (``record:<id>``), so Run records with one declared id in any number of packages are one
node (ADR 0003 §1.1). Each claim about a run holds over the run's interval: ``[first, last]`` on
the clock its record states them on, and again on a civil clock where the evidence maps that clock
there (a ``timestamp_domain`` that declares itself civil, or a stated ``clock_mapping``).

- ``evidenced_by``: the run's ``Run`` and ``RunAssembly`` records.
- ``has_member``: each file a ``RunAssembly`` places in the run, citing the membership's evidence,
  the assembly and its producer's transform (the grouping rule's version).
- ``continues`` / ``continues_candidate``: between the recording parts of one assembled run, by
  their stated times: the next part continues the one that ends before it starts; parts that
  overlap are concurrent and parts whose times cannot be compared are candidates both ways.
- ``recorded_by``, ``at_site``, ``executes_task`` (the issue's ``performed_by``, ``at_site``,
  ``under_task``) from the run's declared machine and manifest declarations: ``Known`` when every
  ground names one id, a ``*_candidate`` claim per reading when they disagree or a field is
  ``Ambiguous``, and no claim when nothing is stated (``involvement`` reads them back as
  ``Knowledge``).

Records are parsed by ``consolidate.run_records``; this module decides. Malformed or contradictory
input is a finding and never a claim, and the rest of the build is unaffected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from neptune.identity import canonical_json
from neptune.model.alignment import ClockMapping, MemberRole, RunAssembly, ValidityWindow
from neptune.model.finding import Severity
from neptune.model.ids import LogicalId
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import Provenance
from neptune.model.run import Run, RunDeclaration
from neptune.model.source import SourceRevision
from neptune.model.time import INT64_MAX, Timestamp
from neptune.model.world import Site
from neptune_memory.consolidate import run_records as parse
from neptune_memory.consolidate.base import ClaimDraft, ConsolidationFinding, ConsolidatorOutput
from neptune_memory.consolidate.identity import node_ref
from neptune_memory.schema.claim import LedgerRecordRef
from neptune_memory.schema.interval import OPEN, CivilClock, Open
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import (
    AT_SITE,
    CANDIDATE_OF,
    CONTINUES,
    CORE_PREDICATES,
    EXECUTES_TASK,
    HAS_MEMBER,
    RECORDED_BY,
    Cardinality,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from neptune.model.alignment import RunMember
    from neptune.model.ids import ContentId, RecordId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge, ProvenanceSlot
    from neptune.model.provenance import EvidenceRef
    from neptune_memory.consolidate.base import ModelRef
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim

RUNS_CONSOLIDATOR_ID: Final = "memory.runs"
EVIDENCED_BY: Final = "evidenced_by"
OBSERVED: Final = AssertionKind.OBSERVED
STATED: Final = AssertionKind.STATED

# What each declared role names, and the predicate a Known reading of it becomes.
_ROLES: Final = (
    ("machine", RECORDED_BY, NodeType.MACHINE),
    ("site", AT_SITE, NodeType.SITE),
    ("task", EXECUTES_TASK, NodeType.TASK),
)


def _declared_id(run: Run) -> LogicalId | None:
    """The run's declared logical id, unless it is in the namespace record-keyed runs use: a
    declared ``record:<id>`` could otherwise forge another run's node (ADR 0009 §2)."""
    if (
        isinstance(run.logical_id, Known)
        and run.logical_id.value.namespace != parse.RECORD_NAMESPACE
    ):
        return run.logical_id.value
    return None


def run_node(run: Run) -> NodeRef:
    """A run's node: its declared logical id, else ``record:<run record id>`` (ADR 0009 §2)."""
    declared = _declared_id(run)
    if declared is not None:
        return node_ref(NodeType.RUN, declared)
    return node_ref(NodeType.RUN, LogicalId(parse.RECORD_NAMESPACE, run.id))


def _key(node: LogicalId) -> bytes:
    return canonical_json.dumps(node.to_json())


def _finding(
    code: str,
    message: str,
    records: Iterable[RecordId] = (),
    severity: Severity = Severity.WARNING,
    **details: JsonValue,
) -> ConsolidationFinding:
    return ConsolidationFinding(
        code=f"runs.{code}",
        severity=severity,
        message=message,
        records=tuple(records),
        details=details,
    )


def _malformed(kind: str, package_id: str, index: int, reason: str) -> ConsolidationFinding:
    try:
        reason.encode("utf-8")
    except UnicodeEncodeError:
        reason = "unrepresentable text"
    return _finding(
        "malformed_record",
        f"{kind} record {index} in package {package_id!r} is malformed: {reason}"[:1000],
        severity=Severity.ERROR,
        index=index,
        kind=kind,
        package_id=package_id,
    )


def _cited(slot: ProvenanceSlot) -> tuple[EvidenceRef, ...]:
    """The evidence a value's own provenance cites; nothing when it inherits its record's."""
    return (slot.evidence,) if isinstance(slot, Provenance) else ()


def _kind(slot: ProvenanceSlot, default: AssertionKind) -> AssertionKind:
    return slot.assertion_kind if isinstance(slot, Provenance) else default


# --- Reading the Ledger -------------------------------------------------------------------------


@dataclass
class _View:
    runs: dict[RecordId, Run] = field(default_factory=dict)  # admitted, any package
    # The runs each package declares from each source's bytes: how a member finds its run.
    declared_from: dict[tuple[str, ContentId], set[RecordId]] = field(default_factory=dict)
    assemblies: list[tuple[str, RunAssembly]] = field(default_factory=list)  # by (id, package)
    revisions: dict[tuple[str, RecordId], ContentId] = field(default_factory=dict)
    clocks: dict[RecordId, CivilClock] = field(default_factory=dict)
    mappings: dict[RecordId, list[ClockMapping]] = field(default_factory=dict)  # by source clock
    sites: set[bytes] = field(default_factory=set)  # every id the site register declares
    site_register: bool = False  # whether any site record is in the Ledger
    declarations: list[RunDeclaration] = field(default_factory=list)  # by record id
    findings: list[ConsolidationFinding] = field(default_factory=list)
    civil: set[RecordId] = field(default_factory=set)  # the civil clocks' domain ids

    def place(self, stamp: Timestamp) -> Timestamp:
        """A stamp on a clock that declares itself civil, on that ``CivilClock`` (the same
        instant, the same ticks: ADR 0002 §3); any other stamp as declared."""
        clock = self.clocks.get(stamp.domain_id)
        return stamp if clock is None else clock.at(stamp.ticks)

    def is_civil(self, domain: RecordId) -> bool:
        return domain in self.civil


class _Admitted:
    """Records de-duplicated by key across packages: one key with two contents is dropped."""

    def __init__(self, findings: list[ConsolidationFinding]) -> None:
        self.seen: dict[tuple[str, ...], object] = {}
        self.conflicted: set[tuple[str, ...]] = set()
        self._findings = findings

    def admit(self, key: tuple[str, ...], rid: RecordId, parsed: object) -> bool:
        if key in self.conflicted:
            return False
        previous = self.seen.setdefault(key, parsed)
        if previous == parsed:
            return True
        self.conflicted.add(key)
        self._findings.append(
            _finding(
                "record_conflict",
                "one record id carries different content in two places; record not used",
                (rid,),
                Severity.ERROR,
            )
        )
        return False

    def kept(self) -> dict[tuple[str, ...], object]:
        return {k: v for k, v in self.seen.items() if k not in self.conflicted}


# The kinds this consolidator reads, in the order it reads them from each package.
_PARSERS: Final[Mapping[str, Callable[[Mapping[str, object]], object]]] = {
    parse.TIMESTAMP_DOMAIN: parse.clock,
    parse.SOURCE_REVISION: parse.revision,
    parse.RUN: parse.run,
    parse.RUN_ASSEMBLY: parse.assembly,
    parse.CLOCK_MAPPING: parse.mapping,
    parse.SITE: parse.site,
    parse.RUN_DECLARATION: parse.declaration,
}


def _record_id(parsed: object) -> RecordId:
    if isinstance(parsed, parse.Clock):
        return parsed.record
    assert isinstance(
        parsed, Run | RunAssembly | SourceRevision | ClockMapping | Site | RunDeclaration
    )
    return parsed.id


def _admission_key(package_id: str, parsed: object) -> tuple[str, ...]:
    """What one record is de-duplicated by. An assembly's id is its file list's evidence id
    (root ADR 0066 §1), so two uploads of one bag share it with different members: assemblies
    and revisions are keyed within their package; every other record by id across packages."""
    kind, rid = type(parsed).__name__, _record_id(parsed)
    if isinstance(parsed, RunAssembly | SourceRevision):
        return (kind, package_id, rid)
    return (kind, rid)


def _read(ledger: LedgerReader) -> _View:
    view = _View()
    admitted = _Admitted(view.findings)
    homes: dict[RecordId, list[str]] = {}  # run -> packages declaring it
    for ref in ledger.list_packages():
        for kind, parser in _PARSERS.items():
            for index, record in enumerate(ledger.read_records(ref.package_id, kind) or ()):
                try:
                    parsed = parser(record)
                except parse.Inferred:
                    view.findings.append(
                        _finding(
                            "inferred_record",
                            f"{kind} record {index} in package {ref.package_id!r} is inferred: "
                            "a derived/ record, never a ground for a run claim",
                            severity=Severity.INFO,
                            index=index,
                            kind=kind,
                            package_id=ref.package_id,
                        )
                    )
                    continue
                except parse.Malformed as exc:
                    view.findings.append(_malformed(kind, ref.package_id, index, str(exc)))
                    continue
                key = _admission_key(ref.package_id, parsed)
                if admitted.admit(key, _record_id(parsed), parsed) and isinstance(parsed, Run):
                    homes.setdefault(parsed.id, []).append(ref.package_id)
    for key, parsed in sorted(admitted.kept().items()):
        if isinstance(parsed, Run):
            view.runs[parsed.id] = parsed
            source = parsed.provenance.evidence.source
            if isinstance(source, str):
                for package_id in homes[parsed.id]:
                    view.declared_from.setdefault((package_id, source), set()).add(parsed.id)
        elif isinstance(parsed, RunAssembly):
            view.assemblies.append((key[1], parsed))
        elif isinstance(parsed, SourceRevision):
            view.revisions[(key[1], parsed.id)] = parsed.content_id
        elif isinstance(parsed, parse.Clock):
            if parsed.civil is not None:
                view.clocks[parsed.record] = parsed.civil
        elif isinstance(parsed, ClockMapping):
            view.mappings.setdefault(parsed.source, []).append(parsed)
        elif isinstance(parsed, Site):
            view.site_register = True
            for identifier in parsed.identifiers:
                if isinstance(identifier, Known):
                    view.sites.add(_key(identifier.value))
        elif isinstance(parsed, RunDeclaration):
            view.declarations.append(parsed)
    view.civil = {clock.domain_id for clock in view.clocks.values()}
    view.assemblies.sort(key=lambda item: (item[1].id, item[0]))
    view.declarations.sort(key=lambda d: d.id)
    for found in view.mappings.values():
        found.sort(key=lambda m: m.id)
    return view


# --- Where a run is in time ---------------------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    """A run's interval ``[start, end)`` on one clock and what places it there: the run's own
    record (its first and last instants), and any clock mapping that projects it."""

    start: Timestamp
    end: Timestamp | Open
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]


def _primary(view: _View, run: Run) -> Placement | None:
    """``[first, last + 1 tick)`` on the clock ``first`` is on; ``None`` when no first instant is
    stated. A last instant on another clock, or none, leaves the end open."""
    if not isinstance(run.first, Known):
        return None
    start = view.place(run.first.value)
    evidence = [run.provenance.evidence, *_cited(run.first.provenance)]
    end: Timestamp | Open = OPEN
    if isinstance(run.last, Known) and run.last.value.ticks < INT64_MAX:
        last = run.last.value
        after = view.place(Timestamp(last.ticks + 1, last.domain_id))
        if after.domain_id != start.domain_id:
            view.findings.append(
                _finding(
                    "end_on_other_clock",
                    "the run's last instant is on another clock than its first; its end is open",
                    (run.id,),
                    Severity.INFO,
                )
            )
        elif not start < after:
            view.findings.append(
                _finding(
                    "inverted_interval",
                    "the run's last instant is before its first; nothing is placed on it",
                    (run.id,),
                )
            )
            return None
        else:
            end = after
            evidence.extend(_cited(run.last.provenance))
    return Placement(start, end, tuple(evidence), (run.id,))


def _hull(placements: Sequence[Placement]) -> Placement | None:
    """The span of parts' placements when they are all on one clock; else ``None``."""
    if not placements or len({p.start.domain_id for p in placements}) != 1:
        return None
    ends = [p.end for p in placements]
    end: Timestamp | Open = (
        OPEN
        if any(isinstance(e, Open) for e in ends)
        else max((e for e in ends if isinstance(e, Timestamp)), key=lambda e: e.ticks)
    )
    return Placement(
        min((p.start for p in placements), key=lambda s: s.ticks),
        end,
        tuple(ref for p in placements for ref in p.evidence),
        tuple(rid for p in placements for rid in p.records),
    )


@dataclass(frozen=True)
class _Window:
    """The part of a stated window every reading of it agrees on: ``[start, end)`` on ``clock``
    (``None`` is a bound no reading states). ``ambiguous`` when the window or a bound has several
    readings: the window is then their intersection, never one reading picked."""

    clock: RecordId
    start: Timestamp | None
    end: Timestamp | None
    ambiguous: bool


def _bound(bound: Knowledge[Timestamp]) -> tuple[list[Timestamp], bool]:
    if isinstance(bound, Known):
        return [bound.value], False
    if isinstance(bound, Ambiguous):
        return [c.value for c in bound.candidates], True
    return [], False  # unstated: open on that side (ADR 0008 §2)


def _definite(validity: Knowledge[ValidityWindow]) -> _Window | None:
    """``None`` when no window is stated (the relation holds as stated, unbounded). An
    ``Ambiguous`` window, or one with an ``Ambiguous`` bound, is the intersection of its
    readings: the latest start and the earliest end any reading states. Readings on different
    clocks share no instant: the window is then empty."""
    if isinstance(validity, Known):
        windows, ambiguous = [validity.value], False
    elif isinstance(validity, Ambiguous):
        windows, ambiguous = [c.value for c in validity.candidates], True
    else:
        return None
    clock = windows[0].clock
    if any(w.clock != clock for w in windows):
        return _Window(clock, Timestamp(0, clock), Timestamp(0, clock), True)
    starts: list[Timestamp] = []
    ends: list[Timestamp] = []
    for window in windows:
        found, several = _bound(window.start)
        starts += found
        ambiguous |= several
        found, several = _bound(window.end)
        ends += found
        ambiguous |= several
    return _Window(
        clock,
        max(starts, key=lambda t: t.ticks) if starts else None,
        min(ends, key=lambda t: t.ticks) if ends else None,
        ambiguous,
    )


def _covers(window: _Window, placement: Placement) -> bool:
    if window.start is not None and placement.start.ticks < window.start.ticks:
        return False
    if window.end is not None:
        return isinstance(placement.end, Timestamp) and placement.end.ticks <= window.end.ticks
    return True


def _ambiguous_window(records: Sequence[RecordId], what: str) -> ConsolidationFinding:
    return _finding(
        "ambiguous_window",
        f"the {what} states its window ambiguously; only the part every reading agrees on is used",
        records,
        Severity.INFO,
    )


def _projections(view: _View, placement: Placement) -> list[Placement]:
    """The placement on each civil clock a stated mapping from its clock reaches, rounded out to
    whole ticks and widened by the residual bound where the mapping states one (an unstated bound
    widens nothing; the claim cites the mapping). Only direct mappings, in their stated
    direction; chains are MVL-130's."""
    if view.is_civil(placement.start.domain_id):
        return []
    out: list[Placement] = []
    for mapping in view.mappings.get(placement.start.domain_id, ()):
        civil = view.clocks.get(mapping.target)
        anchor, rate = mapping.anchor, mapping.rate
        if civil is None or not isinstance(anchor, Known) or not isinstance(rate, Known):
            continue
        window = _definite(mapping.validity)
        if window is not None and window.ambiguous:
            view.findings.append(
                _ambiguous_window((*placement.records, mapping.id), "clock mapping")
            )
        if window is not None and not _covers(window, placement):
            continue
        bound = (
            mapping.residual_bound.value.ticks if isinstance(mapping.residual_bound, Known) else 0
        )
        origin, target, slope = anchor.value.source.ticks, anchor.value.target.ticks, rate.value
        try:
            start = civil.at(math.floor(target + slope * (placement.start.ticks - origin)) - bound)
            end: Timestamp | Open = (
                OPEN
                if isinstance(placement.end, Open)
                else civil.at(math.ceil(target + slope * (placement.end.ticks - origin)) + bound)
            )
        except ValueError:
            view.findings.append(
                _finding(
                    "projection_out_of_range",
                    "the clock mapping projects the run outside the civil clock's range",
                    (*placement.records, mapping.id),
                )
            )
            continue
        out.append(
            Placement(
                start,
                end,
                (*placement.evidence, mapping.provenance.evidence),
                (*placement.records, mapping.id, mapping.target),
            )
        )
    return out


# --- Assemblies and their parts -----------------------------------------------------------------


@dataclass(frozen=True)
class _Link:
    """One recording part of an assembled run: the part's run, and what places it there."""

    part: Run
    member: RunMember
    assembly: RunAssembly

    @property
    def evidence(self) -> tuple[EvidenceRef, ...]:
        return (self.member.evidence, self.assembly.provenance.evidence)

    @property
    def records(self) -> tuple[RecordId, ...]:
        return (self.assembly.id, self.assembly.provenance.transform)


def _links(view: _View, package_id: str, assembly: RunAssembly) -> list[_Link]:
    """The runs a recording member's bytes declare in the assembly's own package."""
    out: list[_Link] = []
    for member in assembly.members:
        if member.role is not MemberRole.RECORDING:
            continue
        content = view.revisions.get((package_id, member.revision))
        if content is None:
            continue
        for rid in sorted(view.declared_from.get((package_id, content), ())):
            if rid != assembly.run:
                out.append(_Link(view.runs[rid], member, assembly))
    return out


# --- Declared roles -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Reading:
    node: LogicalId
    evidence: tuple[EvidenceRef, ...]
    kind: AssertionKind


@dataclass(frozen=True)
class _Ground:
    """What one record says a run's machine, site or task is: one reading when it decides, every
    candidate when the record leaves it ``Ambiguous``."""

    records: tuple[RecordId, ...]
    evidence: tuple[EvidenceRef, ...]
    readings: tuple[_Reading, ...]
    decided: bool


def _ground(
    knowledge: Knowledge[LogicalId],
    records: tuple[RecordId, ...],
    evidence: tuple[EvidenceRef, ...],
    kind: AssertionKind,
) -> _Ground | None:
    if isinstance(knowledge, Known):
        reading = _Reading(
            knowledge.value, _cited(knowledge.provenance), _kind(knowledge.provenance, kind)
        )
        return _Ground(records, evidence, (reading,), True)
    if isinstance(knowledge, Ambiguous):
        readings = tuple(
            _Reading(c.value, _cited(c.provenance), _kind(c.provenance, kind))
            for c in knowledge.candidates
        )
        return _Ground(records, evidence, readings, False)
    return None  # Unknown, NotCovered, KnownAbsent, NotApplicable: nothing is stated


Pairs = list[tuple[_Ground, _Reading]]


def _decide(grounds: Sequence[_Ground], many: bool) -> tuple[Pairs, Pairs, int]:
    """``(known, candidates, distinct decided ids)`` for one role of one run.

    A ``one`` role is known when the deciding grounds name one id and every ambiguous ground has
    it among its candidates; otherwise every reading of every ground is a candidate. A ``many``
    role (``executes_task``) holds every decided id, so decided grounds never disagree; an
    ambiguous ground none of whose readings is decided adds its readings as candidates. No
    ground: nothing (``Unknown``)."""
    decided = [(g, g.readings[0]) for g in grounds if g.decided]
    keys = {_key(r.node) for _, r in decided}
    if many:
        candidates = [
            (g, r)
            for g in grounds
            if not g.decided and not keys & {_key(r.node) for r in g.readings}
            for r in g.readings
        ]
        return decided, candidates, len(keys)
    if len(keys) == 1:
        (only,) = keys
        if all(only in {_key(r.node) for r in g.readings} for g in grounds if not g.decided):
            return decided, [], 1
    return [], [(g, r) for g in grounds for r in g.readings], len(keys)


# --- The policy ---------------------------------------------------------------------------------


@dataclass
class _Build:
    view: _View
    placements: dict[RecordId, list[Placement]] = field(default_factory=dict)
    order: dict[RecordId, Placement] = field(default_factory=dict)  # where parts are compared
    drafts: list[ClaimDraft] = field(default_factory=list)

    def emit(
        self,
        subject: NodeRef,
        predicate: str,
        obj: NodeRef | LedgerRecordRef,
        kind: AssertionKind,
        evidence: Iterable[EvidenceRef],
        records: Iterable[RecordId],
        over: Iterable[Placement],
    ) -> None:
        evidence, records = tuple(evidence), tuple(records)
        for place in over:
            self.drafts.append(
                ClaimDraft(
                    subject=subject,
                    predicate=predicate,
                    object=obj,
                    valid_from=place.start,
                    valid_to=place.end,
                    assertion_kind=kind,
                    evidence=(*evidence, *place.evidence),
                    records=(*records, *place.records),
                )
            )


class RunConsolidator:
    """Deterministic run-thread claims (ADR 0009). Takes no configuration.

    Version 2 (ADR 0020) reads the compiler's ``run_declaration`` (package-schema 9); version 1 read
    ADR 0009's stand-in, so its claims are another lineage.
    """

    consolidator_id: Final = RUNS_CONSOLIDATOR_ID
    version: Final = "2"
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
                    "the run consolidator takes no configuration; keys ignored",
                    keys=sorted(config),
                )
            )
        build = _Build(view)
        nodes: dict[NodeRef, list[Run]] = {}
        for rid in sorted(view.runs):
            run = view.runs[rid]
            nodes.setdefault(run_node(run), []).append(run)
            if isinstance(run.logical_id, Ambiguous):
                view.findings.append(
                    _finding(
                        "ambiguous_run_id",
                        "the run's declared id is ambiguous; it is keyed by its record",
                        (rid,),
                        Severity.INFO,
                    )
                )
            elif isinstance(run.logical_id, Known) and _declared_id(run) is None:
                view.findings.append(
                    _finding(
                        "reserved_namespace",
                        f"the run declares an id in the {parse.RECORD_NAMESPACE!r} namespace, "
                        "which names runs by record; it is keyed by its own record",
                        (rid,),
                    )
                )
        assemblies = _assemblies(view)
        _place(build, assemblies)
        for rid in sorted(view.runs):
            run = view.runs[rid]
            build.emit(
                run_node(run),
                EVIDENCED_BY,
                LedgerRecordRef(rid),
                run.provenance.assertion_kind,
                (),
                (),
                build.placements[rid],
            )
        _members(build, assemblies)
        _roles(build, nodes, assemblies)
        for node in sorted(nodes, key=lambda n: n.node_id):
            _continuation(build, node, nodes[node], assemblies)
        return ConsolidatorOutput(tuple(build.drafts), tuple(view.findings))


def _assemblies(view: _View) -> dict[RecordId, list[tuple[RunAssembly, list[_Link]]]]:
    """Each run's assemblies with their recording parts, by the run's record id; an assembly
    naming a run the Ledger does not hold is a finding."""
    out: dict[RecordId, list[tuple[RunAssembly, list[_Link]]]] = {}
    for package_id, assembly in view.assemblies:
        if assembly.run not in view.runs:
            view.findings.append(
                _finding(
                    "dangling_assembly",
                    "a run assembly names a run record the Ledger does not hold",
                    (assembly.id,),
                    package_id=package_id,
                    run=assembly.run,
                )
            )
            continue
        out.setdefault(assembly.run, []).append((assembly, _links(view, package_id, assembly)))
    return out


def _place(
    build: _Build, assemblies: Mapping[RecordId, Sequence[tuple[RunAssembly, list[_Link]]]]
) -> None:
    """Every run's placements: its own interval, else the span of its assembly's parts, then
    its civil projections. A run with neither is ``untimed`` and gets no claims."""
    view = build.view
    primary = {rid: _primary(view, run) for rid, run in sorted(view.runs.items())}
    for rid in sorted(view.runs):
        base = primary[rid]
        if base is None and not isinstance(view.runs[rid].first, Known):
            parts = sorted(
                {link.part.id for _, links in assemblies.get(rid, ()) for link in links} - {rid}
            )
            placed = [primary[part] for part in parts]
            # An untimed part may lie anywhere: with one, the span's bounds are not stated.
            timed = [p for p in placed if p is not None]
            base = _hull(timed) if len(timed) == len(placed) else None
            if base is None:
                untimed = [part for part, p in zip(parts, placed, strict=True) if p is None]
                view.findings.append(
                    _finding(
                        "untimed_run",
                        "the run states no first instant and its parts span no stated interval "
                        "on one clock; no claim is placed on it",
                        (rid, *untimed),
                    )
                )
        if base is None:
            build.placements[rid] = []
            continue
        placements = [base, *_projections(view, base)]
        build.placements[rid] = placements
        civil = [p for p in placements if view.is_civil(p.start.domain_id)]
        build.order[rid] = civil[0] if civil else base


def _membership(view: _View, assembly: RunAssembly, over: Sequence[Placement]) -> list[Placement]:
    """Where an assembly's membership holds: the run's placements, cut to the assembly's stated
    window where it states one. A window bounds only placements on its own clock; with none
    there, the window itself when its start is stated. An ambiguous window is cut to the part
    every reading agrees on (ADR 0009 §3)."""
    window = _definite(assembly.validity)
    if window is None:
        return list(over)
    if window.ambiguous:
        view.findings.append(_ambiguous_window((assembly.id,), "run assembly"))
    start = None if window.start is None else view.place(window.start)
    end = None if window.end is None else view.place(window.end)
    if start is not None and end is not None and not start < end:
        return []  # readings that share no instant: no membership is certain anywhere
    clock = view.clocks[window.clock].domain_id if window.clock in view.clocks else window.clock
    out: list[Placement] = []
    for place in over:
        if place.start.domain_id != clock:
            continue
        lo = place.start if start is None or start < place.start else start
        hi: Timestamp | Open = place.end
        if end is not None and (isinstance(hi, Open) or end < hi):
            hi = end
        if isinstance(hi, Open) or lo < hi:
            out.append(Placement(lo, hi, place.evidence, place.records))
    if not out and start is not None:
        out.append(Placement(start, OPEN if end is None else end, (), ()))
    if not out:
        view.findings.append(
            _finding(
                "membership_unplaced",
                "the assembly's window is on a clock no placement of its run is on",
                (assembly.id,),
                Severity.INFO,
            )
        )
    return out


def _members(
    build: _Build, assemblies: Mapping[RecordId, Sequence[tuple[RunAssembly, list[_Link]]]]
) -> None:
    for rid in sorted(assemblies):
        node = run_node(build.view.runs[rid])
        for assembly, _ in assemblies[rid]:
            over = _membership(build.view, assembly, build.placements[rid])
            kind = assembly.provenance.assertion_kind
            cited = (assembly.provenance.evidence,)
            grouping = (assembly.id, assembly.provenance.transform)
            build.emit(
                node, EVIDENCED_BY, LedgerRecordRef(assembly.id), kind, cited, grouping, over
            )
            for member in assembly.members:
                build.emit(
                    node,
                    HAS_MEMBER,
                    LedgerRecordRef(member.revision),
                    kind,
                    (member.evidence, *cited),
                    grouping,
                    over,
                )


def _roles(
    build: _Build,
    nodes: Mapping[NodeRef, Sequence[Run]],
    assemblies: Mapping[RecordId, Sequence[tuple[RunAssembly, list[_Link]]]],
) -> None:
    """``recorded_by``, ``at_site`` and ``executes_task`` per run node, from what its records
    and manifest declarations state; a run with no machine of its own takes its parts'."""
    view = build.view
    declared: dict[NodeRef, list[RunDeclaration]] = {}
    for declaration in view.declarations:
        named = view.runs.get(declaration.run)
        if named is None:  # never guessed from its logical id: a finding, not a run
            view.findings.append(
                _finding(
                    "dangling_declaration",
                    "a run declaration names a run record that is not an admitted run",
                    (declaration.id,),
                    run=declaration.run,
                )
            )
            continue
        declared.setdefault(run_node(named), []).append(declaration)
    for node in sorted(nodes, key=lambda n: n.node_id):
        runs = nodes[node]
        over = [p for run in runs for p in build.placements[run.id]]
        if not over:
            continue
        for role, predicate, node_type in _ROLES:
            grounds = [
                g
                for g in (
                    *(
                        _ground(
                            run.machine,
                            (run.id,),
                            (run.provenance.evidence,),
                            run.provenance.assertion_kind,
                        )
                        for run in runs
                        if role == "machine"
                    ),
                    *(
                        _ground(getattr(d, role), (d.id,), (d.provenance.evidence,), STATED)
                        for d in declared.get(node, ())
                    ),
                )
                if g is not None
            ]
            from_parts = False
            unstated: list[RecordId] = []
            if not grounds and role == "machine":
                grounds, unstated = _part_machines(node, runs, assemblies)
                from_parts = bool(grounds)
            if not grounds:
                continue  # Unknown: nothing stated, nothing claimed
            many = CORE_PREDICATES.spec(predicate).cardinality is Cardinality.MANY
            known, candidates, distinct = _decide(grounds, many)
            if unstated:
                # A part that states no machine may be another robot's: what the other parts
                # state is never the run's machine for certain, only a reading of it.
                known, candidates = [], [*known, *candidates]
                view.findings.append(
                    _finding(
                        "part_machine_unstated",
                        "a recording part of the run states no machine; the machines its other "
                        "parts state are candidates, none is the run's for certain",
                        (*(r for g in grounds for r in g.records), *unstated),
                        Severity.INFO,
                        run=node.node_id,
                    )
                )
            if distinct > 1 and not many:
                view.findings.append(
                    _finding(
                        "parts_differ" if from_parts else "declarations_disagree",
                        f"the run's {role} is stated as {distinct} different ids; "
                        "each is a candidate, none is chosen",
                        (r for g in grounds for r in g.records),
                        Severity.INFO if from_parts else Severity.WARNING,
                        run=node.node_id,
                    )
                )
            emitted = [(predicate, pair) for pair in known]
            emitted += [(CANDIDATE_OF[predicate], pair) for pair in candidates]
            for out, (ground, reading) in emitted:
                obj = node_ref(node_type, reading.node)
                if role == "site" and view.site_register and _key(reading.node) not in view.sites:
                    view.findings.append(
                        _finding(
                            "site_unregistered",
                            "a run names a site the site register does not declare",
                            ground.records,
                            run=node.node_id,
                            site=obj.node_id,
                        )
                    )
                build.emit(
                    node,
                    out,
                    obj,
                    reading.kind,
                    (*ground.evidence, *reading.evidence),
                    ground.records,
                    over,
                )


def _part_machines(
    node: NodeRef,
    runs: Sequence[Run],
    assemblies: Mapping[RecordId, Sequence[tuple[RunAssembly, list[_Link]]]],
) -> tuple[list[_Ground], list[RecordId]]:
    """An assembled run that states no machine: the machines its recording parts declare, each
    citing the part and the assembly that places it, and the parts that state none."""
    grounds: list[_Ground] = []
    unstated: list[RecordId] = []
    for run in runs:
        for _, links in assemblies.get(run.id, ()):
            for link in links:
                if run_node(link.part) == node:
                    continue
                ground = _ground(
                    link.part.machine,
                    (link.part.id, *link.records),
                    (link.part.provenance.evidence, *link.evidence),
                    link.part.provenance.assertion_kind,
                )
                if ground is not None:
                    grounds.append(ground)
                else:
                    unstated.append(link.part.id)
    return grounds, sorted(set(unstated))


def _machine(run: Run) -> bytes | None:
    return _key(run.machine.value) if isinstance(run.machine, Known) else None


def _continuation(
    build: _Build,
    node: NodeRef,
    runs: Sequence[Run],
    assemblies: Mapping[RecordId, Sequence[tuple[RunAssembly, list[_Link]]]],
) -> None:
    """``continues`` between consecutive recording parts of one assembled run on one clock;
    ``continues_candidate`` both ways between parts whose times cannot be compared. Parts that
    overlap are concurrent, and parts that declare different machines are different robots:
    neither continues the other. A part with an open end may have ended before a later part
    starts, and a part that states no machine may be another robot's: either leaves the link a
    candidate, never ``continues``."""
    links: dict[RecordId, list[_Link]] = {}
    for run in runs:
        for _, found in assemblies.get(run.id, ()):
            for link in found:
                if run_node(link.part) != node:
                    links.setdefault(link.part.id, []).append(link)
    parts = sorted(links)
    if len({run_node(build.view.runs[p]) for p in parts}) < 2:
        return

    def compatible(a: RecordId, b: RecordId) -> bool:
        ra, rb = build.view.runs[a], build.view.runs[b]
        if run_node(ra) == run_node(rb):
            return False
        ma, mb = _machine(ra), _machine(rb)
        return ma is None or mb is None or ma == mb

    def sure(a: RecordId, b: RecordId) -> bool:
        """Both state one machine, or neither does (one recorder's parts, unnamed)."""
        return (_machine(build.view.runs[a]) is None) == (_machine(build.view.runs[b]) is None)

    def cite(*rids: RecordId) -> tuple[list[EvidenceRef], list[RecordId]]:
        evidence = [ref for r in rids for link in links[r] for ref in link.evidence]
        records = [rec for r in rids for link in links[r] for rec in link.records]
        evidence += [build.view.runs[r].provenance.evidence for r in rids]
        return evidence, [*records, *rids]

    def emit(predicate: str, later: RecordId, earlier: RecordId) -> None:
        evidence, records = cite(earlier, later)
        build.emit(
            run_node(build.view.runs[later]),
            predicate,
            run_node(build.view.runs[earlier]),
            OBSERVED,
            evidence,
            records,
            build.placements[later],
        )

    view = build.view
    primary = {p: build.placements[p][0] for p in parts if build.placements[p]}
    civil = {
        p: next((x for x in build.placements[p] if view.is_civil(x.start.domain_id)), None)
        for p in parts
    }

    def relation(a: RecordId, b: RecordId) -> str:
        """``before``, ``after``, ``maybe_before``, ``maybe_after``, ``concurrent`` or
        ``unknown``. Parts on one clock compare there, and overlap means concurrent, except that
        a part whose end is open and that starts first may have ended before the other starts.
        Otherwise on a civil clock both reach, where a projection is widened to whole ticks and
        its residual bound, so overlap there only means unordered."""
        pa, pb = primary.get(a), primary.get(b)
        if pa is not None and pb is not None and pa.start.domain_id == pb.start.domain_id:
            order = _order(pa, pb)
            if order is not None:
                return order
            if isinstance(pa.end, Open) and pa.start <= pb.start:
                return "maybe_before"
            if isinstance(pb.end, Open) and pb.start <= pa.start:
                return "maybe_after"
            return "concurrent"
        ca, cb = civil[a], civil[b]
        if ca is not None and cb is not None and ca.start.domain_id == cb.start.domain_id:
            return _order(ca, cb) or "unknown"
        return "unknown"

    before: dict[RecordId, set[RecordId]] = {p: set() for p in parts}
    maybe: dict[RecordId, set[RecordId]] = {p: set() for p in parts}  # possibly before
    for i, a in enumerate(parts):
        for b in parts[i + 1 :]:
            if not compatible(a, b):
                continue
            rel = relation(a, b)
            if rel == "before":
                before[b].add(a)
            elif rel == "after":
                before[a].add(b)
            elif rel == "maybe_before":
                maybe[b].add(a)
            elif rel == "maybe_after":
                maybe[a].add(b)
            elif rel == "unknown":
                emit(CANDIDATE_OF[CONTINUES], a, b)
                emit(CANDIDATE_OF[CONTINUES], b, a)
    # A part's nearest predecessors are the compatible earlier parts, certain or possible, that
    # no other one certainly follows; parts of other machines and concurrent ones are skipped.
    # ``continues`` only when there is one, it certainly ends before, and both parts state their
    # machine alike. Otherwise (concurrent logs that all end before it, an open end, a part with
    # no machine) which it continues is ambiguous: a candidate to each.
    for later in parts:
        earlier = before[later] | maybe[later]
        nearest = [p for p in sorted(earlier) if not any(p in before[o] for o in earlier)]
        certain = len(nearest) == 1 and nearest[0] in before[later] and sure(nearest[0], later)
        predicate = CONTINUES if certain else CANDIDATE_OF[CONTINUES]
        for part in nearest:
            emit(predicate, later, part)


def _order(a: Placement, b: Placement) -> str | None:
    """``before`` when ``a`` ends no later than ``b`` starts, ``after`` the other way round."""
    if isinstance(a.end, Timestamp) and a.end <= b.start:
        return "before"
    if isinstance(b.end, Timestamp) and b.end <= a.start:
        return "after"
    return None


# --- Reading run involvement back ---------------------------------------------------------------


def involvement(
    claims: Iterable[Claim], run: NodeRef, predicate: str
) -> Knowledge[tuple[NodeRef, ...]]:
    """What ``claims`` say a run's ``predicate`` is: ``recorded_by``, ``at_site``,
    ``executes_task`` or ``continues``, as the objects that hold. Pass the claims current at one
    ``as_of``.

    - ``Known(objects)`` when no candidate: one object for a ``one`` predicate, every object for
      a ``many`` one (``executes_task``, ``continues``).
    - ``Ambiguous`` when there are candidates, or a ``one`` predicate has several objects. Each
      reading is a tuple: one object for a ``one`` predicate; for a ``many`` predicate, the known
      objects plus one candidate, and also the known objects alone for ``continues`` (a lone
      ``continues_candidate`` may mean it continues nothing) or when there is one candidate.
    - ``Unknown`` when the run is in the claims but nothing names one, or a lone candidate of a
      ``one`` predicate says only that it might be.
    - ``NotCovered`` when no claim names the run.
    """
    if predicate not in CANDIDATE_OF:
        raise ValueError(f"{predicate!r} has no candidate form: one of {sorted(CANDIDATE_OF)}")
    many = CORE_PREDICATES.spec(predicate).cardinality is Cardinality.MANY
    candidate = CANDIDATE_OF[predicate]
    named = False
    known: set[NodeRef] = set()
    readings: set[NodeRef] = set()
    for claim in claims:
        if claim.subject == run or claim.object == run:
            named = True
        if claim.subject != run or not isinstance(claim.object, NodeRef):
            continue
        if claim.predicate == predicate:
            known.add(claim.object)
        elif claim.predicate == candidate:
            readings.add(claim.object)
    if not named:
        return NotCovered()

    def ordered(nodes: Iterable[NodeRef]) -> tuple[NodeRef, ...]:
        return tuple(sorted(nodes, key=lambda n: (n.node_type, n.node_id)))

    options: list[tuple[NodeRef, ...]]
    if many:
        options = [ordered({*known, c}) for c in ordered(readings - known)]
        if options and (predicate == CONTINUES or len(options) == 1):
            options.insert(0, ordered(known))
    else:
        options = [(n,) for n in ordered(known | readings)]
    if len(options) > 1:
        return Ambiguous(tuple(Candidate(option) for option in options))
    if known and (many or len(known) == 1) and not (readings - known):
        return Known(ordered(known))
    return Unknown()
