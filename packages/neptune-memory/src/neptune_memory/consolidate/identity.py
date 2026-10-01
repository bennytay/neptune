"""The identity policy as a deterministic consolidator (ADR 0003 §1).

One node per Ledger thread, keyed by its declared logical id. ``same_as`` only from a declared
identifier match (compiler ``IdentityLink``, MVL-82), configuration-lineage continuity, or an
operator assertion. Threads that merely share evidence get an ``Ambiguous`` ``same_as_candidate``.
Nothing is merged: ``same_as`` is an edge that queries traverse.

The record shapes below are what Memory consumes through ``LedgerReader``; they are pinned when
MVL-82 (``IdentityLink``) and MVL-85 (catalog API) land. Malformed records are findings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from neptune.identity import canonical_json
from neptune.model.finding import Severity
from neptune.model.ids import (
    ContentId,
    LogicalId,
    RecordId,
    check_text,
    logical_id_from_json,
    parse_content_id,
    parse_record_id,
)
from neptune.model.knowledge import KnowledgeState
from neptune_memory.consolidate.base import (
    ClaimDraft,
    ConsolidationFinding,
    ConsolidatorOutput,
    ModelRef,
    PriorClaim,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.ledger import LedgerReader

# Predicates; MVL-102's predicate registry adopts these names.
SAME_AS: Final = "same_as"
SAME_AS_CANDIDATE: Final = "same_as_candidate"

# Ledger record kinds this consolidator reads.
THREAD: Final = "ledger_thread"
IDENTITY_LINK: Final = "identity_link"
CONFIGURATION_LINEAGE: Final = "configuration_lineage"
OPERATOR_ASSERTION: Final = "operator_assertion"

Ground = Literal["declared_identifier", "configuration_lineage", "operator_assertion"]


def _key(node: LogicalId) -> bytes:
    return canonical_json.dumps(node.to_json())


@dataclass(frozen=True)
class _Link:
    """One grounded ``same_as`` reason from one record."""

    record: RecordId
    ground: Ground
    left: LogicalId
    right: LogicalId
    detail: Mapping[str, JsonValue]


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


def _logical(record: Mapping[str, object], name: str) -> LogicalId:
    try:
        return logical_id_from_json(_field(record, name))  # type: ignore[arg-type]
    except (ValueError, TypeError) as exc:
        raise _Malformed(f"{name!r}: {exc}") from exc


def _record_id(record: Mapping[str, object]) -> RecordId:
    try:
        return parse_record_id(_str(record, "id"))
    except ValueError as exc:
        raise _Malformed(str(exc)) from exc


def _sources(record: Mapping[str, object]) -> tuple[ContentId, ...]:
    value = _field(record, "sources")
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise _Malformed("'sources' must be a list of content ids")
    try:
        return tuple(sorted({parse_content_id(v) for v in value}))
    except ValueError as exc:
        raise _Malformed(str(exc)) from exc


def _link(kind: str, record: Mapping[str, object]) -> _Link | None:
    """Parse one link-bearing record; ``None`` for an operator assertion about another predicate."""
    rid = _record_id(record)
    if kind == IDENTITY_LINK:
        identifier = _logical(record, "identifier")
        return _Link(
            rid,
            "declared_identifier",
            _logical(record, "left"),
            _logical(record, "right"),
            {"identifier": identifier.to_json()},
        )
    if kind == CONFIGURATION_LINEAGE:
        before, after = _logical(record, "predecessor"), _logical(record, "successor")
        return _Link(
            rid,
            "configuration_lineage",
            before,
            after,
            {"predecessor": before.to_json(), "successor": after.to_json()},
        )
    if _str(record, "predicate") != SAME_AS:
        return None
    operator = _str(record, "operator")
    try:
        check_text("operator", operator)
    except ValueError as exc:
        raise _Malformed(str(exc)) from exc
    return _Link(
        rid,
        "operator_assertion",
        _logical(record, "subject"),
        _logical(record, "object"),
        {"operator": operator},
    )


def _malformed(kind: str, package_id: str, index: int, reason: str) -> ConsolidationFinding:
    return ConsolidationFinding(
        code="identity.malformed_record",
        severity=Severity.ERROR,
        message=f"{kind} record {index} in {package_id} is malformed: {reason}",
        details={"index": index, "kind": kind, "package_id": package_id},
    )


@dataclass(frozen=True)
class _View:
    threads: Mapping[bytes, tuple[LogicalId, tuple[tuple[RecordId, tuple[ContentId, ...]], ...]]]
    links: tuple[_Link, ...]
    findings: tuple[ConsolidationFinding, ...]


def _read(ledger: LedgerReader) -> _View:
    """Collect threads and links across every package, de-duplicated by record id."""
    threads: dict[bytes, tuple[LogicalId, dict[RecordId, tuple[ContentId, ...]]]] = {}
    links: dict[RecordId, _Link] = {}
    findings: list[ConsolidationFinding] = []
    for ref in ledger.list_packages():
        for index, record in enumerate(ledger.read_records(ref.package_id, THREAD) or ()):
            try:
                rid, node, sources = (
                    _record_id(record),
                    _logical(record, "logical_id"),
                    _sources(record),
                )
            except _Malformed as exc:
                findings.append(_malformed(THREAD, ref.package_id, index, str(exc)))
                continue
            threads.setdefault(_key(node), (node, {}))[1][rid] = sources
        for kind in (IDENTITY_LINK, CONFIGURATION_LINEAGE, OPERATOR_ASSERTION):
            for index, record in enumerate(ledger.read_records(ref.package_id, kind) or ()):
                try:
                    link = _link(kind, record)
                except _Malformed as exc:
                    findings.append(_malformed(kind, ref.package_id, index, str(exc)))
                    continue
                if link is not None:
                    links[link.record] = link
    return _View(
        {k: (node, tuple(sorted(recs.items()))) for k, (node, recs) in sorted(threads.items())},
        tuple(links[k] for k in sorted(links)),
        tuple(findings),
    )


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


def nodes(ledger: LedgerReader) -> tuple[LogicalId, ...]:
    """One node per Ledger thread, in canonical logical-id order."""
    return tuple(node for node, _ in _read(ledger).threads.values())


class IdentityConsolidator:
    """Deterministic ``same_as`` / ``same_as_candidate`` claims. Takes no configuration."""

    consolidator_id: Final = "memory.identity"
    version: Final = "1"
    model: Final[ModelRef | None] = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[PriorClaim],
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
            draft, finding = self._same_as(view, link)
            if finding is not None:
                findings.append(finding)
            if draft is not None:
                drafts.append(draft)
                components.union(_key(link.left), _key(link.right))
        drafts.extend(self._candidates(view, components))
        return ConsolidatorOutput(tuple(drafts), tuple(findings))

    @staticmethod
    def _same_as(view: _View, link: _Link) -> tuple[ClaimDraft | None, ConsolidationFinding | None]:
        left, right = _key(link.left), _key(link.right)
        if left == right:
            return None, ConsolidationFinding(
                code="identity.self_link",
                severity=Severity.WARNING,
                message=f"{link.ground} record links a node to itself",
                records=(link.record,),
            )
        missing = [
            node
            for key, node in ((left, link.left), (right, link.right))
            if key not in view.threads
        ]
        if missing:
            return None, ConsolidationFinding(
                code="identity.dangling_link",
                severity=Severity.WARNING,
                message=f"{link.ground} record names a logical id with no Ledger thread",
                records=(link.record,),
                details={"missing": [node.to_json() for node in missing]},
            )
        subject, other = sorted((link.left, link.right), key=_key)
        draft = ClaimDraft(
            predicate=SAME_AS,
            subject=subject,
            object={"ground": link.ground, "node": other.to_json(), **link.detail},
            assertion_kind="stated" if link.ground == "operator_assertion" else "observed",
            inputs=(link.record,),
        )
        return draft, None

    @staticmethod
    def _candidates(view: _View, components: _Components) -> list[ClaimDraft]:
        """Per shared content id and subject: every other thread citing it, not already same_as."""
        citing: dict[ContentId, dict[bytes, list[RecordId]]] = {}
        for key, (_, records) in view.threads.items():
            for rid, sources in records:
                for source in sources:
                    citing.setdefault(source, {}).setdefault(key, []).append(rid)
        drafts: list[ClaimDraft] = []
        for source, by_node in sorted(citing.items()):
            for key in sorted(by_node):
                # Nodes already joined to the subject by same_as are not candidate readings of it.
                group = [
                    k
                    for k in sorted(by_node)
                    if k == key or components.find(k) != components.find(key)
                ]
                if len(group) < 2:
                    continue
                candidates: list[JsonValue] = [
                    {"evidence": sorted(by_node[k]), "node": view.threads[k][0].to_json()}
                    for k in group
                ]
                drafts.append(
                    ClaimDraft(
                        predicate=SAME_AS_CANDIDATE,
                        subject=view.threads[key][0],
                        object={
                            "basis": "shared_source",
                            "candidates": candidates,
                            "source": source,
                        },
                        assertion_kind="observed",
                        inputs=tuple(rid for k in group for rid in by_node[k]),
                        state=KnowledgeState.AMBIGUOUS,
                    )
                )
        return drafts
