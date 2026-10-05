"""Coverage and health as a deterministic consolidator (ADR 0015).

What each run recorded, where its streams have gaps, how fast they declared and actually sampled,
what the compiler found wrong with the evidence, and whether each configured sensor recorded:

- ``recorded(stream → run)``: per clock, over the series' first to last known sample, from the
  Ledger's series coverage.
- ``gap(stream → run)``: where the stream's declared first or last instant (its source's index)
  predicts samples and the series holds none: before its first known sample, after its last.
  Never when a sample has no known tick on that clock: it may lie in the gap.
- ``rate_declared`` / ``rate_observed`` (stream, Hz): ``(n - 1)`` samples over the first-to-last
  span, from the declared count and extent and from the series; only on a clock that states its
  resolution. Both are stated side by side; nothing judges whether they agree.
- ``integrity_finding(run | stream → severity)``: each compiler finding that names the run or the
  stream, and each corrupt, limit or failed finding about the run's files' bytes, with the
  severity the compiler stated.
- ``sensor_recorded`` / ``sensor_not_recorded`` / ``sensor_presence_unknown`` (run → sensor): for
  each sensor component of each hardware configuration bound to the run.

Records are parsed by ``consolidate.coverage_records``; this module decides. Malformed or
contradictory input is a finding and never a claim, and the rest of the build is unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING, Final, TypeVar

from neptune.model.alignment import MemberRole, RunAssembly, SnapshotBinding, SnapshotKind
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import LogicalId
from neptune.model.knowledge import Ambiguous, AssertionKind, Known
from neptune.model.machine import ComponentCategory, HardwareComponent, HardwareConfiguration
from neptune.model.provenance import EvidenceRef, Provenance
from neptune.model.run import Run, Stream
from neptune.model.source import SourceRevision
from neptune.model.time import INT64_MAX, Timestamp
from neptune.model.units import unit_from_json
from neptune.model.world import Image, Video
from neptune_memory.consolidate import coverage_records as parse
from neptune_memory.consolidate import run_records
from neptune_memory.consolidate.base import ClaimDraft, ConsolidationFinding, ConsolidatorOutput
from neptune_memory.consolidate.identity import node_ref
from neptune_memory.consolidate.runs import run_node
from neptune_memory.schema.claim import TypedLiteral, ValueType
from neptune_memory.schema.interval import OPEN, Open
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import (
    GAP,
    INTEGRITY_FINDING,
    RATE_DECLARED,
    RATE_OBSERVED,
    RECORDED,
    SENSOR_NOT_RECORDED,
    SENSOR_PRESENCE_UNKNOWN,
    SENSOR_RECORDED,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from neptune.model.ids import ContentId, RecordId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune_memory.consolidate.base import ModelRef
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim

_T = TypeVar("_T")

COVERAGE_CONSOLIDATOR_ID: Final = "memory.coverage"
OBSERVED: Final = AssertionKind.OBSERVED
HERTZ: Final = unit_from_json("Hz")
RECORD_NAMESPACE: Final = run_records.RECORD_NAMESPACE

# Finding categories that say a file's bytes did not all reach the canonical output (root ADR 0017
# §9): about a run's file, they qualify the run even when they name no record of it.
BYTES_LOST: Final = frozenset(
    {FindingCategory.CORRUPT, FindingCategory.LIMIT, FindingCategory.FAILED}
)

Media = Image | Video


def stream_node(stream: Stream) -> NodeRef:
    """A stream's node: ``record:<stream record id>``, as the Ledger anchors a stream's thread on
    its own record (Ledger ADR 0003 §2)."""
    return node_ref(NodeType.STREAM, LogicalId(RECORD_NAMESPACE, stream.id))


def sensor_nodes(component: HardwareComponent) -> tuple[NodeRef, ...]:
    """A sensor's nodes: one per identifier its component declares (``Known``), as the Ledger keys
    a sensor thread per declared identifier; ``record:<component id>`` when it declares none."""
    known = sorted(
        (i.value for i in component.identifiers if isinstance(i, Known)),
        key=lambda i: (i.namespace, i.value),
    )
    if known:
        return tuple(node_ref(NodeType.SENSOR, i) for i in known)
    return (node_ref(NodeType.SENSOR, LogicalId(RECORD_NAMESPACE, component.id)),)


def _finding(
    code: str,
    message: str,
    records: Iterable[RecordId] = (),
    severity: Severity = Severity.WARNING,
    **details: JsonValue,
) -> ConsolidationFinding:
    return ConsolidationFinding(
        code=f"coverage.{code}",
        severity=severity,
        message=message,
        records=tuple(records),
        details=details,
    )


def _cited(value: Knowledge[_T]) -> tuple[EvidenceRef, ...]:
    """The evidence a ``Known`` value's own provenance cites; nothing when it inherits its
    record's."""
    if isinstance(value, Known) and isinstance(value.provenance, Provenance):
        return (value.provenance.evidence,)
    return ()


