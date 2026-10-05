"""Episodes from stated task evidence as a deterministic consolidator (ADR 0012).

An episode is a stated attempt at a task within a run. The only statement of a task attempt the
Ledger holds today is a run's declared task, which ``memory.runs`` grounds as ``executes_task``
(or ``executes_task_candidate``); no compiler kind states a task, a mission or a job of its own,
or an outcome (root ADR 0047 §9). So a run with task evidence and a time placement holds one
episode, bounded by what bounds the run; a run with none (or one ``memory.runs`` cannot place in
time) holds no episode.

The consolidator reads ``memory.runs``' claims (it runs after it in a plan, ADR 0003 §4) for each
run's placements, machine and tasks, and the Ledger's ``run``, ``timestamp_domain``,
``intervention`` and ``incident_record`` records:

- ``episode_of(episode, run)`` (the issue's ``part_of``) and ``executes_task`` /
  ``executes_task_candidate`` (``performs``) copy the run's task grounds onto the episode.
- ``starts_at`` / ``ends_at`` (``episode_interval``): the span the run's records state, on each
  clock they state it on, citing them. A clock the run is only projected onto (a clock mapping's
  envelope) gets no boundary. A stated stop (an ``incident_record``) inside the episode leaves the
  end ``Ambiguous``: every reading is an ``ends_at_candidate``.
- ``intervened`` / ``intervened_candidate``: an ``Intervention`` that names the run, or names its
  machine and surely overlaps the episode on one clock (on a projection, only beyond the
  mapping's stated error); an overlap that only might hold is a candidate.
- ``outcome`` is never emitted: no record declares one, and none is inferred. It reads ``Unknown``.

Every claim about an episode holds over the episode's interval on each clock the run is placed on.
Records are parsed by ``consolidate.event_records``; this module decides. Malformed or
contradictory input is a finding and never a claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Final, Literal

from neptune.identity import canonical_json
from neptune.identity.ids import record_id
from neptune.model.finding import Severity
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    NotCovered,
    Unknown,
)
from neptune.model.time import INT64_MAX, Timestamp
from neptune_memory.consolidate import event_records as events
from neptune_memory.consolidate import run_records
from neptune_memory.consolidate.base import ClaimDraft, ConsolidationFinding, ConsolidatorOutput
from neptune_memory.consolidate.identity import node_ref
from neptune_memory.consolidate.runs import EVIDENCED_BY, RUNS_CONSOLIDATOR_ID
from neptune_memory.schema.claim import LedgerRecordRef, TypedLiteral, ValueType, is_inferred
from neptune_memory.schema.interval import OPEN, Open
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import (
    CANDIDATE_OF,
    ENDS_AT,
    EPISODE_CANDIDATE_OF,
    EXECUTES_TASK,
    INTERVENED,
    OUTCOME,
    RECORDED_BY,
    STARTS_AT,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from neptune.model.ids import RecordId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune.model.provenance import EvidenceRef
    from neptune_memory.consolidate.base import ModelRef
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim, ClaimObject
    from neptune_memory.schema.interval import CivilClock

EPISODES_CONSOLIDATOR_ID: Final = "memory.episodes"
EPISODE_OF: Final = "episode_of"
OBSERVED: Final = AssertionKind.OBSERVED
STATED: Final = AssertionKind.STATED
# An episode node id: a content hash, so it never collides with a declared ``<namespace>:<value>``.
EPISODE_ID_KIND: Final = "memory.episode"

_TASKS: Final = (EXECUTES_TASK, CANDIDATE_OF[EXECUTES_TASK])
_MACHINES: Final = (RECORDED_BY, CANDIDATE_OF[RECORDED_BY])


def _finding(
    code: str,
    message: str,
    records: Iterable[RecordId] = (),
    severity: Severity = Severity.WARNING,
    **details: JsonValue,
) -> ConsolidationFinding:
    return ConsolidationFinding(
        code=f"episodes.{code}",
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


# --- Reading the Ledger -------------------------------------------------------------------------


@dataclass
class _View:
    runs: set[RecordId] = field(default_factory=set)  # every Run record id the Ledger holds
    clocks: dict[RecordId, CivilClock] = field(default_factory=dict)  # civil-declared domains
    # Each clock mapping's stated residual bound in ticks (None: unstated), to tell a run's
    # projected placement from a stated one and to know how far a projection may be off.
    bounds: dict[RecordId, int | None] = field(default_factory=dict)
    interventions: list[events.Event] = field(default_factory=list)  # by record id
    incidents: list[events.Event] = field(default_factory=list)  # by record id
    findings: list[ConsolidationFinding] = field(default_factory=list)

    def place(self, stamp: Timestamp) -> Timestamp:
        """A stamp on a clock that declares itself civil, on that ``CivilClock``; else as declared
        (ADR 0009 §2)."""
        clock = self.clocks.get(stamp.domain_id)
        return stamp if clock is None else clock.at(stamp.ticks)


_EVENTS: Final[Mapping[str, Callable[[Mapping[str, object]], events.Event]]] = {
    events.INTERVENTION: events.intervention,
    events.INCIDENT: events.incident,
}


def _read(ledger: LedgerReader) -> _View:
    """Run ids, civil clocks and events. Run and clock records are read for their ids and civil
    declarations only: ``memory.runs`` reports their faults, so they are not reported twice. A
    clock id with two contents is no clock here, as it is none for ``memory.runs``."""
    view = _View()
    clocks: dict[RecordId, run_records.Clock | None] = {}  # None: one id, two contents
    seen: dict[RecordId, tuple[bytes, events.Event]] = {}  # by id: its whole content, parsed
    conflicted: set[RecordId] = set()
    for ref in ledger.list_packages():
        for record in ledger.read_records(ref.package_id, run_records.RUN) or ():
            try:
                view.runs.add(run_records.run(record).id)
            except (run_records.Malformed, run_records.Inferred):
                continue
        for record in ledger.read_records(ref.package_id, run_records.TIMESTAMP_DOMAIN) or ():
            try:
                clock = run_records.clock(record)
            except run_records.Malformed:
                continue
            if clocks.setdefault(clock.record, clock) != clock:
                clocks[clock.record] = None
        for record in ledger.read_records(ref.package_id, run_records.CLOCK_MAPPING) or ():
            try:
                found = run_records.mapping(record)
            except (run_records.Malformed, run_records.Inferred):
                continue
            bound = found.residual_bound
            view.bounds[found.id] = bound.value.ticks if isinstance(bound, Known) else None
        for kind, parser in _EVENTS.items():
            for index, record in enumerate(ledger.read_records(ref.package_id, kind) or ()):
                try:
                    event = parser(record)
                    content = canonical_json.dumps(dict(record))  # type: ignore[arg-type]
                except (events.Malformed, ValueError, TypeError) as exc:
                    view.findings.append(_malformed(kind, ref.package_id, index, str(exc)))
                    continue
                if event.record in conflicted:
                    continue
                if seen.setdefault(event.record, (content, event))[0] != content:
                    conflicted.add(event.record)
                    view.findings.append(
                        _finding(
                            "record_conflict",
                            "one record id carries different content in two places; "
                            "record not used",
                            (event.record,),
                            Severity.ERROR,
                        )
                    )
    view.clocks = {
        rid: clock.civil
        for rid, clock in sorted(clocks.items())
        if clock is not None and clock.civil is not None
    }
    for rid in sorted(seen):
        if rid in conflicted:
            continue
        event = seen[rid][1]
        (view.interventions if event.kind == events.INTERVENTION else view.incidents).append(event)
    return view


# --- Runs as memory.runs places them ------------------------------------------------------------


@dataclass
class _Run:
    """One run node as ``memory.runs``' claims state it: the claims placing its ``Run`` records
    (``evidenced_by`` a run record), its machine and its tasks."""

    node: NodeRef
    spans: list[Claim] = field(default_factory=list)
    machines: list[Claim] = field(default_factory=list)
    tasks: list[Claim] = field(default_factory=list)

    def clocks(self) -> list[RecordId]:
        return sorted({c.valid_from.domain_id for c in self.spans})

    def on(self, clock: RecordId) -> list[Claim]:
        return [c for c in self.spans if c.valid_from.domain_id == clock]

    @cached_property
    def known_machines(self) -> frozenset[str]:
        """The machines a ``recorded_by`` claim decides. Read only once ``_runs`` is done."""
        return frozenset(
            c.object.node_id
            for c in self.machines
            if c.predicate == RECORDED_BY and isinstance(c.object, NodeRef)
        )

    @cached_property
    def machine_readings(self) -> frozenset[str]:
        """Every machine the run's claims name, decided or a candidate."""
        return frozenset(c.object.node_id for c in self.machines if isinstance(c.object, NodeRef))


