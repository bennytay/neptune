"""The identity policy as a deterministic consolidator (ADR 0003 §1, ADR 0008).

One node per Ledger thread, keyed by its declared logical id; equal logical ids in any number of
packages are one thread, so one node. ``same_as`` only on three declared grounds, each cited: the
compiler's ``IdentityLink`` with a ``Known`` right side (two ids co-declared, or one identifier
both sides declare verbatim), configuration-lineage continuity, and a person's ``same_identity``
assertion that no effective ``retract`` withdraws; and since version 3, a ``Machine`` record that
declares one machine by several ids (a manifest entry's id and its aliases: ADR 0021). Everything
else plausible is a
``same_as_candidate`` pair, one claim each way: every candidate of a link the compiler marked
``Ambiguous``, with that candidate's own evidence; every window of a statement whose validity (or
a bound of it, or an assertion's ``authored_at``) is ``Ambiguous``, with that window's evidence;
and threads of one type in different namespaces that cite one identical evidence ref. Nothing is
merged: ``same_as`` is an edge that queries traverse (``schema.traverse.same_as_closure``).

Records are parsed by ``consolidate.identity_records``; this module decides. Malformed or
contradictory input is a finding and never a claim, and the rest of the build is unaffected.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, TypeAlias

from neptune.identity import canonical_json
from neptune.model.assertion import AssertionType
from neptune.model.finding import Severity
from neptune.model.knowledge import AssertionKind, Known
from neptune.model.time import Timestamp
from neptune_memory.consolidate import identity_records as parse
from neptune_memory.consolidate import run_records
from neptune_memory.consolidate.base import (
    IDENTITY_CONSOLIDATOR_ID,
    ClaimDraft,
    ConsolidationFinding,
    ConsolidatorOutput,
    ModelRef,
)
from neptune_memory.consolidate.identity_records import (
    ASSERTION,
    CONFIGURATION_LINEAGE,
    IDENTITY_LINK,
    MACHINE,
    THREAD,
    TIMESTAMP_DOMAIN,
    Link,
    Side,
    Statement,
    Thread,
    Window,
)
from neptune_memory.schema.interval import OPEN, CivilClock, Open
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES
from neptune_memory.schema.predicates import SAME_AS as SAME_AS
from neptune_memory.schema.predicates import SAME_AS_CANDIDATE as SAME_AS_CANDIDATE

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from neptune.model.ids import LogicalId, RecordId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune.model.provenance import EvidenceRef
    from neptune.model.run import Run, RunDeclaration
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim

# The vocabulary identity claims are validated against. Since graph-schema v1 the identity
# predicates are core (ADR 0006 §4), so this is ``CORE_PREDICATES``; kept as a name for callers.
IDENTITY_PREDICATES: Final = CORE_PREDICATES


Key = bytes  # a logical id's canonical JSON: the order subjects are chosen in (ADR 0003 §1.4)


def node_ref(node_type: NodeType, logical_id: LogicalId) -> NodeRef:
    """A thread's node id is ``<namespace>:<value>``; a namespace has no ``:``, so it is unique."""
    return NodeRef(node_type, f"{logical_id.namespace}:{logical_id.value}")


def _key(node: LogicalId) -> Key:
    return canonical_json.dumps(node.to_json())


def _ref_key(ref: EvidenceRef) -> bytes:
    return canonical_json.dumps(ref.to_json())