def _known(value: Knowledge[Timestamp]) -> Timestamp | None:
    return value.value if isinstance(value, Known) else None


# --- Reading the Ledger -------------------------------------------------------------------------


@dataclass
class _View:
    runs: dict[RecordId, Run] = field(default_factory=dict)
    streams: dict[RecordId, Stream] = field(default_factory=dict)
    ingest: dict[RecordId, IngestFinding] = field(default_factory=dict)
    domains: dict[RecordId, parse.Domain] = field(default_factory=dict)
    bindings: list[SnapshotBinding] = field(default_factory=list)  # by id
    configurations: set[RecordId] = field(default_factory=set)
    components: list[HardwareComponent] = field(default_factory=list)  # by id
    media: list[Media] = field(default_factory=list)  # by id
    assemblies: list[tuple[str, RunAssembly]] = field(default_factory=list)  # by (id, package)
    revisions: dict[tuple[str, RecordId], ContentId] = field(default_factory=dict)
    series: dict[tuple[RecordId, RecordId], parse.SeriesInterval] = field(default_factory=dict)
    findings: list[ConsolidationFinding] = field(default_factory=list)
    # A run's file, stream or finding the Ledger holds but this build could not read: a run may
    # have more data than the records show, so no sensor of any run is known absent.
    unreadable: set[str] = field(default_factory=set)

    def place(self, stamp: Timestamp) -> Timestamp:
        """A stamp on a clock that declares itself civil, on that ``CivilClock``; any other stamp
        as declared (ADR 0002 §3, as runs and identity place instants)."""
        found = self.domains.get(stamp.domain_id)
        return stamp if found is None or found.civil is None else found.civil.at(stamp.ticks)

    def same_clock(self, a: RecordId, b: RecordId) -> bool:
        """Whether ticks on clocks ``a`` and ``b`` are on one timeline: one clock, or both civil
        on one ``CivilClock`` (as ``place`` puts them)."""
        return self.place(Timestamp(0, a)).domain_id == self.place(Timestamp(0, b)).domain_id

    def resolution(self, clock: RecordId) -> Fraction | None:
        found = self.domains.get(clock)
        return None if found is None else found.resolution


# The kinds this consolidator reads, in the order it reads them from each package.
_PARSERS: Final[Mapping[str, Callable[[Mapping[str, object]], object]]] = {
    parse.TIMESTAMP_DOMAIN: parse.domain,
    run_records.SOURCE_REVISION: run_records.revision,
    run_records.RUN: run_records.run,
    run_records.RUN_ASSEMBLY: run_records.assembly,
    parse.STREAM: parse.stream,
    parse.SERIES_INTERVAL: parse.series,
    parse.INGEST_FINDING: parse.finding,
    parse.SNAPSHOT_BINDING: parse.binding,
    parse.HARDWARE_CONFIGURATION: parse.configuration,
    parse.HARDWARE_COMPONENT: parse.component,
    parse.IMAGE: parse.media,
    parse.VIDEO: parse.media,
}


# Kinds that say what a run holds or what went wrong with it: one the build cannot read leaves
# every run's content incomplete as far as sensor presence is concerned.
_RUN_CONTENT: Final = frozenset(
    {
        run_records.RUN,
        run_records.RUN_ASSEMBLY,
        run_records.SOURCE_REVISION,
        parse.STREAM,
        parse.INGEST_FINDING,
        parse.IMAGE,
        parse.VIDEO,
    }
)


