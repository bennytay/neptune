"""The identity policy as a deterministic consolidator (ADR 0003 §1).

One node per Ledger thread, keyed by its declared logical id. ``same_as`` only from a declared
identifier match (compiler ``IdentityLink``, MVL-82), configuration-lineage continuity, or an
operator assertion. Threads of one node type that merely cite the same source get
``same_as_candidate`` claims: the identity is ambiguous and stays undecided. Nothing is merged:
``same_as`` is an edge that queries traverse.

The record shapes below are what Memory consumes through ``LedgerReader``; they are pinned when
MVL-82 (``IdentityLink``) and MVL-85 (catalog API) land. Every link-bearing record carries its own
``valid_from`` and ``evidence``. Malformed records are findings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypeVar

from neptune.identity import canonical_json
from neptune.model.finding import Severity
from neptune.model.ids import (
    LogicalId,
    RecordId,
    check_text,
    logical_id_from_json,
    parse_record_id,
)
from neptune.model.knowledge import AssertionKind
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json
from neptune.model.time import Timestamp, timestamp_from_json
from neptune_memory.consolidate.base import (
    IDENTITY_CONSOLIDATOR_ID,
    ClaimDraft,
    ConsolidationFinding,
    ConsolidatorOutput,
    ModelRef,
)
from neptune_memory.consolidate.base import (
    SAME_AS as SAME_AS,
)
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES, Cardinality, PredicateSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim

SAME_AS_CANDIDATE: Final = "same_as_candidate"

# The vocabulary identity claims are validated against: the core plus the two identity predicates.
IDENTITY_PREDICATES: Final = CORE_PREDICATES.extend(
    PredicateSpec(
        SAME_AS,
        1,
        frozenset(NodeType),
        frozenset(NodeType),
        Cardinality.MANY,
        "the same real-world thing: declared identifier, configuration lineage or operator",
    ),
    PredicateSpec(
        SAME_AS_CANDIDATE,
        1,
        frozenset(NodeType),
        frozenset(NodeType),
        Cardinality.MANY,
        "ambiguous: both cite the same source; whether they are one thing is undecided",
    ),
)

# Ledger record kinds this consolidator reads.
THREAD: Final = "ledger_thread"
IDENTITY_LINK: Final = "identity_link"
CONFIGURATION_LINEAGE: Final = "configuration_lineage"
OPERATOR_ASSERTION: Final = "operator_assertion"

_T = TypeVar("_T")
Ground = Literal["declared_identifier", "configuration_lineage", "operator_assertion"]


def node_ref(node_type: NodeType, logical_id: LogicalId) -> NodeRef:
    """A thread's node id is ``<namespace>:<value>``; a namespace has no ``:``, so it is unique."""
    return NodeRef(node_type, f"{logical_id.namespace}:{logical_id.value}")


def _key(node: LogicalId) -> bytes:
    return canonical_json.dumps(node.to_json())


class _Malformed(ValueError):
    pass


def _field(record: Mapping[str, object], name: str) -> object:
    if name not in record:
        raise _Malformed(f"missing {name!r}")
    return record[name]


def _str(record: Mapping[str, object], name: str) -> str:
    value = _field(record, name)
    if not isinstance(value, str):
        raise _Malformed(f"{name!r} must be a string")
    return value


def _parsed(record: Mapping[str, object], name: str, parse: Callable[[JsonValue], _T]) -> _T:
    try:
        return parse(_field(record, name))  # type: ignore[arg-type]
    except _Malformed:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise _Malformed(f"{name!r}: {exc}") from exc


def _record_id(record: Mapping[str, object]) -> RecordId:
    return _parsed(record, "id", lambda v: parse_record_id(v))  # type: ignore[arg-type]


def _evidence(record: Mapping[str, object]) -> tuple[EvidenceRef, ...]:
    value = _field(record, "evidence")
    if not isinstance(value, (list, tuple)) or not value:
        raise _Malformed("'evidence' must be a non-empty list of evidence refs")
    try:
        return tuple(evidence_ref_from_json(item) for item in value)
    except (ValueError, TypeError, KeyError) as exc:
        raise _Malformed(f"'evidence': {exc}") from exc


@dataclass(frozen=True)
class _Thread:
    record: RecordId
    node: LogicalId
    node_type: NodeType
    valid_from: Timestamp
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class _Link:
    """One grounded ``same_as`` reason from one record."""

    record: RecordId
    ground: Ground
    left: LogicalId
    right: LogicalId
    valid_from: Timestamp
    evidence: tuple[EvidenceRef, ...]