def _finding(
    code: str,
    message: str,
    records: Iterable[RecordId] = (),
    severity: Severity = Severity.WARNING,
    **details: JsonValue,
) -> ConsolidationFinding:
    return ConsolidationFinding(
        code=f"identity.{code}",
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


@dataclass(frozen=True)
class _Node:
    ref: NodeRef
    threads: tuple[Thread, ...]  # sorted by record id


@dataclass
class _View:
    nodes: dict[Key, _Node] = field(default_factory=dict)  # by logical-id key, sorted
    links: list[Link] = field(default_factory=list)  # sorted by record id, then sides
    statements: list[Statement] = field(default_factory=list)  # sorted by record id
    clocks: dict[RecordId, CivilClock] = field(default_factory=dict)
    findings: list[ConsolidationFinding] = field(default_factory=list)

    def place(self, stamp: Timestamp) -> Timestamp:
        """A stamp on a clock that declares itself civil, moved onto that ``CivilClock`` (the
        same instant, the same ticks: ADR 0002 §3); any other stamp as declared."""
        clock = self.clocks.get(stamp.domain_id)
        return stamp if clock is None else clock.at(stamp.ticks)


class _Admitted:
    """Records de-duplicated by id across packages: one id with two contents is dropped."""

    def __init__(self, findings: list[ConsolidationFinding]) -> None:
        self._seen: dict[RecordId, object] = {}
        self.conflicted: set[RecordId] = set()
        self._findings = findings

    def admit(self, rid: RecordId, parsed: object) -> bool:
        if rid in self.conflicted:
            return False
        previous = self._seen.setdefault(rid, parsed)
        if previous == parsed:
            return True
        self.conflicted.add(rid)
        self._findings.append(
            _finding(
                "record_conflict",
                "one record id carries different content in two places; record not used",
                (rid,),
                Severity.ERROR,
            )
        )
        return False


# The kinds this consolidator reads, in the order it reads them from each package.
_PARSERS: Final[Mapping[str, Callable[[Mapping[str, object]], object]]] = {
    THREAD: parse.thread,
    TIMESTAMP_DOMAIN: parse.clock,
    IDENTITY_LINK: parse.identity_link,
    CONFIGURATION_LINEAGE: parse.configuration_lineage,
    ASSERTION: parse.assertion,
}


def _read(ledger: LedgerReader) -> _View:
    """Parse every record identity reads, in every package, de-duplicated by record id."""
    view = _View()
    admitted = _Admitted(view.findings)
    threads: dict[Key, dict[RecordId, Thread]] = {}
    links: dict[RecordId, Link] = {}
    statements: dict[RecordId, Statement] = {}
    clocks: dict[RecordId, parse.Clock] = {}
    for ref in ledger.list_packages():
        for kind, parser in _PARSERS.items():
            for index, record in enumerate(ledger.read_records(ref.package_id, kind) or ()):
                try:
                    parsed = parser(record)
                except parse.Inferred:
                    view.findings.append(_inferred(ref.package_id, index))
                    continue
                except parse.Malformed as exc:
                    view.findings.append(_malformed(kind, ref.package_id, index, str(exc)))
                    continue
                rid: RecordId = parsed.record  # type: ignore[attr-defined]
                if not admitted.admit(rid, parsed):
                    continue
                if isinstance(parsed, Thread):
                    threads.setdefault(_key(parsed.node), {})[rid] = parsed
                elif isinstance(parsed, Link):
                    links[rid] = parsed
                elif isinstance(parsed, Statement):
                    statements[rid] = parsed
                elif isinstance(parsed, parse.Clock):
                    clocks[rid] = parsed
    for group in threads.values():
        for rid in admitted.conflicted & group.keys():
            del group[rid]
    view.links = [links[r] for r in sorted(links.keys() - admitted.conflicted)]
    view.statements = [statements[r] for r in sorted(statements.keys() - admitted.conflicted)]
    view.clocks = {
        rid: c.civil
        for rid, c in clocks.items()
        if c.civil is not None and rid not in admitted.conflicted
    }
    for key in sorted(k for k in threads if threads[k]):
        group_ = tuple(threads[key][rid] for rid in sorted(threads[key]))
        types = sorted({t.node_type for t in group_})
        if len(types) > 1:
            view.findings.append(
                _finding(
                    "node_type_conflict",
                    "threads with one logical id declare different node types; no node",
                    (t.record for t in group_),
                    node_types=[str(t) for t in types],
                )
            )
            continue
        view.nodes[key] = _Node(node_ref(types[0], group_[0].node), group_)
    return view


def _inferred(package_id: str, index: int) -> ConsolidationFinding:
    return _finding(
        "inferred_link",
        f"identity_link record {index} in package {package_id!r} is inferred: a derived/ "
        "consolidator's input, never a same_as ground",
        severity=Severity.INFO,
        index=index,
        package_id=package_id,
    )


def nodes(ledger: LedgerReader) -> tuple[NodeRef, ...]:
    """One node per Ledger thread (logical id), in canonical logical-id order."""
    return tuple(node.ref for node in _read(ledger).nodes.values())


def node_threads(ledger: LedgerReader) -> Mapping[NodeRef, tuple[Thread, ...]]:
    """Each node ``nodes`` keys, with its thread records sorted by record id. Other consolidators
    resolve the ids a record declares through this, so no node is keyed twice (ADR 0010 §1)."""
    return {node.ref: node.threads for node in _read(ledger).nodes.values()}


# --- Human assertions: retraction ---------------------------------------------------------------

Status = Literal["effective", "retracted", "undecided", "doubtful"]
# A retraction edge: attacker, target, and whether it certainly names the target.
_Edge: TypeAlias = "tuple[RecordId, RecordId, bool]"


def _edges(statements: Sequence[Statement]) -> list[_Edge]:
    """Which ``retract`` names which assertion. An edge is certain when the retract names one id
    (``Known``) and the target carries one (``Known``); where either is ``Ambiguous`` the retract
    only possibly names the target (ADR 0008 §3, graph-schema rule 11)."""
    by_identifier: dict[Key, list[Statement]] = {}
    for statement in statements:
        for identifier in statement.identifiers:
            by_identifier.setdefault(_key(identifier), []).append(statement)
    edges: dict[tuple[RecordId, RecordId], bool] = {}
    for statement in statements:
        if statement.assertion_type is not AssertionType.RETRACT:
            continue
        for named in statement.retracts:
            for target in by_identifier.get(_key(named), ()):
                certain = not statement.retracts_ambiguous and not target.identifier_ambiguous
                pair = (statement.record, target.record)
                edges[pair] = edges.get(pair, False) or certain
    return [(attacker, target, certain) for (attacker, target), certain in edges.items()]


def _label(records: Iterable[RecordId], edges: Sequence[_Edge]) -> dict[RecordId, Status]:
    """The grounded labelling over certain and possible retractions, computed with a worklist so
    a chain of any length is safe. An assertion is effective when every retract that names it,
    certainly or possibly, is retracted (or none does); retracted when a retract that certainly
    names it is effective. Anything else is left unlabelled."""
    attackers: dict[RecordId, int] = dict.fromkeys(records, 0)
    targets: dict[RecordId, list[tuple[RecordId, bool]]] = {r: [] for r in attackers}
    for attacker, target, certain in edges:
        attackers[target] += 1
        targets[attacker].append((target, certain))
    status: dict[RecordId, Status] = {}
    effective = [record for record, count in attackers.items() if count == 0]
    while effective:
        record = effective.pop()
        if record in status:
            continue
        status[record] = "effective"
        for target, certain in targets[record]:
            if not certain or target in status:
                continue
            status[target] = "retracted"
            for freed, _ in targets[target]:
                attackers[freed] -= 1
                if attackers[freed] == 0 and freed not in status:
                    effective.append(freed)
    return status


def _statuses(statements: Sequence[Statement]) -> dict[RecordId, Status]:
    """Whether each assertion stands (root ADR 0062 §5, ADR 0008 §3).

    An assertion is retracted when an effective ``retract`` names its declared identifier, and
    effective when every ``retract`` naming it is itself retracted (or none does); a retraction
    of a retraction therefore restores. What neither rule settles is a loop (a ``retract``
    naming its own id, or two naming each other) and whatever rests on one: undecided. What
    certain retractions alone would settle but a retract that only possibly names it (an
    ``Ambiguous`` ``retracts`` or ``identifier``) leaves open is doubtful: never a decided fact.
    """
    records = [s.record for s in statements]
    edges = _edges(statements)
    certain = _label(records, [e for e in edges if e[2]])
    final = _label(records, edges)
    return {r: final.get(r, "undecided" if r not in certain else "doubtful") for r in records}


def _doubters(
    record: RecordId, edges: Sequence[_Edge], status: Mapping[RecordId, Status]
) -> list[RecordId]:
    """The retracts that leave ``record`` in doubt: those naming it that are not retracted."""
    return sorted({a for a, t, _ in edges if t == record and status[a] != "retracted"})


def _from_statements(view: _View) -> tuple[list[Link], dict[frozenset[Key], set[RecordId]]]:
    """``same_identity`` assertions that stand, as links; ``distinct_identity`` ones, as pairs
    with the assertions that declare them distinct. A ``same_identity`` that may have been
    retracted, or whose own identifier is ``Ambiguous``, is an undecided link: candidates."""
    status = _statuses(view.statements)
    edges = _edges(view.statements)
    by_record = {s.record: s for s in view.statements}
    known_ids = {_key(i) for s in view.statements for i in s.identifiers}
    links: list[Link] = []
    distinct: dict[frozenset[Key], set[RecordId]] = {}
    for statement in view.statements:
        kind, rid = statement.assertion_type, (statement.record,)
        if kind is None:
            view.findings.append(
                _finding("assertion_unread", "an assertion's type is not stated as Known", rid)
            )
            continue
        if (
            kind is AssertionType.RETRACT
            and statement.retracts
            and not any(_key(named) in known_ids for named in statement.retracts)
        ):
            named = [r.to_json() for r in statement.retracts]
            view.findings.append(
                _finding(
                    "retraction_unmatched",
                    "a retract names an assertion id no assertion in the Ledger carries",
                    rid,
                    retracts=named if statement.retracts_ambiguous else named[0],
                )
            )
        if kind not in (AssertionType.SAME_IDENTITY, AssertionType.DISTINCT_IDENTITY):
            continue
        if status[statement.record] == "undecided":
            view.findings.append(
                _finding(
                    "retraction_undecided",
                    "retractions of this assertion form a loop; nothing rests on it",
                    rid,
                )
            )
            continue
        if status[statement.record] == "retracted":
            continue
        doubters: list[RecordId] = []
        if status[statement.record] == "doubtful":
            doubters = _doubters(statement.record, edges, status)
            view.findings.append(
                _finding(
                    "retraction_ambiguous",
                    f"a retract may name this {kind} assertion; it is not a decided statement",
                    (statement.record, *doubters),
                )
            )
            if kind is AssertionType.DISTINCT_IDENTITY:
                continue  # a distinctness that may be withdrawn suppresses nothing
        ids = statement.nodes or ()
        if len(ids) < 2:
            view.findings.append(
                _finding(
                    "assertion_scope",
                    f"a {kind} assertion needs two logical ids in a Known scope",
                    rid,
                )
            )
            continue
        ordered = sorted(ids, key=_key)
        if kind is AssertionType.DISTINCT_IDENTITY:
            for a in ordered:
                for b in ordered[ordered.index(a) + 1 :]:
                    distinct.setdefault(frozenset((_key(a), _key(b))), set()).add(statement.record)
            continue
        links.append(
            Link(
                record=statement.record,
                ground="operator_assertion",
                assertion_kind=AssertionKind.STATED,
                left=ordered[0],
                right=tuple(Side(node) for node in ordered[1:]),
                decided=statement.timed
                and not statement.identifier_ambiguous
                and not doubters
                and status[statement.record] == "effective",
                windows=statement.windows,
                evidence=(
                    *statement.evidence,
                    *(ref for d in doubters for ref in by_record[d].evidence),
                ),
                also=tuple(doubters),
            )
        )
    return links, distinct


# --- Machine declarations (ADR 0021) ------------------------------------------------------------


@dataclass(frozen=True)
class _Origin:
    """Where a machine's ids are first placed in time, and what places them there."""

    start: Timestamp
    records: tuple[RecordId, ...]
    evidence: tuple[EvidenceRef, ...]


def _read_machines(view: _View, ledger: LedgerReader) -> list[parse.Declaration]:
    """Every ``machine`` record, de-duplicated by record id; refused ones are findings."""
    admitted = _Admitted(view.findings)
    found: dict[RecordId, parse.Declaration] = {}
    for ref in ledger.list_packages():
        for index, record in enumerate(ledger.read_records(ref.package_id, MACHINE) or ()):
            try:
                parsed = parse.machine(record)
            except parse.Inferred:
                continue  # an inferred machine is a derived/ record: never a ground
            except parse.Malformed as exc:
                view.findings.append(_malformed(MACHINE, ref.package_id, index, str(exc)))
                continue
            if admitted.admit(parsed.record, parsed):
                found[parsed.record] = parsed
    return [found[r] for r in sorted(found.keys() - admitted.conflicted)]


def _run_origins(ledger: LedgerReader) -> dict[Key, list[_Origin]]:
    """Each machine id a run names (its ``Run.machine``, or a ``run_declaration`` of it), with
    that run's ``Known`` first instant. Records others' parsers refuse are theirs to report, and a
    run id with two contents places nothing."""
    runs: dict[RecordId, Run | None] = {}
    declarations: list[RunDeclaration] = []
    for ref in ledger.list_packages():
        for record in ledger.read_records(ref.package_id, run_records.RUN) or ():
            try:
                run = run_records.run(record)
            except (run_records.Inferred, parse.Malformed):
                continue
            runs[run.id] = run if runs.setdefault(run.id, run) == run else None
        for record in ledger.read_records(ref.package_id, run_records.RUN_DECLARATION) or ():
            try:
                declarations.append(run_records.declaration(record))
            except (run_records.Inferred, parse.Malformed):
                continue
    origins: dict[Key, list[_Origin]] = {}

    def place(run: Run | None, node: Knowledge[LogicalId], via: RunDeclaration | None) -> None:
        if run is None or not isinstance(run.first, Known) or not isinstance(node, Known):
            return
        records: tuple[RecordId, ...] = (run.id,)
        cited: tuple[EvidenceRef, ...] = (run.provenance.evidence,)
        if via is not None:
            records, cited = (run.id, via.id), (*cited, via.provenance.evidence)
        origins.setdefault(_key(node.value), []).append(_Origin(run.first.value, records, cited))

    for entry in runs.values():
        if entry is not None:
            place(entry, entry.machine, None)
    for declaration in declarations:
        place(runs.get(declaration.run), declaration.machine, declaration)
    return origins


def _origin(
    view: _View, ids: Sequence[LogicalId], runs: Mapping[Key, Sequence[_Origin]]
) -> _Origin | None:
    """A declaration states no time, so it holds from its machine's first placement (ADR 0021
    §2): the first thread record of any of its ids (ADR 0008 §2), else the first run naming one,
    each by record id. A convention, not a lifetime."""
    threads = [t for i in ids if _key(i) in view.nodes for t in view.nodes[_key(i)].threads]
    if threads:
        first = min(threads, key=lambda t: t.record)
        return _Origin(first.valid_from, (first.record,), ())
    placed = [o for i in ids for o in runs.get(_key(i), ())]
    return min(placed, key=lambda o: o.records) if placed else None


class _Conflicts:
    """Machine declarations joined through a shared id that cannot all be one machine (ADR 0021
    §3): two of them cite one document (which lists them as two), or the join puts two ids of one
    namespace together that no one declaration gives one machine."""

    def __init__(self, declarations: Sequence[parse.Declaration]) -> None:
        components = _Components()
        for d in declarations:
            for side in d.known[1:]:
                components.union(_key(d.known[0].node), _key(side.node))
        groups: dict[Key, list[parse.Declaration]] = {}
        for d in declarations:
            if d.known:
                groups.setdefault(components.find(_key(d.known[0].node)), []).append(d)
        self.groups = [g for g in groups.values() if len(g) > 1 and _clash(g)]
        self.records = {d.record for g in self.groups for d in g}


def _clash(group: Sequence[parse.Declaration]) -> bool:
    documents = [_ref_key_of(d.document) for d in group]
    if len(set(documents)) < len(documents):
        return True
    together = {frozenset(_key(s.node) for s in d.known) for d in group}
    by_namespace: dict[str, set[Key]] = {}
    for d in group:
        for side in d.known:
            by_namespace.setdefault(side.node.namespace, set()).add(_key(side.node))
    return any(
        not any({a, b} <= ids for ids in together)
        for keys in by_namespace.values()
        for a in keys
        for b in keys
        if a < b
    )


def _ref_key_of(source: object) -> bytes:
    return canonical_json.dumps(source if isinstance(source, str) else source.to_json())  # type: ignore[attr-defined]


def _from_machines(view: _View, ledger: LedgerReader) -> list[Link]:
    """One decided link per ``Machine`` record joining its ``Known`` ids to the lowest, and one
    undecided link per ``Ambiguous`` identifier: its candidates against that id (ADR 0021)."""
    declarations = _read_machines(view, ledger)
    if not declarations:
        return []
    runs = _run_origins(ledger)
    usable: list[parse.Declaration] = []
    for d in declarations:
        if d.refused:
            view.findings.append(
                _finding(
                    "machine_identifier_unrepresentable",
                    "a machine record declares ids Memory keys no node by (blank, padded, or in "
                    f"the reserved {parse.RESERVED_NAMESPACE!r} namespace); its other ids still"
                    " count",
                    (d.record,),
                    ids=[i.to_json() for i in d.refused],
                )
            )
        typed = [s for s in d.known if _machine_node(view, d, s.node)]
        alternatives = [
            tuple(s for s in sides if _machine_node(view, d, s.node)) for sides in d.ambiguous
        ]
        known = tuple(sorted(typed, key=lambda s: _key(s.node)))
        usable.append(
            dataclasses.replace(d, known=known, ambiguous=tuple(a for a in alternatives if a))
        )
    conflicts = _Conflicts(usable)
    for group in conflicts.groups:
        view.findings.append(
            _finding(
                "machine_conflict",
                "machine records joined by a shared id cannot all be one machine (one document "
                "lists two of them, or the join gives one machine two ids no declaration pairs); "
                "their ids are candidates, never same_as",
                sorted(d.record for d in group),
                ids=sorted({s.node.namespace + ":" + s.node.value for d in group for s in d.known}),
            )
        )
    links: list[Link] = []
    for d in usable:
        if len(d.known) + len(d.ambiguous) < 2:
            continue
        if not d.known:
            view.findings.append(
                _finding(
                    "machine_undecided",
                    "a machine record states none of its ids as Known; no id to join the "
                    "candidates to",
                    (d.record,),
                )
            )
            continue
        origin = _origin(view, [s.node for s in d.known], runs)
        if origin is None:
            view.findings.append(
                _finding(
                    "machine_unplaced",
                    "no thread or run places this machine's ids in time; its ids are joined "
                    "once one does",
                    (d.record,),
                    Severity.INFO,
                )
            )
            continue
        if len(d.known) > 1:
            decided = d.record not in conflicts.records
            links.append(_declaration_link(d, origin, d.known[1:], decided))
        links.extend(_declaration_link(d, origin, sides, False) for sides in d.ambiguous)
    return links


def _declaration_link(
    d: parse.Declaration, origin: _Origin, right: tuple[Side, ...], decided: bool
) -> Link:
    """A machine record's lowest ``Known`` id against ``right``, from the machine's origin."""
    hub = d.known[0]
    return Link(
        record=d.record,
        ground="machine_declaration",
        assertion_kind=d.assertion_kind,
        left=hub.node,
        right=right,
        decided=decided,
        windows=(Window(origin.start, OPEN, origin.evidence),),
        evidence=(*d.evidence, *hub.evidence),
        also=origin.records,
    )


def _machine_node(view: _View, declaration: parse.Declaration, node: LogicalId) -> bool:
    """Key a machine node for ``node`` unless a Ledger thread keys it as another type."""
    key = _key(node)
    existing = view.nodes.get(key)
    if existing is None:
        view.nodes[key] = _Node(node_ref(NodeType.MACHINE, node), ())
        return True
    if existing.ref.node_type is NodeType.MACHINE:
        return True
    view.findings.append(
        _finding(
            "type_mismatch",
            "a machine record declares an id a Ledger thread keys as another node type",
            (declaration.record, *(t.record for t in existing.threads)),
            node_types=[str(existing.ref.node_type), str(NodeType.MACHINE)],
        )
    )
    return False


# --- The policy ---------------------------------------------------------------------------------


class _Components:
    """Union-find over node keys, used only to suppress redundant candidates. Never a merge."""

    def __init__(self) -> None:
        self._parent: dict[Key, Key] = {}

    def find(self, key: Key) -> Key:
        parent = self._parent.setdefault(key, key)
        if parent != key:
            parent = self._parent[key] = self.find(parent)
        return parent

    def union(self, a: Key, b: Key) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)