def _admission_key(package_id: str, parsed: object) -> tuple[str, ...]:
    """What one record is de-duplicated by: its id across packages; an assembly or a revision
    within its package (two uploads of one bag share an assembly id, ADR 0009 §1); a series
    interval by its stream and clock."""
    kind = type(parsed).__name__
    if isinstance(parsed, parse.SeriesInterval):
        return (kind, parsed.stream, parsed.clock)
    if isinstance(parsed, parse.Domain):
        return (kind, parsed.record)
    assert isinstance(
        parsed,
        Run
        | Stream
        | RunAssembly
        | SourceRevision
        | IngestFinding
        | SnapshotBinding
        | HardwareConfiguration
        | HardwareComponent
        | Image
        | Video,
    )
    if isinstance(parsed, RunAssembly | SourceRevision):
        return (kind, package_id, parsed.id)
    return (kind, parsed.id)


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


def _read(ledger: LedgerReader) -> _View:
    view = _View()
    seen: dict[tuple[str, ...], object] = {}
    conflicted: set[tuple[str, ...]] = set()
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
                            "a derived/ record, never a ground for a coverage claim",
                            severity=Severity.INFO,
                            index=index,
                            kind=kind,
                            package_id=ref.package_id,
                        )
                    )
                    # What it says about a run is not read, so the run's content is incomplete.
                    if kind in _RUN_CONTENT:
                        view.unreadable.add(kind)
                    continue
                except parse.Malformed as exc:
                    view.findings.append(_malformed(kind, ref.package_id, index, str(exc)))
                    if kind in _RUN_CONTENT:
                        view.unreadable.add(kind)
                    continue
                key = _admission_key(ref.package_id, parsed)
                if key in conflicted:
                    continue
                if seen.setdefault(key, parsed) != parsed:
                    conflicted.add(key)
                    if kind in _RUN_CONTENT:
                        view.unreadable.add(kind)
                    view.findings.append(
                        _finding(
                            "record_conflict",
                            f"one {kind} key carries different content in two places; "
                            "record not used",
                            (rid,) if (rid := getattr(parsed, "id", None)) else (),
                            Severity.ERROR,
                            key=list(key),
                        )
                    )
    for key, parsed in sorted(seen.items()):
        if key in conflicted:
            continue
        if isinstance(parsed, Run):
            view.runs[parsed.id] = parsed
        elif isinstance(parsed, Stream):
            view.streams[parsed.id] = parsed
        elif isinstance(parsed, parse.SeriesInterval):
            view.series[(parsed.stream, parsed.clock)] = parsed
        elif isinstance(parsed, IngestFinding):
            view.ingest[parsed.id] = parsed
        elif isinstance(parsed, parse.Domain):
            view.domains[parsed.record] = parsed
        elif isinstance(parsed, SnapshotBinding):
            view.bindings.append(parsed)
        elif isinstance(parsed, HardwareConfiguration):
            view.configurations.add(parsed.id)
        elif isinstance(parsed, HardwareComponent):
            view.components.append(parsed)
        elif isinstance(parsed, Image | Video):
            view.media.append(parsed)
        elif isinstance(parsed, RunAssembly):
            view.assemblies.append((key[1], parsed))
        elif isinstance(parsed, SourceRevision):
            view.revisions[(key[1], parsed.id)] = parsed.content_id
    view.bindings.sort(key=lambda b: b.id)
    view.components.sort(key=lambda c: c.id)
    view.media.sort(key=lambda m: m.id)
    view.assemblies.sort(key=lambda item: (item[1].id, item[0]))
    return view


# --- Where things are in time ------------------------------------------------------------------


@dataclass(frozen=True)
class _Span:
    """``[start, end)`` on one clock, what places it there, and whether both ends are stated."""

    start: Timestamp
    end: Timestamp | Open
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]
    closed: bool