def _runs(previous: Sequence[Claim], view: _View) -> dict[NodeRef, _Run]:
    """Run nodes from ``memory.runs``' observed and stated claims. Inferred claims, and claims of
    any other consolidator, are never grounds for an episode."""
    out: dict[NodeRef, _Run] = {}
    for claim in previous:
        if (
            claim.provenance.consolidator_id != RUNS_CONSOLIDATOR_ID
            or is_inferred(claim.assertion_kind)
            or claim.subject.node_type is not NodeType.RUN
        ):
            continue
        run = out.setdefault(claim.subject, _Run(claim.subject))
        if claim.predicate == EVIDENCED_BY and isinstance(claim.object, LedgerRecordRef):
            if claim.object.record_id in view.runs:
                run.spans.append(claim)
        elif claim.predicate in _MACHINES:
            run.machines.append(claim)
        elif claim.predicate in _TASKS:
            run.tasks.append(claim)
    return out


@dataclass(frozen=True)
class _Window:
    """The episode's interval on one clock: the span of its run's placements there."""

    start: Timestamp
    end: Timestamp | Open

    def holds(self, start: Timestamp, end: Timestamp) -> bool:
        """Whether ``[start, end)`` overlaps the window (both on its clock)."""
        return start < self.end_or(end) and self.start < end

    def end_or(self, default: Timestamp) -> Timestamp:
        return default if isinstance(self.end, Open) else self.end

    def inside(self, instant: Timestamp) -> bool:
        """Strictly after the start and before the end: where a stop ends an attempt early."""
        return self.start < instant and (isinstance(self.end, Open) or instant < self.end)