class IdentityConsolidator:
    """Deterministic ``same_as`` / ``same_as_candidate`` claims. Takes no configuration.

    Version 2 (ADR 0008) reads the compiler's ``identity_link`` and ``assertion`` kinds; version 1
    read ADR 0003's stand-ins, so its claims are another lineage. Version 3 (ADR 0021) also reads
    ``machine`` records: the ids one declaration gives one machine are ``same_as``.
    """

    consolidator_id: Final = IDENTITY_CONSOLIDATOR_ID
    version: Final = "3"
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
                    "the identity consolidator takes no configuration; keys ignored",
                    keys=sorted(config),
                )
            )
        stated, distinct = _from_statements(view)
        machines = _from_machines(view, ledger)
        links = sorted((*view.links, *stated, *machines), key=lambda link: link.record)
        for link in (link for link in links if not link.windows):
            view.findings.append(
                _link_finding(
                    "untimeable_window",
                    link,
                    f"states more than {parse.MAX_WINDOWS} candidate windows; none is read",
                )
            )
        links = [link for link in links if link.windows]
        drafts: list[ClaimDraft] = []
        components = _Components()
        for link in (link for link in links if link.decided):
            for side in link.right:
                outcome = _same_as(view, link, side)
                if outcome is None:
                    continue
                drafts.append(outcome)
                components.union(_key(link.left), _key(side.node))
        for pair, declared in sorted(distinct.items(), key=lambda item: sorted(item[0])):
            a, b = sorted(pair)
            if components.find(a) != components.find(b):
                continue
            # same_as reaches across a pair a person declared distinct, directly or through a
            # chain: both stand, and the contest names the declaration and any direct ground.
            nodes_ = [view.nodes[a].ref, view.nodes[b].ref]
            view.findings.append(
                _finding(
                    "contested",
                    "same_as joins two ids a distinct_identity assertion declares distinct",
                    (*declared, *_grounds(drafts, *nodes_)),
                    nodes=[n.node_id for n in nodes_],
                )
            )

        def apart(a: Key, b: Key) -> bool:
            return components.find(a) != components.find(b) and frozenset((a, b)) not in distinct

        for link in (link for link in links if not link.decided):
            drafts.extend(_link_candidates(view, link, apart))
        drafts.extend(_shared_evidence_candidates(view, apart))
        return ConsolidatorOutput(tuple(drafts), tuple(view.findings))