def _after(stamp: Timestamp) -> Timestamp | None:
    """The tick after an inclusive last instant; ``None`` at the clock's last tick."""
    return Timestamp(stamp.ticks + 1, stamp.domain_id) if stamp.ticks < INT64_MAX else None


def _run_span(view: _View, run: Run) -> _Span | None:
    """The run's ``[first, last + 1 tick)`` on ``first``'s clock, as runs places it (ADR 0009 §2):
    open (and not closed) when ``last`` is not stated, is on another clock or is the clock's last
    tick; ``None`` when ``first`` is not stated or ``last`` precedes it."""
    first = _known(run.first)
    if first is None:
        return None
    start = view.place(first)
    evidence = [run.provenance.evidence, *_cited(run.first)]
    last = _known(run.last)
    after = _after(last) if last is not None else None
    if last is None or after is None or not view.same_clock(last.domain_id, first.domain_id):
        return _Span(start, OPEN, tuple(evidence), (run.id,), closed=False)
    if last.ticks < first.ticks:
        return None
    evidence.extend(_cited(run.last))
    return _Span(start, view.place(after), tuple(evidence), (run.id,), closed=True)


# --- Building claims ----------------------------------------------------------------------------


@dataclass
class _Build:
    view: _View
    drafts: list[ClaimDraft] = field(default_factory=list)
    spans: dict[RecordId, _Span | None] = field(default_factory=dict)
    unplaced: set[RecordId] = field(default_factory=set)

    def emit(
        self,
        subject: NodeRef,
        predicate: str,
        obj: NodeRef | TypedLiteral,
        evidence: Iterable[EvidenceRef],
        records: Iterable[RecordId],
        start: Timestamp,
        end: Timestamp | Open,
        kind: AssertionKind = OBSERVED,
    ) -> None:
        self.drafts.append(
            ClaimDraft(
                subject=subject,
                predicate=predicate,
                object=obj,
                valid_from=start,
                valid_to=end,
                assertion_kind=kind,
                evidence=tuple(evidence),
                records=tuple(records),
            )
        )

    def span(self, run: Run) -> _Span | None:
        """The run's span, once; an unplaceable run is one ``unplaced_run`` finding."""
        if run.id not in self.spans:
            span = _run_span(self.view, run)
            self.spans[run.id] = span
            if span is None:
                inverted = isinstance(run.first, Known) and isinstance(run.last, Known)
                self.view.findings.append(
                    _finding(
                        "unplaced_run",
                        "the run's last instant precedes its first; nothing is placed on it"
                        if inverted
                        else "the run states no first instant; nothing about it is placed",
                        (run.id,),
                        Severity.WARNING if inverted else Severity.INFO,
                    )
                )
        return self.spans[run.id]


def _hertz(samples: int, ticks: int, resolution: Fraction | None) -> TypedLiteral | str:
    """``(samples - 1)`` intervals over ``ticks`` ticks of ``resolution`` seconds, in Hz; or why
    the rate is not determined."""
    if resolution is None:
        return "clock_resolution_unstated"
    if samples < 2:
        return "fewer_than_two_samples"
    if ticks <= 0:
        return "zero_span"
    rate = Fraction(samples - 1) / (ticks * resolution)
    return TypedLiteral(ValueType.QUANTITY, float(rate), Known(HERTZ))


@dataclass(frozen=True)
class _Declared:
    """What a stream's source declares about its extent on one clock: ``[first, last]``."""

    first: Timestamp
    last: Timestamp
    evidence: tuple[EvidenceRef, ...]


def _declared(view: _View, stream: Stream) -> _Declared | None:
    first, last = _known(stream.first), _known(stream.last)
    if first is None or last is None:
        return None
    if not view.same_clock(first.domain_id, last.domain_id) or last.ticks < first.ticks:
        view.findings.append(
            _finding(
                "declared_extent_unusable",
                "the stream's declared first and last instants are on two clocks or inverted; "
                "its declared extent predicts nothing",
                (stream.id,),
                Severity.INFO,
            )
        )
        return None
    evidence = (*_cited(stream.first), *_cited(stream.last))
    return _Declared(first, last, evidence)