def _window(spans: Sequence[Claim]) -> _Window:
    ends = [c.valid_to for c in spans]
    end: Timestamp | Open = (
        OPEN
        if any(isinstance(e, Open) for e in ends)
        else max((e for e in ends if isinstance(e, Timestamp)), key=lambda e: e.ticks)
    )
    return _Window(min((c.valid_from for c in spans), key=lambda s: s.ticks), end)


# --- Events against an episode ------------------------------------------------------------------


@dataclass(frozen=True)
class _Link:
    """How an event bears on a run: ``stated`` (its ``related`` names the run), ``decided`` (it
    names the run's machine, or the run states one machine), the evidence of either."""

    stated: bool
    decided: bool
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]


def _link(event: events.Event, run: _Run) -> _Link | None:
    named = [n for n in event.related if run.node.node_id in _nodes(NodeType.RUN, n)]
    if named:
        decided = any(n.decided for n in named)
        return _Link(True, decided, tuple(r for n in named for r in n.evidence), ())
    known, readings = run.known_machines, run.machine_readings
    matches = [n for n in event.machines if readings & _nodes(NodeType.MACHINE, n)]
    if not matches:
        return None
    decided = any(n.decided and known & _nodes(NodeType.MACHINE, n) for n in matches)
    cited = [c for c in run.machines if isinstance(c.object, NodeRef)]
    return _Link(
        False,
        decided,
        (
            *(r for n in matches for r in n.evidence),
            *(r for c in cited for r in c.provenance.evidence),
        ),
        tuple(r for c in cited for r in c.provenance.records),
    )


def _nodes(node_type: NodeType, named: events.Named) -> set[str]:
    return {node_ref(node_type, i).node_id for i in named.ids}