def _grounds(drafts: Sequence[ClaimDraft], a: NodeRef, b: NodeRef) -> list[RecordId]:
    """The records grounding ``same_as`` between ``a`` and ``b``: what a contest names."""
    return sorted(
        {
            record
            for d in drafts
            if d.predicate == SAME_AS and {d.subject, d.object} == {a, b}
            for record in d.records
        }
    )


def _link_finding(
    code: str, link: Link, message: str, **details: JsonValue
) -> ConsolidationFinding:
    return _finding(
        code, f"{link.ground} record {message}", (link.record,), Severity.WARNING, **details
    )


def _ends(view: _View, link: Link, side: Side) -> tuple[_Node, _Node] | ConsolidationFinding:
    """The two nodes a statement relates, subject first; a finding if they cannot be related."""
    left, right = _key(link.left), _key(side.node)
    if left == right:
        return _link_finding("self_link", link, "links a node to itself")
    missing = [n for k, n in ((left, link.left), (right, side.node)) if k not in view.nodes]
    if missing:
        return _link_finding(
            "dangling_link",
            link,
            "names a logical id with no Ledger node",
            missing=[n.to_json() for n in missing],
        )
    a, b = view.nodes[min(left, right)], view.nodes[max(left, right)]
    if a.ref.node_type is not b.ref.node_type:
        return _link_finding(
            "type_mismatch",
            link,
            "links nodes of different types",
            node_types=[str(a.ref.node_type), str(b.ref.node_type)],
        )
    return a, b