def _undetermined(stream: Stream, what: str, reason: str) -> ConsolidationFinding:
    return _finding(
        "rate_undetermined",
        f"the stream's {what} rate is not determined: {reason.replace('_', ' ')}",
        (stream.id,),
        Severity.INFO,
        rate=what,
        reason=reason,
    )


def _streams(build: _Build) -> None:
    view = build.view
    for (sid, clock), row in sorted(view.series.items()):
        found = view.streams.get(sid)
        if found is None or clock not in found.clocks:
            view.findings.append(
                _finding(
                    "dangling_series",
                    "a series coverage row names a stream the Ledger does not hold, or a clock "
                    "the stream does not carry; it places nothing",
                    (row.stream,),
                    clock=clock,
                )
            )
    for sid in sorted(view.streams):
        stream = view.streams[sid]
        run = view.runs.get(stream.run)
        if run is None:
            view.findings.append(
                _finding(
                    "dangling_stream",
                    "the stream names a run the Ledger does not hold; nothing about it is placed",
                    (sid,),
                )
            )
            continue
        subject, run_ref = stream_node(stream), run_node(run)
        base = (stream.provenance.evidence,)
        records = (sid, run.id)
        declared = _declared(view, stream)
        if declared is not None:
            _declared_rate(build, stream, declared, subject, records)
        for clock in stream.clocks:
            covered = view.series.get((sid, clock))
            if covered is None:
                if declared is not None and view.same_clock(declared.first.domain_id, clock):
                    view.findings.append(
                        _finding(
                            "series_not_indexed",
                            "the stream declares an extent on a clock the Ledger's series "
                            "coverage holds no known sample on; what it recorded there is unknown",
                            (sid,),
                            Severity.INFO,
                        )
                    )
                continue
            first = Timestamp(covered.first, clock)
            after = _after(Timestamp(covered.last, clock))
            if after is None:
                view.findings.append(
                    _finding(
                        "end_unrepresentable",
                        "the series' last known sample is the clock's last tick; its span is "
                        "not placed",
                        (sid,),
                        Severity.INFO,
                    )
                )
                continue
            start, end = view.place(first), view.place(after)
            build.emit(subject, RECORDED, run_ref, base, records, start, end)
            if covered.rows_unknown:
                view.findings.append(
                    _finding(
                        "untimed_samples",
                        f"{covered.rows_unknown} of the stream's samples have no known tick on "
                        "this clock and may lie anywhere: no gap and no observed rate on it",
                        (sid,),
                        Severity.INFO,
                        clock=clock,
                        rows_unknown=covered.rows_unknown,
                    )
                )
                continue
            rate = _hertz(covered.rows_known, covered.last - covered.first, view.resolution(clock))
            if isinstance(rate, TypedLiteral):
                build.emit(subject, RATE_OBSERVED, rate, base, records, start, end)
            else:
                view.findings.append(_undetermined(stream, "observed", rate))
            if declared is not None and view.same_clock(declared.first.domain_id, clock):
                _gaps(build, stream, declared, covered, subject, run_ref, records)


def _declared_rate(
    build: _Build,
    stream: Stream,
    declared: _Declared,
    subject: NodeRef,
    records: tuple[RecordId, ...],
) -> None:
    view = build.view
    count = stream.message_count
    if not isinstance(count, Known):
        return  # no declared count: no declared rate, and nothing to report beyond the record
    after = _after(declared.last)
    if after is None:
        view.findings.append(_undetermined(stream, "declared", "end_unrepresentable"))
        return
    clock = declared.first.domain_id
    rate = _hertz(count.value, declared.last.ticks - declared.first.ticks, view.resolution(clock))
    if not isinstance(rate, TypedLiteral):
        view.findings.append(_undetermined(stream, "declared", rate))
        return
    kind = (
        count.provenance.assertion_kind
        if isinstance(count.provenance, Provenance)
        else stream.provenance.assertion_kind
    )
    evidence = (stream.provenance.evidence, *_cited(count), *declared.evidence)
    start, end = view.place(declared.first), view.place(after)
    build.emit(subject, RATE_DECLARED, rate, evidence, records, start, end, kind)


