"""Consolidator upgrade semantics (ADR 0003 §3): new transaction, defined order, retirement."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pytest

from neptune.identity.hashing import content_id
from neptune.identity.ids import record_id
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.time import Timestamp
from neptune_memory.consolidate.base import (
    ClaimDraft,
    ConsolidatorOutput,
    ModelRef,
    rebuild,
)
from neptune_memory.ledger import LedgerReader, StubLedger
from neptune_memory.schema.claim import Claim, ClaimObject, LedgerRecordRef
from neptune_memory.schema.interval import OPEN, LedgerTx, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES
from neptune_memory.schema.supersede import as_of, resolve

CLOCK = record_id("test.clock", {"name": "site"})
AMR = NodeRef(NodeType.MACHINE, "serial:AMR-12")
DOCK = NodeRef(NodeType.SITE, "site:dock-3")
RECORD = record_id("test.record", {"n": 1})
LEDGER = StubLedger({"pkg": (1, [{"kind": "observation", "id": RECORD}])})
TX1, TX2 = ledger_tx(1), ledger_tx(2)


@dataclass(frozen=True)
class Locator:
    """Re-consolidates one record: a ``one`` and a ``many`` claim, equal across versions."""

    version: str = "1"
    consolidator_id: str = "test.locator"
    model: ModelRef | None = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        return ConsolidatorOutput(
            (_draft("located_at", DOCK), _draft("evidenced_by", LedgerRecordRef(RECORD)))
        )


def _draft(predicate: str, obj: ClaimObject) -> ClaimDraft:
    return ClaimDraft(
        subject=AMR,
        predicate=predicate,
        object=obj,
        valid_from=Timestamp(10, CLOCK),
        assertion_kind=AssertionKind.OBSERVED,
        evidence=(EvidenceRef(content_id(b"bag"), (ByteRange(0, 8),)),),
        records=(RecordId(RECORD),),
    )


PRIORITIES = {"test.locator": 1, "test.other": 2}


def _build(version: str, tx: LedgerTx, consolidator_id: str = "test.locator") -> tuple[Claim, ...]:
    (result,) = rebuild(LEDGER, [(Locator(version, consolidator_id), {})], recorded_at=tx)
    return result.claims


def _history(*builds: tuple[Claim, ...]) -> tuple[Claim, ...]:
    return resolve([c for b in builds for c in b], CORE_PREDICATES, PRIORITIES).claims


def test_upgrade_is_recorded_at_a_new_transaction_and_keeps_the_past() -> None:
    v1, v2 = _build("1", TX1), _build("2", TX2)
    history = _history(v1, v2)
    assert {c.id for c in history} == {c.id for c in v1} | {c.id for c in v2}  # nothing deleted
    assert {c.id for c in as_of(history, TX1)} == {c.id for c in v1}
    assert {c.id for c in as_of(history, TX2)} == {c.id for c in v2}


def test_upgrade_retires_the_old_lineage_including_many_predicates() -> None:
    v1, v2 = _build("1", TX1), _build("2", TX2)
    by_id = {c.id: c for c in _history(v1, v2)}
    assert all(by_id[c.id].superseded_at == TX2 for c in v1)
    assert all(by_id[c.id].superseded_at == OPEN for c in v2)
    assert all(by_id[c.id].valid_to == OPEN for c in v1)  # valid time is not cut


def test_upgrade_leaves_other_consolidators_alone() -> None:
    other = _build("1", TX1, "test.other")
    history = _history(_build("1", TX1), other, _build("2", TX2))
    current = {c.id for c in as_of(history, TX2)}
    assert {c.id for c in other} <= current


def test_two_versions_at_one_transaction_are_refused() -> None:
    with pytest.raises(ValueError, match="two versions"):
        _history(_build("1", TX1), _build("2", TX1))


def test_re_running_the_same_version_retires_nothing() -> None:
    history = _history(_build("1", TX1), _build("1", TX2))
    assert all(c.superseded_at == OPEN for c in history)
    assert {c.recorded_at for c in history} == {TX1}  # same ids: first recording kept


def test_upgrade_resolution_is_order_free_and_idempotent() -> None:
    claims = [*_build("1", TX1), *_build("2", TX2), *_build("3", ledger_tx(3))]
    forward = resolve(claims, CORE_PREDICATES, PRIORITIES).claims
    backward = resolve(list(reversed(claims)), CORE_PREDICATES, PRIORITIES).claims
    assert forward == backward
    assert resolve(forward, CORE_PREDICATES, PRIORITIES).claims == forward
    assert len(as_of(forward, ledger_tx(3))) == 2
