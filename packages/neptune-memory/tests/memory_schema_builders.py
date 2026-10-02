"""Small builders for claims in tests. Real compiler types throughout; no mocks."""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING

from neptune.identity.hashing import content_id
from neptune.identity.ids import config_hash, record_id
from neptune.model.knowledge import AssertionKind, Knowledge, Known, NotApplicable
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.time import Epoch, Timescale, Timestamp
from neptune_memory.schema.claim import (
    Claim,
    ClaimAssertionKind,
    ClaimObject,
    ClaimProvenance,
    ModelRef,
    is_inferred,
)
from neptune_memory.schema.interval import OPEN, CivilClock, Open, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from neptune.model.ids import RecordId

OBSERVED = AssertionKind.OBSERVED
STATED = AssertionKind.STATED
INFERRED: ClaimAssertionKind = "inferred"

# Civil seconds (POSIX, Unix epoch) and civil days: shared timelines, comparable across sources.
SECONDS = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(1))
DAYS = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(86400))
# A robot's boot clock, as a compiler TimestampDomain id would name it: not comparable to civil.
BOOT_CLOCK: RecordId = record_id("timestamp_domain", {"test": "boot clock of amr-12"})

CONFIG = config_hash({"test": True})
# The model every inferred test claim names in its provenance (ADR 0006 §3).
MODEL = ModelRef("test-model", "1")


def evidence(n: int) -> EvidenceRef:
    return EvidenceRef(content_id(f"source {n}".encode()), (ByteRange(0, n + 1),))


def provenance(
    n: int = 0,
    consolidator: str = "memory.test",
    records: tuple[RecordId, ...] = (),
    model: ModelRef | None = None,
) -> ClaimProvenance:
    return ClaimProvenance((evidence(n),), records, consolidator, "1", CONFIG, model)


def at(ticks: int, clock: CivilClock | RecordId = SECONDS) -> Timestamp:
    return Timestamp(ticks, clock if isinstance(clock, str) else clock.domain_id)


def node(node_type: NodeType, node_id: str) -> NodeRef:
    return NodeRef(node_type, node_id)


def claim(
    subject: NodeRef,
    predicate: str,
    obj: ClaimObject,
    start: int,
    end: int | Open = OPEN,
    *,
    tx: int,
    kind: ClaimAssertionKind = STATED,
    confidence: Knowledge[float] | None = None,
    consolidator: str = "memory.test",
    ev: int = 0,
    clock: CivilClock | RecordId = SECONDS,
) -> Claim:
    if confidence is None:
        confidence = Known(0.8) if is_inferred(kind) else NotApplicable()
    return Claim(
        subject=subject,
        predicate=predicate,
        object=obj,
        valid_from=at(start, clock),
        valid_to=end if isinstance(end, Open) else at(end, clock),
        recorded_at=ledger_tx(tx),
        assertion_kind=kind,
        confidence=confidence,
        provenance=provenance(ev, consolidator, model=MODEL if is_inferred(kind) else None),
    )
