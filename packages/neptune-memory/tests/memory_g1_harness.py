"""Shared harness for the G1 gate stress tests (MVL-106, ``docs/reviews/g1-stress-test.md``).

Every scenario runs through the real code: Ledger records in a ``StubLedger``, the real
consolidator runner and identity policy, the real resolver, and the reference ``MemoryReader``
over a graph document. Times are POSIX seconds on ``CivilClock(posix, unix, 1 s)`` (declared
instants; the dates in comments are UTC labels for the reader) or a robot's own clock domain.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Final

from neptune.identity.hashing import content_id
from neptune.identity.ids import record_id
from neptune.model.ids import ContentId, LogicalId, RecordId
from neptune.model.knowledge import AssertionKind, Known, NotApplicable
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.time import Epoch, Timescale, Timestamp
from neptune_memory.consolidate.base import ClaimDraft, ConsolidatorOutput, ModelRef
from neptune_memory.ledger import StubLedger
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.interval import CivilClock, ledger_tx
from neptune_memory.schema.predicates import CORE_PREDICATES, PredicateRegistry
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import resolve, resolver_config

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.consolidate.base import Consolidation
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim, ClaimAssertionKind, ClaimObject
    from neptune_memory.schema.nodes import NodeRef

Record = dict[str, object]
# A consolidator run (ADR 0007 §5.1): consolidator id, version, config hash, recorded_at.
Build = dict[str, object]

SECONDS: Final = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(1))
DAY: Final = 86_400
# Declared POSIX instants (UTC labels for the reader only).
JAN_10_2025: Final = 1736467200
JUN_01_2025: Final = 1748736000
MAR_02_2026: Final = 1772409600
JUN_10_2026: Final = 1781049600


def civil(ticks: int) -> Timestamp:
    return SECONDS.at(ticks)


def own_clock(name: str) -> RecordId:
    """A robot's own clock domain (boot clock, an unset RTC): never comparable to civil time."""
    return record_id("timestamp_domain", {"g1": name})


def rid(kind: str, name: str) -> RecordId:
    return record_id(kind, {"g1": name})


def source(name: str) -> ContentId:
    """The content id of a source's bytes: equal bytes, equal id, wherever they are stored."""
    return content_id(name.encode())


def cite(src: ContentId, length: int = 4096) -> EvidenceRef:
    return EvidenceRef(src, (ByteRange(0, length),))


def thread(
    namespace: str,
    value: str,
    node_type: str,
    *evidence: EvidenceRef,
    valid_from: Timestamp | None = None,
    record: str | None = None,
) -> Record:
    """A ``ledger_thread`` record as ADR 0003 §1 consumes it."""
    logical = LogicalId(namespace, value)
    return {
        "kind": "ledger_thread",
        "id": rid("ledger_thread", record or f"{namespace}:{value}"),
        "logical_id": logical.to_json(),
        "node_type": node_type,
        "valid_from": (valid_from or civil(JAN_10_2025)).to_json(),
        "evidence": [e.to_json() for e in evidence],
    }


def link(
    kind: str, name: str, left: LogicalId, right: LogicalId, when: int, **extra: object
) -> Record:
    """A ``configuration_lineage`` stand-in (ADR 0003 §1); identity links and assertions are the
    compiler's kinds (``memory_identity_records``)."""
    sides = {"configuration_lineage": ("predecessor", "successor")}[kind]
    return {
        "kind": kind,
        "id": rid(kind, name),
        sides[0]: left.to_json(),
        sides[1]: right.to_json(),
        "valid_from": civil(when).to_json(),
        "evidence": [cite(source(name)).to_json()],
        **extra,
    }


def ledger(packages: Mapping[str, Sequence[Record]]) -> StubLedger:
    return StubLedger({pid: (1, list(records)) for pid, records in packages.items()})


def draft(
    subject: NodeRef,
    predicate: str,
    obj: ClaimObject,
    start: Timestamp,
    *,
    records: Sequence[RecordId],
    evidence: Sequence[EvidenceRef],
    kind: ClaimAssertionKind = AssertionKind.OBSERVED,
    end: Timestamp | None = None,
) -> ClaimDraft:
    inferred = not isinstance(kind, AssertionKind)
    return ClaimDraft(
        subject=subject,
        predicate=predicate,
        object=obj,
        valid_from=start,
        assertion_kind=kind,
        evidence=tuple(evidence),
        records=tuple(records),
        confidence=Known(0.7) if inferred else NotApplicable(),
        **({} if end is None else {"valid_to": end}),
    )


@dataclass(frozen=True)
class Fixed:
    """A deterministic consolidator whose drafts are given: stands in for a G2 consolidator."""

    consolidator_id: str
    drafts: tuple[ClaimDraft, ...]
    version: str = "1"
    model: ModelRef | None = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        return ConsolidatorOutput(self.drafts)


def build(consolidation: Consolidation, recorded_at: int) -> Build:
    """One consolidator run as ADR 0007 §5.1 records it: its lineage and transaction."""
    transform = consolidation.transform
    return {
        "consolidator_id": transform.consolidator_id,
        "config_hash": transform.config_hash,
        "recorded_at": recorded_at,
        "version": transform.version,
    }


def accepts_builds() -> bool:
    """Whether ``resolve`` takes ADR 0007 §5.5's ``builds`` yet (MVL-132)."""
    return "builds" in inspect.signature(resolve).parameters


def reader(
    claims: Sequence[Claim],
    priorities: Mapping[str, int],
    *,
    registry: PredicateRegistry = CORE_PREDICATES,
    head: int | None = None,
    builds: Sequence[Build] = (),
) -> ReferenceReader:
    """Resolve ``claims`` and wrap the history in the reference reader.

    ``builds`` go to ``resolve`` as soon as it accepts them, so the strict ``xfail`` tests that pass
    them flip on their own when MVL-132 lands withdrawal; until then they are ignored.
    """
    if builds and accepts_builds():
        resolution = resolve(claims, registry, priorities, builds=builds)  # type: ignore[call-arg]
    else:
        resolution = resolve(claims, registry, priorities)
    top = max((c.recorded_at for c in claims), default=0)
    document = GraphDocument(
        resolution,
        resolver_config(registry, priorities),
        ledger_tx(head if head is not None else top),
    )
    return ReferenceReader(document)