def _gaps(
    build: _Build,
    stream: Stream,
    declared: _Declared,
    covered: parse.SeriesInterval,
    subject: NodeRef,
    run_ref: NodeRef,
    records: tuple[RecordId, ...],
) -> None:
    """Before the first known sample and after the last, where the declared extent reaches."""
    view, clock = build.view, covered.clock
    evidence = (stream.provenance.evidence, *declared.evidence)
    lo, hi = declared.first.ticks, declared.last.ticks
    if covered.first < lo or covered.last > hi:
        view.findings.append(
            _finding(
                "extent_disagrees",
                "the series holds samples outside the extent the stream declares; no gap is "
                "claimed on that side",
                (stream.id,),
                Severity.INFO,
                clock=clock,
            )
        )
    # Each gap is clamped to the declared extent: outside it the source predicts nothing.
    after = _after(declared.last)
    if after is None:
        view.findings.append(
            _finding(
                "end_unrepresentable",
                "the stream's declared last instant is the clock's last tick; no gap is placed",
                (stream.id,),
                Severity.INFO,
            )
        )
        return
    if covered.first > lo:
        end = min(covered.first, after.ticks)
        build.emit(
            subject,
            GAP,
            run_ref,
            evidence,
            records,
            view.place(Timestamp(lo, clock)),
            view.place(Timestamp(end, clock)),
        )
    if covered.last < hi:
        start = max(covered.last + 1, lo)
        build.emit(
            subject,
            GAP,
            run_ref,
            evidence,
            records,
            view.place(Timestamp(start, clock)),
            view.place(Timestamp(after.ticks, clock)),
        )


# --- Integrity findings -------------------------------------------------------------------------


def _members(view: _View) -> dict[RecordId, set[ContentId]]:
    """The content ids of each run's files: its own declaration's bytes and every member of every
    assembly naming it whose revision its package holds."""
    contents: dict[RecordId, set[ContentId]] = {}
    for rid, run in view.runs.items():
        source = run.provenance.evidence.source
        contents[rid] = {source} if isinstance(source, str) else set()
    for package_id, assembly in view.assemblies:
        if assembly.run not in view.runs:
            continue
        for member in assembly.members:
            content = view.revisions.get((package_id, member.revision))
            if content is not None:
                contents[assembly.run].add(content)
    return contents


def _data_files(view: _View) -> dict[RecordId, set[object]]:
    """The files that may hold each run's samples: its assemblies' ``recording`` members and the
    bytes that declare it, unless an assembly naming the run states those bytes are its
    ``description`` (a manifest or a rosbag2 ``metadata.yaml``, which declares the run itself).
    A source with no content id (an external object) is kept: it is a file whose content is not
    read here. An empty set means the Ledger holds no recording of the run."""
    files: dict[RecordId, set[object]] = {rid: set() for rid in view.runs}
    described: dict[RecordId, set[object]] = {}
    for package_id, assembly in view.assemblies:
        if assembly.run not in files:
            continue
        for member in assembly.members:
            content = view.revisions.get((package_id, member.revision))
            if content is None:
                continue  # an unresolved member is ``members_unresolved``
            if member.role is MemberRole.RECORDING:
                files[assembly.run].add(content)
            elif member.role is MemberRole.DESCRIPTION:
                described.setdefault(assembly.run, set()).add(content)
    for rid, run in view.runs.items():
        source = run.provenance.evidence.source
        if source not in described.get(rid, set()):
            files[rid].add(source)
    return files


def _unresolved(view: _View) -> set[RecordId]:
    """Runs with an assembly member whose revision the assembly's package does not hold: what
    that file is, and whose sensor it recorded, is not known."""
    return {
        assembly.run
        for package_id, assembly in view.assemblies
        if any((package_id, m.revision) not in view.revisions for m in assembly.members)
    }