def _span(view: _View, event: events.Event) -> tuple[Timestamp, Timestamp] | None:
    """``[start, end + 1 tick)`` on the start's clock (placed on its civil clock if it declares
    one); a single stated instant is one tick. An end on another clock, before the start, or not
    stated leaves the instant alone. ``None`` when neither instant is stated."""
    first = event.start if event.start is not None else event.end
    if first is None:
        return None
    start = view.place(first)
    if start.ticks >= INT64_MAX:
        return None
    end = Timestamp(start.ticks + 1, start.domain_id)
    if event.start is not None and event.end is not None and event.end.ticks < INT64_MAX:
        after = view.place(Timestamp(event.end.ticks + 1, event.end.domain_id))
        if after.domain_id == start.domain_id and start < after:
            end = after
    return start, end


# --- The policy ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Boundary:
    """One reading of a boundary on one clock: the instant and what states it."""

    instant: Timestamp
    kind: AssertionKind
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]


def _stated(instant: Timestamp, grounds: Sequence[Claim]) -> _Boundary:
    """A boundary the run's own placements state: every placement at that instant cites it."""
    return _Boundary(
        instant,
        STATED if any(c.assertion_kind is STATED for c in grounds) else OBSERVED,
        tuple(r for c in grounds for r in c.provenance.evidence),
        tuple(r for c in grounds for r in c.provenance.records),
    )


@dataclass
class _Episode:
    """A run's one episode: its window on each clock, its stated start and end there, the stops
    inside it (each an end reading), and where the run surely was."""

    run: _Run
    windows: dict[RecordId, _Window]
    starts: dict[RecordId, _Boundary]
    ends: dict[RecordId, _Boundary]  # only where every placement on the clock states its end
    stops: dict[RecordId, list[_Boundary]]
    # Where the run surely was on each clock: stated placements, and projections shrunk by
    # their mapping's stated error (none where the error is unstated).
    certain: dict[RecordId, list[_Window]]
    node: NodeRef = field(init=False)

    def __post_init__(self) -> None:
        self.node = episode_node(self.run.node, self.windows, self.run.spans)


def episode_node(
    run: NodeRef, windows: Mapping[RecordId, _Window], spans: Sequence[Claim]
) -> NodeRef:
    """An episode's node: a hash of its run, its stated boundaries on every clock and the records
    stating them (ADR 0012 §3). Stops and interventions are claims about it, never in its id."""
    content: dict[str, JsonValue] = {
        "boundaries": [
            {"end": windows[clock].end.to_json(), "start": windows[clock].start.to_json()}
            for clock in sorted(windows)
        ],
        "records": sorted({r for c in spans for r in c.provenance.records}),
        "run": run.node_id,
    }
    digest = record_id(EPISODE_ID_KIND, content).removeprefix("rec:")
    return NodeRef(NodeType.EPISODE, f"episode:{digest}")


@dataclass
class _Build:
    view: _View
    drafts: list[ClaimDraft] = field(default_factory=list)

    def emit(
        self,
        episode: _Episode,
        predicate: str,
        obj: ClaimObject,
        kind: AssertionKind,
        evidence: Iterable[EvidenceRef],
        records: Iterable[RecordId],
        clocks: Iterable[RecordId] | None = None,
    ) -> None:
        evidence, records = tuple(evidence), tuple(records)
        for clock in sorted(episode.windows) if clocks is None else clocks:
            window = episode.windows[clock]
            spans = episode.run.on(clock)
            self.drafts.append(
                ClaimDraft(
                    subject=episode.node,
                    predicate=predicate,
                    object=obj,
                    valid_from=window.start,
                    valid_to=window.end,
                    assertion_kind=kind,
                    evidence=(*evidence, *(r for c in spans for r in c.provenance.evidence)),
                    records=(*records, *(r for c in spans for r in c.provenance.records)),
                )
            )


@dataclass(frozen=True)
class _Index:
    """Episodes by the run node ids and machine node ids an event can name, so each event is
    linked only to the episodes it could bear on."""

    by_run: Mapping[str, _Episode]
    by_machine: Mapping[str, list[_Episode]]

    @staticmethod
    def of(episodes: Sequence[_Episode]) -> _Index:
        by_machine: dict[str, list[_Episode]] = {}
        for episode in episodes:
            for machine in sorted(episode.run.machine_readings):
                by_machine.setdefault(machine, []).append(episode)
        return _Index({e.run.node.node_id: e for e in episodes}, by_machine)

    def reach(self, event: events.Event) -> list[_Episode]:
        named = {e for n in event.related for e in _nodes(NodeType.RUN, n)}
        named |= {
            e.run.node.node_id
            for n in event.machines
            for m in _nodes(NodeType.MACHINE, n)
            for e in self.by_machine.get(m, ())
        }
        return [self.by_run[n] for n in sorted(named) if n in self.by_run]