@dataclass(frozen=True)
class _Timed:
    """Where a statement holds for one subject, and the records and evidence that time cites:
    its window's own candidate, and the subject's first thread when it supplies the start."""

    start: Timestamp
    end: Timestamp | Open
    records: tuple[RecordId, ...]
    evidence: tuple[EvidenceRef, ...]


def _interval(
    view: _View, link: Link, window: Window, subject: _Node
) -> _Timed | ConsolidationFinding:
    """The window on a shared clock where it declares one. A window that states no start holds
    from the subject's first thread record (by record id; ADR 0008 §2), which the claim then
    cites, so a conventional start is told from a stated one. A convention, not a lifetime:
    Memory models none (ADR 0007 §3)."""
    end = OPEN if isinstance(window.end, Open) else view.place(window.end)
    records: tuple[RecordId, ...] = ()
    if window.start is None:
        if not subject.threads:  # a machine record's id, with no thread to start from
            return _link_finding(
                "untimeable_window", link, "states no start and its subject has no thread"
            )
        first = subject.threads[0]
        start, records = view.place(first.valid_from), (first.record,)
    else:
        start = view.place(window.start)
    if isinstance(end, Timestamp) and (end.domain_id != start.domain_id or not start < end):
        return _link_finding(
            "untimeable_window",
            link,
            "states an end but no start its subject's clock can place before it",
        )
    return _Timed(start, end, records, window.evidence)