def _integrity(build: _Build, contents: Mapping[RecordId, set[ContentId]]) -> set[RecordId]:
    """Each compiler finding that names a run or a stream, or that says a run's file lost bytes,
    as an ``integrity_finding`` over the run; returns the runs with any."""
    view = build.view
    flagged: set[RecordId] = set()
    holders: dict[ContentId, set[RecordId]] = {}
    for rid, held in contents.items():
        for content in held:
            holders.setdefault(content, set()).add(rid)
    run_ids, stream_ids = set(view.runs), set(view.streams)
    for fid in sorted(view.ingest):
        found = view.ingest[fid]
        severity = TypedLiteral(ValueType.TEXT, str(found.severity))
        cited = (found.subject,) if isinstance(found.subject, EvidenceRef) else ()
        evidence = (*cited, *found.related)
        named = set(found.records)
        runs = named & run_ids
        about = found.subject
        if (
            found.category in BYTES_LOST
            and isinstance(about, EvidenceRef)
            and isinstance(about.source, str)
        ):
            runs |= holders.get(about.source, set())
        targets: list[tuple[NodeRef, Run, tuple[RecordId, ...]]] = [
            (run_node(view.runs[rid]), view.runs[rid], (fid, rid)) for rid in sorted(runs)
        ]
        for sid in sorted(named & stream_ids):
            stream = view.streams[sid]
            parent = view.runs.get(stream.run)
            if parent is not None:  # a stream without its run is ``dangling_stream`` already
                targets.append((stream_node(stream), parent, (fid, sid, parent.id)))
        for subject, run, records in targets:
            flagged.add(run.id)
            span = build.span(run)
            if span is None:
                continue
            build.emit(
                subject,
                INTEGRITY_FINDING,
                severity,
                (*evidence, *span.evidence),
                (*records, *span.records),
                span.start,
                span.end,
            )
    return flagged


# --- Sensor presence ----------------------------------------------------------------------------


def _ids(identifiers: Iterable[Knowledge[LogicalId]]) -> tuple[set[LogicalId], set[LogicalId]]:
    """The identifiers a list states definitely (``Known``) and possibly (``Ambiguous``)."""
    known: set[LogicalId] = set()
    possible: set[LogicalId] = set()
    for identifier in identifiers:
        if isinstance(identifier, Known):
            known.add(identifier.value)
        elif isinstance(identifier, Ambiguous):
            possible.update(c.value for c in identifier.candidates)
    return known, possible


def _configured(view: _View) -> dict[RecordId, list[tuple[SnapshotBinding, HardwareComponent]]]:
    """Each run's sensors: every sensor component of every hardware configuration a binding names
    for it. A binding to a run or configuration the Ledger does not hold is a finding."""
    sensors: dict[RecordId, list[HardwareComponent]] = {}
    for component in view.components:
        if component.category is ComponentCategory.SENSOR:
            sensors.setdefault(component.configuration, []).append(component)
    out: dict[RecordId, list[tuple[SnapshotBinding, HardwareComponent]]] = {}
    for bound in view.bindings:
        if bound.snapshot_kind is not SnapshotKind.HARDWARE_CONFIGURATION:
            continue
        if bound.run not in view.runs or bound.snapshot not in view.configurations:
            view.findings.append(
                _finding(
                    "dangling_binding",
                    "a hardware binding names a run or configuration the Ledger does not hold; "
                    "its sensors are not enumerated",
                    (bound.id,),
                )
            )
            continue
        for component in sensors.get(bound.snapshot, ()):
            out.setdefault(bound.run, []).append((bound, component))
    return out