class EpisodeConsolidator:
    """Deterministic episodes from stated task evidence (ADR 0012). Takes no configuration and
    runs after ``memory.runs``, whose claims it reads."""

    consolidator_id: Final = EPISODES_CONSOLIDATOR_ID
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
                    "the episode consolidator takes no configuration; keys ignored",
                    keys=sorted(config),
                )
            )
        runs = _runs(previous, view)
        if view.runs and not any(run.spans for run in runs.values()):
            view.findings.append(
                _finding(
                    "no_run_claims",
                    "the Ledger holds runs but no memory.runs claim places one: run memory.runs "
                    "before memory.episodes; no episode is built",
                    severity=Severity.INFO,
                )
            )
        episodes = [
            _episode(view, runs[node])
            for node in sorted(runs, key=lambda n: n.node_id)
            if runs[node].spans and runs[node].tasks
        ]
        index = _Index.of(episodes)
        build = _Build(view)
        for event in view.incidents:
            span = _span(view, event)
            for episode in index.reach(event):
                link = _link(event, episode.run)
                if link is not None:
                    _stop(view, episode, event, link, span)
        for episode in episodes:
            _claims(build, episode)
        for event in view.interventions:
            span = _span(view, event)
            for episode in index.reach(event):
                link = _link(event, episode.run)
                if link is not None:
                    _intervened(build, episode, event, link, span)
        return ConsolidatorOutput(tuple(build.drafts), tuple(view.findings))


def _projection(view: _View, span: Claim) -> tuple[bool, int | None]:
    """``(projected, bound)``: whether a placement is a projection through a stated clock mapping
    (it cites one), and that mapping's residual bound in ticks (``None``: unstated)."""
    for record in span.provenance.records:
        if record in view.bounds:
            return True, view.bounds[record]
    return False, 0


def _certain(window: _Window, bound: int) -> _Window | None:
    """The part of a projected placement the run surely covers. ``memory.runs`` rounds a
    projection out by up to a tick and widens it by the bound on each side, and the true instant
    may sit a bound the other way: ``2 * bound + 1`` ticks in from each edge."""
    margin = 2 * bound + 1
    start = Timestamp(window.start.ticks + margin, window.start.domain_id)
    if isinstance(window.end, Open):
        return _Window(start, OPEN)
    end = Timestamp(window.end.ticks - margin, window.end.domain_id)
    return _Window(start, end) if start < end else None


def _episode(view: _View, run: _Run) -> _Episode:
    """The run's one episode: the span of its placements on each clock. The start is the earliest
    stated start, cited by every placement that states it; the end likewise, but only when every
    placement there states its end (one that does not may run on). Placements of one run that do
    not coincide are parts of it, or statements of the same run, never two attempts. A boundary
    is only ever one the run's records state on that clock: where a projection reaches past every
    stated placement (or there is none), the clock has no boundary."""
    windows = {clock: _window(run.on(clock)) for clock in run.clocks()}
    starts: dict[RecordId, _Boundary] = {}
    ends: dict[RecordId, _Boundary] = {}
    certain: dict[RecordId, list[_Window]] = {}
    for clock, window in windows.items():
        spans = run.on(clock)
        stated = [c for c in spans if not _projection(view, c)[0]]
        certain[clock] = []
        for span in spans:
            projected, bound = _projection(view, span)
            whole = _Window(span.valid_from, span.valid_to)
            core = whole if not projected else None if bound is None else _certain(whole, bound)
            if core is not None:
                certain[clock].append(core)
        first = [c for c in stated if c.valid_from == window.start]
        if first:
            starts[clock] = _stated(window.start, first)
        last = [c for c in stated if c.valid_to == window.end]
        if isinstance(window.end, Timestamp) and last:
            ends[clock] = _stated(window.end, last)
    return _Episode(run, windows, starts, ends, {clock: [] for clock in windows}, certain)


