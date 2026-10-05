"""The identity policy as a deterministic consolidator (ADR 0003 §1, ADR 0008).

One node per Ledger thread, keyed by its declared logical id; equal logical ids in any number of
packages are one thread, so one node. ``same_as`` only on three declared grounds, each cited: the
compiler's ``IdentityLink`` with a ``Known`` right side (two ids co-declared, or one identifier
both sides declare verbatim), configuration-lineage continuity, and a person's ``same_identity``
assertion that no effective ``retract`` withdraws. Everything else plausible is a
``same_as_candidate`` pair, one claim each way: every candidate of a link the compiler marked
``Ambiguous``, with that candidate's own evidence, and threads of one type in different namespaces
that cite one identical evidence ref. Nothing is merged: ``same_as`` is an edge that queries
traverse (``schema.traverse.same_as_closure``).

Records are parsed by ``consolidate.identity_records``; this module decides. Malformed or
contradictory input is a finding and never a claim, and the rest of the build is unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from neptune.identity import canonical_json
from neptune.model.assertion import AssertionType
from neptune.model.finding import Severity
from neptune.model.knowledge import AssertionKind
from neptune.model.time import Timestamp
from neptune_memory.consolidate import identity_records as parse
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
    from neptune.model.provenance import EvidenceRef
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

    @property
    def start(self) -> Timestamp:
        """Where a statement that states no time starts: the node's first thread record's start
        (by record id). A convention, not a lifetime: Memory models none (ADR 0007 §3)."""
        return self.threads[0].valid_from


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


# --- Human assertions: retraction ---------------------------------------------------------------

Status = Literal["effective", "retracted", "undecided"]


def _statuses(statements: Sequence[Statement]) -> dict[RecordId, Status]:
    """Whether each assertion stands (root ADR 0062 §5, ADR 0008 §3).

    An assertion is retracted when an effective ``retract`` names its declared identifier, and
    effective when every ``retract`` naming it is itself retracted (or none does); a retraction
    of a retraction therefore restores. What neither rule settles is a loop (a ``retract``
    naming its own id, or two naming each other) and whatever rests on one: undecided. This is
    the grounded labelling, computed with a worklist so a chain of any length is safe.
    """
    attackers: dict[RecordId, list[RecordId]] = {s.record: [] for s in statements}
    targets: dict[RecordId, list[RecordId]] = {s.record: [] for s in statements}
    by_identifier: dict[Key, list[RecordId]] = {}
    for statement in statements:
        if statement.identifier is not None:
            by_identifier.setdefault(_key(statement.identifier), []).append(statement.record)
    for statement in statements:
        if statement.assertion_type is AssertionType.RETRACT and statement.retracts is not None:
            for target in by_identifier.get(_key(statement.retracts), ()):
                attackers[target].append(statement.record)
                targets[statement.record].append(target)
    status: dict[RecordId, Status] = {}
    pending = {record: len(found) for record, found in attackers.items()}
    effective = [record for record, count in pending.items() if count == 0]
    while effective:
        record = effective.pop()
        if record in status:
            continue
        status[record] = "effective"
        for target in targets[record]:
            if target in status:
                continue
            status[target] = "retracted"
            for freed in targets[target]:
                pending[freed] -= 1
                if pending[freed] == 0 and freed not in status:
                    effective.append(freed)
    return {s.record: status.get(s.record, "undecided") for s in statements}


def _from_statements(view: _View) -> tuple[list[Link], dict[frozenset[Key], set[RecordId]]]:
    """``same_identity`` assertions that stand, as links; ``distinct_identity`` ones, as pairs
    with the assertions that declare them distinct."""
    status = _statuses(view.statements)
    known_ids = {_key(s.identifier) for s in view.statements if s.identifier is not None}
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
            and statement.retracts is not None
            and _key(statement.retracts) not in known_ids
        ):
            view.findings.append(
                _finding(
                    "retraction_unmatched",
                    "a retract names an assertion id no assertion in the Ledger carries",
                    rid,
                    retracts=statement.retracts.to_json(),
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
                decided=True,
                window=Window(statement.authored_at, OPEN),
                evidence=statement.evidence,
            )
        )
    return links, distinct


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
    read ADR 0003's stand-ins, so its claims are another lineage.
    """

    consolidator_id: Final = IDENTITY_CONSOLIDATOR_ID
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
                    "the identity consolidator takes no configuration; keys ignored",
                    keys=sorted(config),
                )
            )
        stated, distinct = _from_statements(view)
        links = sorted((*view.links, *stated), key=lambda link: link.record)
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


def _interval(
    view: _View, link: Link, subject: _Node
) -> tuple[Timestamp, Timestamp | Open] | ConsolidationFinding:
    """The statement's window on a shared clock where it declares one; a window that states no
    start holds from the subject's first thread (ADR 0008 §2)."""
    end = OPEN if isinstance(link.window.end, Open) else view.place(link.window.end)
    start = subject.start if link.window.start is None else link.window.start
    start = view.place(start)
    if isinstance(end, Timestamp) and (end.domain_id != start.domain_id or not start < end):
        return _link_finding(
            "untimeable_window",
            link,
            "states an end but no start its subject's clock can place before it",
        )
    return start, end


def _same_as(view: _View, link: Link, side: Side) -> ClaimDraft | None:
    ends = _ends(view, link, side)
    if isinstance(ends, ConsolidationFinding):
        view.findings.append(ends)
        return None
    a, b = ends
    interval = _interval(view, link, a)
    if isinstance(interval, ConsolidationFinding):
        view.findings.append(interval)
        return None
    return ClaimDraft(
        subject=a.ref,
        predicate=SAME_AS,
        object=b.ref,
        valid_from=interval[0],
        valid_to=interval[1],
        assertion_kind=link.assertion_kind,
        evidence=(*link.evidence, *side.evidence),
        records=(link.record,),
    )


def _link_candidates(
    view: _View, link: Link, apart: Callable[[Key, Key], bool]
) -> list[ClaimDraft]:
    """Every candidate of an ambiguous link, one claim each way, each with its own evidence."""
    drafts: list[ClaimDraft] = []
    for side in link.right:
        ends = _ends(view, link, side)
        if isinstance(ends, ConsolidationFinding):
            view.findings.append(ends)
            continue
        if not apart(_key(link.left), _key(side.node)):
            continue
        directions = [
            (subject, obj, _interval(view, link, subject)) for subject, obj in (ends, ends[::-1])
        ]
        refused = [i for _, _, i in directions if isinstance(i, ConsolidationFinding)]
        if refused:  # both ways or neither: a candidate pair is never one-sided
            view.findings.append(refused[0])
            continue
        drafts.extend(
            ClaimDraft(
                subject=subject.ref,
                predicate=SAME_AS_CANDIDATE,
                object=obj.ref,
                valid_from=interval[0],  # type: ignore[index]
                valid_to=interval[1],  # type: ignore[index]
                assertion_kind=link.assertion_kind,
                evidence=(*link.evidence, *side.evidence),
                records=(link.record,),
            )
            for subject, obj, interval in directions
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