def _same_as(view: _View, link: Link, side: Side) -> ClaimDraft | None:
    ends = _ends(view, link, side)
    if isinstance(ends, ConsolidationFinding):
        view.findings.append(ends)
        return None
    a, b = ends
    (window,) = link.windows  # a decided statement states one window
    timed = _interval(view, link, window, a)
    if isinstance(timed, ConsolidationFinding):
        view.findings.append(timed)
        return None
    return ClaimDraft(
        subject=a.ref,
        predicate=SAME_AS,
        object=b.ref,
        valid_from=timed.start,
        valid_to=timed.end,
        assertion_kind=link.assertion_kind,
        evidence=(*link.evidence, *side.evidence, *timed.evidence),
        records=(link.record, *link.also, *timed.records),
    )


def _link_candidates(
    view: _View, link: Link, apart: Callable[[Key, Key], bool]
) -> list[ClaimDraft]:
    """Every candidate of an ambiguous link over every window it may hold in, one claim each way,
    each citing that candidate and that window's own evidence."""
    drafts: list[ClaimDraft] = []
    for side in link.right:
        ends = _ends(view, link, side)
        if isinstance(ends, ConsolidationFinding):
            view.findings.append(ends)
            continue
        if not apart(_key(link.left), _key(side.node)):
            continue
        for window in link.windows:
            directions = [
                (subject, obj, _interval(view, link, window, subject))
                for subject, obj in (ends, ends[::-1])
            ]
            timed = [t for _, _, t in directions if isinstance(t, _Timed)]
            if len(timed) < len(directions):  # both ways or neither: never one-sided
                view.findings.append(
                    next(t for _, _, t in directions if isinstance(t, ConsolidationFinding))
                )
                continue
            drafts.extend(
                ClaimDraft(
                    subject=subject.ref,
                    predicate=SAME_AS_CANDIDATE,
                    object=obj.ref,
                    valid_from=t.start,
                    valid_to=t.end,
                    assertion_kind=link.assertion_kind,
                    evidence=(*link.evidence, *side.evidence, *t.evidence),
                    records=(link.record, *link.also, *t.records),
                )
                for (subject, obj, _), t in zip(directions, timed, strict=True)
            )
    return drafts