def _stop(
    view: _View,
    episode: _Episode,
    event: events.Event,
    link: _Link,
    span: tuple[Timestamp, Timestamp] | None,
) -> None:
    """A stated stop (an ``incident_record`` naming the run or its machine) strictly inside the
    episode is a reading of its end: the attempt may have ended there."""
    if span is None:
        _unplaced(view, episode, event, link)
        return
    instant = span[0]
    window = episode.windows.get(instant.domain_id)
    if window is None:
        _unplaced(view, episode, event, link)
    elif window.inside(instant):
        episode.stops[instant.domain_id].append(
            _Boundary(
                instant,
                STATED if link.stated else OBSERVED,
                (*event.evidence, *link.evidence),
                (event.record, *link.records),
            )
        )


def _claims(build: _Build, episode: _Episode) -> None:
    run = episode.run
    tasks = run.tasks
    kind = STATED if any(c.assertion_kind is STATED for c in tasks) else OBSERVED
    build.emit(
        episode,
        EPISODE_OF,
        run.node,
        kind,
        (r for c in tasks for r in c.provenance.evidence),
        (r for c in tasks for r in c.provenance.records),
    )
    by_task: dict[tuple[str, NodeRef], list[Claim]] = {}
    for claim in tasks:
        if isinstance(claim.object, NodeRef):
            by_task.setdefault((claim.predicate, claim.object), []).append(claim)
    for (predicate, task), grounds in sorted(
        by_task.items(), key=lambda i: (i[0][0], i[0][1].node_id)
    ):
        build.emit(
            episode,
            predicate,
            task,
            STATED if any(c.assertion_kind is STATED for c in grounds) else OBSERVED,
            (r for c in grounds for r in c.provenance.evidence),
            (r for c in grounds for r in c.provenance.records),
        )
    for clock, start in sorted(episode.starts.items()):
        _instant(build, episode, STARTS_AT, clock, start)
    # A stop anywhere makes the end ambiguous on every clock: each stated end and each stop is a
    # reading, and a lone reading only might be the end.
    stopped = any(episode.stops.values())
    for clock in sorted(episode.windows):
        readings = [
            *([episode.ends[clock]] if clock in episode.ends else []),
            *_merged(episode.stops[clock]),
        ]
        for boundary in readings:
            _instant(
                build,
                episode,
                EPISODE_CANDIDATE_OF[ENDS_AT] if stopped else ENDS_AT,
                clock,
                boundary,
            )


def _merged(stops: Sequence[_Boundary]) -> list[_Boundary]:
    """Stops at one instant merged into one reading citing each of them."""
    by_instant: dict[Timestamp, list[_Boundary]] = {}
    for stop in stops:
        by_instant.setdefault(stop.instant, []).append(stop)
    return [
        _Boundary(
            instant,
            STATED if any(b.kind is STATED for b in group) else OBSERVED,
            tuple(r for b in group for r in b.evidence),
            tuple(r for b in group for r in b.records),
        )
        for instant, group in sorted(by_instant.items(), key=lambda i: i[0].ticks)
    ]


def _instant(
    build: _Build, episode: _Episode, predicate: str, clock: RecordId, boundary: _Boundary
) -> None:
    build.emit(
        episode,
        predicate,
        TypedLiteral(ValueType.INSTANT, boundary.instant),
        boundary.kind,
        boundary.evidence,
        boundary.records,
        (clock,),
    )


