"""Compiler-shaped identity records for tests, built with the compiler's own types.

Every ``identity_link``, ``assertion`` and ``timestamp_domain`` here is constructed as the compiler
model class and serialised with its ``to_json``, so a test can never feed the identity consolidator
a shape the compiler would not write (root ADRs 0050 and 0062). Ids are the compiler's evidence
record ids under one test transform. ``ledger_thread`` and ``configuration_lineage`` are the
Ledger stand-ins (ADR 0003 §1).
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Final, TypeVar

from neptune.identity.hashing import content_id
from neptune.identity.ids import record_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.alignment import IdentityLink, LinkBasis, ValidityWindow
from neptune.model.assertion import Assertion, AssertionType, ScopeRef
from neptune.model.ids import ContentId, LogicalId, RecordId
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance
from neptune.model.reference import TimestampDomain
from neptune.model.time import Epoch, Timescale, Timestamp
from neptune_memory.ledger import StubLedger
from neptune_memory.schema.nodes import NodeType

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

Record = dict[str, object]
_V = TypeVar("_V")
TRANSFORM: Final = transform_record(adapter_id="test.identity", adapter_version="1", config={})
# A robot's own clock: never comparable with civil time.
CLOCK: Final = record_id("test.clock", {"name": "site-utc"})
OBSERVED, STATED = AssertionKind.OBSERVED, AssertionKind.STATED


def source(name: str) -> ContentId:
    return content_id(name.encode())


def cite(src: ContentId | str, offset: int = 0, length: int = 64) -> EvidenceRef:
    src = source(src) if not src.startswith("sha256:") else ContentId(src)
    return EvidenceRef(src, (ByteRange(offset, length),))


def provenance(evidence: EvidenceRef, kind: AssertionKind = OBSERVED) -> Provenance:
    return Provenance(evidence, TRANSFORM.id, kind)


def rid(kind: str, evidence: EvidenceRef) -> RecordId:
    return evidence_record_id(kind, evidence, TRANSFORM)


def at(ticks: int, clock: RecordId = CLOCK) -> Timestamp:
    return Timestamp(ticks, clock)


def thread(
    node: LogicalId,
    *sources: str | ContentId,
    node_type: NodeType = NodeType.MACHINE,
    start: Timestamp | None = None,
    name: str | None = None,
) -> Record:
    """A ``ledger_thread`` stand-in (ADR 0003 §1); ``name`` makes a second record of one thread."""
    return {
        "kind": "ledger_thread",
        "id": record_id("ledger_thread", {"n": [node.namespace, node.value, name or ""]}),
        "logical_id": node.to_json(),
        "node_type": str(node_type),
        "valid_from": (start or at(100)).to_json(),
        "evidence": [cite(s).to_json() for s in sources],
    }


def lineage(
    name: str, before: LogicalId, after: LogicalId, start: Timestamp | None = None
) -> Record:
    """A ``configuration_lineage`` stand-in: ``after`` continues ``before``."""
    return {
        "kind": "configuration_lineage",
        "id": record_id("configuration_lineage", {"n": name}),
        "predecessor": before.to_json(),
        "successor": after.to_json(),
        "valid_from": (start or at(200)).to_json(),
        "evidence": [cite(name).to_json()],
    }


def window(
    start: Timestamp | None, end: Timestamp | None = None, clock: RecordId = CLOCK
) -> Knowledge[ValidityWindow]:
    def bound(stamp: Timestamp | None) -> Knowledge[Timestamp]:
        return Known(stamp) if stamp is not None else Unknown()

    return Known(ValidityWindow(clock, bound(start), bound(end)))


def ambiguous(name: str, *values: _V) -> Ambiguous[_V]:
    """``Ambiguous`` over ``values``, each candidate citing its own place in declaration
    ``name``."""
    return Ambiguous(
        tuple(
            Candidate(v, provenance(cite(name, 256 + 8 * i, 8), STATED))
            for i, v in enumerate(values)
        )
    )


def link(
    name: str,
    left: LogicalId,
    right: LogicalId | Sequence[LogicalId],
    *,
    identifier: LogicalId | Sequence[LogicalId] | None = None,
    validity: Knowledge[ValidityWindow] | None = None,
    kind: AssertionKind = STATED,
) -> Record:
    """The compiler's ``IdentityLink`` from one declaration ``name``.

    ``right`` as a sequence is ``Ambiguous`` with each candidate citing its own cell; without
    ``identifier`` the link is ``co_declared`` (one declaration names both ids), with one it is
    ``shared_identifier`` and cites a second declaration, ``name + " (other)"``.
    """
    declared = provenance(cite(name), kind)
    if isinstance(right, LogicalId):
        right_k: Knowledge[LogicalId] = Known(right, provenance(cite(name, 64, 8), kind))
    else:
        right_k = Ambiguous(
            tuple(
                Candidate(c, provenance(cite(name, 64 + 8 * i, 8), kind))
                for i, c in enumerate(right)
            )
        )
    ident: Knowledge[LogicalId]
    others: tuple[EvidenceRef, ...]
    if identifier is None:
        basis, ident, others = LinkBasis.CO_DECLARED, NotApplicable(), ()
    else:
        basis, others = LinkBasis.SHARED_IDENTIFIER, (cite(f"{name} (other)"),)
        ident = (
            Known(identifier)
            if isinstance(identifier, LogicalId)
            else Ambiguous(tuple(Candidate(i) for i in identifier))
        )
    return IdentityLink(
        id=rid("identity_link", declared.evidence),
        provenance=declared,
        left=left,
        right=right_k,
        basis=basis,
        identifier=ident,
        evidence=others,
        validity=validity if validity is not None else NotCovered(),
    ).to_json()  # type: ignore[return-value]


def assertion(
    name: str,
    assertion_type: AssertionType | None,
    scope: Sequence[ScopeRef] | None,
    *,
    identifier: LogicalId | Ambiguous[LogicalId] | None = None,
    retracts: LogicalId | Ambiguous[LogicalId] | None = None,
    authored_at: Timestamp | Knowledge[Timestamp] | None = None,
) -> Record:
    """A person's ``Assertion`` (root ADR 0062) entered as ``name`` in an assertions file.

    ``identifier`` defaults to ``ops-console:<name>``; ``None`` for the type or scope is
    ``Unknown`` (the adapter could not read it); ``authored_at`` may be any ``Knowledge``, and
    ``identifier`` and ``retracts`` ``Ambiguous``.
    """
    declared = provenance(cite(f"assertions/{name}"), STATED)
    absent = KnownAbsent(declared)
    if assertion_type is AssertionType.RETRACT:
        retracts_k: Knowledge[LogicalId] = (
            retracts
            if isinstance(retracts, Ambiguous)
            else Known(retracts)
            if retracts
            else Unknown()
        )
    elif assertion_type is None:
        retracts_k = Unknown()
    else:
        retracts_k = NotApplicable()
    return Assertion(
        id=rid("assertion", declared.evidence),
        provenance=declared,
        identifier=(
            identifier
            if isinstance(identifier, Ambiguous)
            else Known(identifier or LogicalId("ops-console", name))
        ),
        assertion_type=Known(assertion_type) if assertion_type is not None else Unknown(),
        author=Known(LogicalId("staff", "ana")),
        authored_at=(
            Known(authored_at)
            if isinstance(authored_at, Timestamp)
            else authored_at
            if authored_at is not None
            else Unknown()
        ),
        authored_zone=Unknown(),
        scope=Known(tuple(scope)) if scope is not None else Unknown(),
        retracts=retracts_k,
        payload=absent,
        rationale=absent,
        signature=absent,
        ticket=absent,
    ).to_json()  # type: ignore[return-value]


def civil_domain(name: str, resolution: Fraction = Fraction(1)) -> tuple[Record, RecordId]:
    """A ``timestamp_domain`` declaring POSIX seconds since the Unix epoch, and its id."""
    declared = provenance(cite(f"clock {name}"))
    domain = TimestampDomain(
        id=rid("timestamp_domain", declared.evidence),
        provenance=declared,
        field="authored_at",
        scope=(),
        role=Unknown(),
        resolution=Known(resolution),
        epoch=Known(Epoch.UNIX),
        timescale=Known(Timescale.POSIX),
        declared_monotonic=Unknown(),
    )
    return domain.to_json(), domain.id  # type: ignore[return-value]


def ledger(packages: Mapping[str, Sequence[Record]]) -> StubLedger:
    return StubLedger({pid: (1, list(records)) for pid, records in packages.items()})