def _thread(record: Mapping[str, object]) -> _Thread:
    return _Thread(
        _record_id(record),
        _parsed(record, "logical_id", logical_id_from_json),
        _parsed(record, "node_type", NodeType),  # type: ignore[arg-type]
        _parsed(record, "valid_from", timestamp_from_json),
        _evidence(record),
    )


_SIDES: Final[Mapping[str, tuple[Ground, str, str]]] = {
    IDENTITY_LINK: ("declared_identifier", "left", "right"),
    CONFIGURATION_LINEAGE: ("configuration_lineage", "predecessor", "successor"),
    OPERATOR_ASSERTION: ("operator_assertion", "subject", "object"),
}


def _link(kind: str, record: Mapping[str, object]) -> _Link | None:
    """Parse one link-bearing record; ``None`` for an operator assertion about another predicate."""
    if kind == IDENTITY_LINK:
        _parsed(record, "identifier", logical_id_from_json)
    if kind == OPERATOR_ASSERTION:
        if _str(record, "predicate") != SAME_AS:
            return None
        _parsed(record, "operator", lambda v: check_text("operator", v))  # type: ignore[arg-type]
    ground, left, right = _SIDES[kind]
    return _Link(
        _record_id(record),
        ground,
        _parsed(record, left, logical_id_from_json),
        _parsed(record, right, logical_id_from_json),
        _parsed(record, "valid_from", timestamp_from_json),
        _evidence(record),
    )


def _malformed(kind: str, package_id: str, index: int, reason: str) -> ConsolidationFinding:
    try:
        reason.encode("utf-8")
    except UnicodeEncodeError:
        reason = "unrepresentable text"
    return ConsolidationFinding(
        code="identity.malformed_record",
        severity=Severity.ERROR,
        message=f"{kind} record {index} in package {package_id!r} is malformed: {reason}"[:1000],
        details={"index": index, "kind": kind, "package_id": package_id},
    )


@dataclass(frozen=True)
class _Node:
    ref: NodeRef
    threads: tuple[_Thread, ...]  # sorted by record id


@dataclass(frozen=True)
class _View:
    nodes: Mapping[bytes, _Node]  # by logical-id key, sorted
    links: tuple[_Link, ...]  # sorted by record id
    findings: tuple[ConsolidationFinding, ...]


def _read(ledger: LedgerReader) -> _View:
    """Collect threads and links across every package, de-duplicated by record id."""
    threads: dict[bytes, dict[RecordId, _Thread]] = {}
    links: dict[RecordId, _Link] = {}
    findings: list[ConsolidationFinding] = []
    seen: dict[RecordId, object] = {}
    conflicted: set[RecordId] = set()
    for ref in ledger.list_packages():
        for index, record in enumerate(ledger.read_records(ref.package_id, THREAD) or ()):
            try:
                thread = _thread(record)
            except _Malformed as exc:
                findings.append(_malformed(THREAD, ref.package_id, index, str(exc)))
                continue
            if _admit(seen, conflicted, thread.record, thread, findings):
                threads.setdefault(_key(thread.node), {})[thread.record] = thread
        for kind in (IDENTITY_LINK, CONFIGURATION_LINEAGE, OPERATOR_ASSERTION):
            for index, record in enumerate(ledger.read_records(ref.package_id, kind) or ()):
                try:
                    link = _link(kind, record)
                except _Malformed as exc:
                    findings.append(_malformed(kind, ref.package_id, index, str(exc)))
                    continue
                if link is not None and _admit(seen, conflicted, link.record, link, findings):
                    links[link.record] = link
    for group_of in threads.values():
        for rid in conflicted & group_of.keys():
            del group_of[rid]
    for rid in conflicted & links.keys():
        del links[rid]
    nodes: dict[bytes, _Node] = {}
    for key in sorted(k for k in threads if threads[k]):
        group = tuple(threads[key][rid] for rid in sorted(threads[key]))
        types = sorted({t.node_type for t in group})
        if len(types) > 1:
            findings.append(
                ConsolidationFinding(
                    code="identity.node_type_conflict",
                    severity=Severity.WARNING,
                    message="threads with one logical id declare different node types; no node",
                    records=tuple(t.record for t in group),
                    details={"node_types": [str(t) for t in types]},
                )
            )
            continue
        nodes[key] = _Node(node_ref(types[0], group[0].node), group)
    return _View(nodes, tuple(links[k] for k in sorted(links)), tuple(findings))