def _intervened(
    build: _Build,
    episode: _Episode,
    event: events.Event,
    link: _Link,
    span: tuple[Timestamp, Timestamp] | None,
) -> None:
    """``intervened`` when an intervention names the run, or names its one stated machine and
    surely overlaps the episode on one clock; ``intervened_candidate`` when either side's machine
    is ambiguous, when the overlap holds only within a projection's error (or one whose error is
    unstated), or when the intervention names the run but its stated times fall outside it."""
    view = build.view
    window = None if span is None else episode.windows.get(span[0].domain_id)
    overlaps = window is not None and span is not None and window.holds(*span)
    if link.stated:
        if window is None:
            _unplaced(view, episode, event, link)
        outside = window is not None and not overlaps
        if outside:
            view.findings.append(
                _finding(
                    "intervention_outside",
                    "an intervention names the run but its stated times fall outside the run's "
                    "episode; it is only a candidate",
                    (event.record,),
                    episode=episode.node.node_id,
                )
            )
        definite, kind = link.decided and not outside, STATED
    elif overlaps:
        surely = any(c.holds(*span) for c in episode.certain[span[0].domain_id]) if span else False
        definite, kind = link.decided and surely, OBSERVED
    else:
        return
    build.emit(
        episode,
        INTERVENED if definite else EPISODE_CANDIDATE_OF[INTERVENED],
        LedgerRecordRef(event.record),
        kind,
        (*event.evidence, *link.evidence),
        (event.record, *link.records),
    )


def _unplaced(view: _View, episode: _Episode, event: events.Event, link: _Link) -> None:
    """An event that names the run (so it is not just another time of the same machine) but
    states no instant on a clock the episode is placed on."""
    if not link.stated:
        return
    view.findings.append(
        _finding(
            "event_unplaced",
            f"the {event.kind} names the run but states no time on a clock the episode is "
            "placed on",
            (event.record,),
            Severity.INFO,
            episode=episode.node.node_id,
        )
    )


# --- Reading episodes back ----------------------------------------------------------------------


def _named(claims: Iterable[Claim], node: NodeRef) -> bool:
    return any(c.subject == node or c.object == node for c in claims)


def episodes_of(claims: Iterable[Claim], run: NodeRef) -> Knowledge[tuple[NodeRef, ...]]:
    """The episodes ``claims`` place in a run: ``Known`` when there are some, ``Unknown`` when the
    run is named but no evidence states an attempt in it, ``NotCovered`` when no claim names it.
    Pass the claims current at one ``as_of``."""
    claims = tuple(claims)
    if not _named(claims, run):
        return NotCovered()
    found = {c.subject for c in claims if c.predicate == EPISODE_OF and c.object == run}
    if not found:
        return Unknown()
    return Known(tuple(sorted(found, key=lambda n: n.node_id)))


def outcome_of(claims: Iterable[Claim], episode: NodeRef) -> Knowledge[str]:
    """An episode's declared outcome: ``Known`` (one declared text), ``Ambiguous`` (several),
    ``Unknown`` (nothing declares one: never inferred), ``NotCovered`` (no claim names it)."""
    claims = tuple(claims)
    if not _named(claims, episode):
        return NotCovered()
    texts = sorted(
        {
            c.object.value
            for c in claims
            if c.subject == episode
            and c.predicate == OUTCOME
            and isinstance(c.object, TypedLiteral)
            and isinstance(c.object.value, str)
        }
    )
    if len(texts) > 1:
        return Ambiguous(tuple(Candidate(t) for t in texts))
    return Known(texts[0]) if texts else Unknown()


def boundary_of(
    claims: Iterable[Claim],
    episode: NodeRef,
    which: Literal["start", "end"],
    clock: RecordId,
) -> Knowledge[Timestamp]:
    """An episode's start or end on ``clock``: ``Known`` (one stated instant), ``Ambiguous``
    (several readings), ``Unknown`` (none stated there, or one reading that only might be it),
    ``NotCovered`` (no claim names the episode)."""
    claims = tuple(claims)
    if not _named(claims, episode):
        return NotCovered()
    predicate = STARTS_AT if which == "start" else ENDS_AT
    candidate = EPISODE_CANDIDATE_OF.get(predicate)
    known: set[Timestamp] = set()
    readings: set[Timestamp] = set()
    for c in claims:
        if c.subject != episode or not isinstance(c.object, TypedLiteral):
            continue
        value = c.object.value
        if not isinstance(value, Timestamp) or value.domain_id != clock:
            continue
        if c.predicate == predicate:
            known.add(value)
        elif c.predicate == candidate:
            readings.add(value)
    options = sorted(known | readings, key=lambda t: t.ticks)
    if len(options) > 1:
        return Ambiguous(tuple(Candidate(t) for t in options))
    if len(known) == 1:
        return Known(next(iter(known)))
    return Unknown()