def _presence(
    build: _Build,
    contents: Mapping[RecordId, set[ContentId]],
    flagged: set[RecordId],
) -> None:
    view = build.view
    unresolved = _unresolved(view)
    data = _data_files(view)
    by_source: dict[object, list[Media]] = {}
    for found in view.media:
        by_source.setdefault(found.provenance.evidence.source, []).append(found)
    streams_of: dict[RecordId, list[RecordId]] = {}
    for sid, stream in sorted(view.streams.items()):
        streams_of.setdefault(stream.run, []).append(sid)
    for rid, configured in sorted(_configured(view).items()):
        run = view.runs[rid]
        span = build.span(run)
        if span is None:
            continue
        artifacts = sorted(
            (m for content in contents[rid] for m in by_source.get(content, ())),
            key=lambda m: m.id,
        )
        # A file that holds the run's samples and is no image or video: whose they are is unknown.
        held = data.get(rid, set())
        opaque = [c for c in held if c not in by_source]
        cites = {m.id: _ids(m.capture.device_identifiers) for m in artifacts}
        own = {c.id: _ids(c.identifiers) for _, c in configured}
        for bound, component in configured:
            mine = own[component.id][0]  # only a Known identifier attributes a file definitely
            recorded = [m for m in artifacts if cites[m.id][0] & mine]
            records = [rid, bound.id, bound.snapshot, component.id]
            evidence = [component.provenance.evidence, bound.provenance.evidence, *span.evidence]
            if recorded:
                predicate = SENSOR_RECORDED
                for m in recorded:
                    records.append(m.id)
                    evidence.append(m.provenance.evidence)
            else:
                reasons = _undecided(
                    component,
                    artifacts,
                    cites,
                    own,
                    has_streams=rid in streams_of,
                    opaque=bool(opaque),
                    held=bool(held),
                    unreadable=bool(view.unreadable),
                    unresolved=rid in unresolved,
                    closed=span.closed,
                    flagged=rid in flagged,
                )
                if reasons:
                    predicate = SENSOR_PRESENCE_UNKNOWN
                    view.findings.append(
                        _finding(
                            "presence_undecided",
                            "whether the sensor recorded in the run is not decided: "
                            + ", ".join(r.replace("_", " ") for r in reasons),
                            (rid, component.id),
                            Severity.INFO,
                            reasons=list(reasons),
                        )
                    )
                    records.extend(streams_of.get(rid, ()))
                else:
                    predicate = SENSOR_NOT_RECORDED
                records.extend(m.id for m in artifacts)
                evidence.extend(m.provenance.evidence for m in artifacts)
            for node in sensor_nodes(component):
                build.emit(
                    run_node(run),
                    predicate,
                    node,
                    evidence,
                    (*records, *span.records),
                    span.start,
                    span.end,
                )


def _undecided(
    component: HardwareComponent,
    artifacts: Sequence[Media],
    cites: Mapping[RecordId, tuple[set[LogicalId], set[LogicalId]]],
    own: Mapping[RecordId, tuple[set[LogicalId], set[LogicalId]]],
    *,
    has_streams: bool,
    opaque: bool,
    held: bool,
    unreadable: bool,
    unresolved: bool,
    closed: bool,
    flagged: bool,
) -> tuple[str, ...]:
    """Why a sensor no file of the run definitely cites is not known absent; empty when it is."""
    # Every identifier this sensor may have: its Ambiguous candidates are possibly its own.
    mine = own[component.id][0] | own[component.id][1]
    others: set[LogicalId] = set().union(
        *(known - mine for cid, (known, _) in own.items() if cid != component.id)
    )

    def elsewhere(media: Media) -> bool:
        """The file definitely cites another configured sensor and cannot be this one's."""
        known, possible = cites[media.id]
        return bool(known & others) and not ((known | possible) & mine)

    reasons: list[str] = []
    if not held:
        reasons.append("no_recording")
    if has_streams:
        reasons.append("streams_declare_no_sensor")
    if opaque or any(not elsewhere(m) for m in artifacts):
        reasons.append("files_not_attributed")
    if unresolved:
        reasons.append("members_unresolved")
    if not closed:
        reasons.append("recording_not_closed")
    if flagged:
        reasons.append("integrity_findings")
    if unreadable:
        reasons.append("ledger_records_unreadable")
    return tuple(reasons)


# --- The consolidator ---------------------------------------------------------------------------


class CoverageConsolidator:
    """Deterministic coverage and health claims (ADR 0015). Takes no configuration."""

    consolidator_id: Final = COVERAGE_CONSOLIDATOR_ID
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
                    "the coverage consolidator takes no configuration; keys ignored",
                    keys=sorted(config),
                )
            )
        build = _Build(view)
        _streams(build)
        contents = _members(view)
        flagged = _integrity(build, contents)
        _presence(build, contents, flagged)
        return ConsolidatorOutput(tuple(build.drafts), tuple(view.findings))