def _shared_evidence_candidates(view: _View, apart: Callable[[Key, Key], bool]) -> list[ClaimDraft]:
    """One claim each way per pair of nodes that cite one identical evidence ref (ADR 0003 §1.3).

    A pair qualifies when both nodes have the same type, logical ids in different namespaces (two
    values in one namespace are declared distinct), and are neither joined by ``same_as`` nor
    asserted distinct. Evidence is the shared refs; records are the threads citing them. Valid
    from the subject's first such thread (by record id). Citing different parts of one file (rows
    of a register, channels of a log) is not shared evidence.
    """
    citing: dict[bytes, set[Key]] = {}
    for key, node in view.nodes.items():
        for thread in node.threads:
            for ref in thread.evidence:
                citing.setdefault(_ref_key(ref), set()).add(key)
    pairs: dict[tuple[Key, Key], set[bytes]] = {}
    for ref_key, keys in citing.items():
        for a in keys:
            for b in keys:
                if a != b and _comparable(view.nodes[a], view.nodes[b]):
                    pairs.setdefault((a, b), set()).add(ref_key)
    drafts: list[ClaimDraft] = []
    for (a, b), shared in sorted(pairs.items()):
        if not apart(a, b):
            continue
        cited = [
            (thread, ref)
            for key in (a, b)
            for thread in view.nodes[key].threads
            for ref in thread.evidence
            if _ref_key(ref) in shared
        ]
        first = next(thread for thread, _ in cited if _key(thread.node) == a)
        drafts.append(
            ClaimDraft(
                subject=view.nodes[a].ref,
                predicate=SAME_AS_CANDIDATE,
                object=view.nodes[b].ref,
                valid_from=view.place(first.valid_from),
                assertion_kind=AssertionKind.OBSERVED,
                evidence=tuple(ref for _, ref in cited),
                records=tuple(thread.record for thread, _ in cited),
            )
        )
    return drafts


def _comparable(a: _Node, b: _Node) -> bool:
    same_type = a.ref.node_type is b.ref.node_type
    return same_type and a.threads[0].node.namespace != b.threads[0].node.namespace


def same_as_candidates(claims: Sequence[Claim], subject: NodeRef) -> tuple[NodeRef, ...]:
    """The candidate identities of ``subject``: itself (the distinct reading) and each candidate."""
    others = {
        c.object
        for c in claims
        if c.predicate == SAME_AS_CANDIDATE
        and c.subject == subject
        and isinstance(c.object, NodeRef)
    }
    if not others:
        return ()
    return (subject, *sorted(others, key=lambda n: n.node_id))