def _admit(
    seen: dict[RecordId, object],
    conflicted: set[RecordId],
    rid: RecordId,
    parsed: object,
    findings: list[ConsolidationFinding],
) -> bool:
    """Whether to keep a record; one id with two different contents is dropped with a finding."""
    if rid in conflicted:
        return False
    previous = seen.setdefault(rid, parsed)
    if previous == parsed:
        return True
    conflicted.add(rid)
    findings.append(
        ConsolidationFinding(
            code="identity.record_conflict",
            severity=Severity.ERROR,
            message="one record id carries different content in two places; record not used",
            records=(rid,),
        )
    )
    return False


class _Components:
    """Union-find over node keys, used only to suppress redundant candidates. Never a merge."""

    def __init__(self) -> None:
        self._parent: dict[bytes, bytes] = {}

    def find(self, key: bytes) -> bytes:
        parent = self._parent.setdefault(key, key)
        if parent != key:
            parent = self._parent[key] = self.find(parent)
        return parent

    def union(self, a: bytes, b: bytes) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)


def nodes(ledger: LedgerReader) -> tuple[NodeRef, ...]:
    """One node per Ledger thread (logical id), in canonical logical-id order."""
    return tuple(node.ref for node in _read(ledger).nodes.values())


def _ref_key(ref: EvidenceRef) -> bytes:
    return canonical_json.dumps(ref.to_json())


class IdentityConsolidator:
    """Deterministic ``same_as`` / ``same_as_candidate`` claims. Takes no configuration."""

    consolidator_id: Final = IDENTITY_CONSOLIDATOR_ID
    version: Final = "1"
    model: Final[ModelRef | None] = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        view = _read(ledger)
        findings = list(view.findings)
        if config:
            findings.append(
                ConsolidationFinding(
                    code="identity.unknown_config",
                    severity=Severity.WARNING,
                    message="the identity consolidator takes no configuration; keys ignored",
                    details={"keys": sorted(config)},
                )
            )
        drafts: list[ClaimDraft] = []
        components = _Components()
        for link in view.links:
            outcome = _same_as(view, link)
            if isinstance(outcome, ConsolidationFinding):
                findings.append(outcome)
                continue
            drafts.append(outcome)
            components.union(_key(link.left), _key(link.right))
        drafts.extend(_candidates(view, components))
        return ConsolidatorOutput(tuple(drafts), tuple(findings))


def _link_finding(
    code: str, link: _Link, message: str, **details: JsonValue
) -> ConsolidationFinding:
    return ConsolidationFinding(
        code=f"identity.{code}",
        severity=Severity.WARNING,
        message=f"{link.ground} record {message}",
        records=(link.record,),
        details=details,
    )


def _same_as(view: _View, link: _Link) -> ClaimDraft | ConsolidationFinding:
    left, right = _key(link.left), _key(link.right)
    if left == right:
        return _link_finding("self_link", link, "links a node to itself")
    missing = [n for k, n in ((left, link.left), (right, link.right)) if k not in view.nodes]
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
    return ClaimDraft(
        subject=a.ref,
        predicate=SAME_AS,
        object=b.ref,
        valid_from=link.valid_from,
        assertion_kind=(
            AssertionKind.STATED if link.ground == "operator_assertion" else AssertionKind.OBSERVED
        ),
        evidence=link.evidence,
        records=(link.record,),
    )


def _candidates(view: _View, components: _Components) -> list[ClaimDraft]:
    """One claim each way per pair of nodes that cite one identical evidence ref and nothing more.

    A pair qualifies when both nodes have the same type, logical ids in different namespaces (two
    values in one namespace are declared distinct), and are not already joined by ``same_as``.
    Evidence is the shared refs; records are the threads citing them. Valid from the subject's
    first such thread (by record id). Citing different parts of one file (rows of a register,
    channels of a log) is not shared evidence.
    """
    citing: dict[bytes, set[bytes]] = {}
    for key, node in view.nodes.items():
        for thread in node.threads:
            for ref in thread.evidence:
                citing.setdefault(_ref_key(ref), set()).add(key)
    pairs: dict[tuple[bytes, bytes], set[bytes]] = {}
    for ref_key, keys in citing.items():
        for a in keys:
            for b in keys:
                if a != b and _comparable(view.nodes[a], view.nodes[b]):
                    pairs.setdefault((a, b), set()).add(ref_key)
    drafts: list[ClaimDraft] = []
    for (a, b), shared in sorted(pairs.items()):
        if components.find(a) == components.find(b):
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
                valid_from=first.valid_from,
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
