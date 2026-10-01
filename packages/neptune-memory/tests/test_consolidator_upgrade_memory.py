"""Consolidator upgrade semantics (ADR 0003 §3): new transaction, defined order, retirement."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pytest

from neptune.identity.hashing import content_id
from neptune.identity.ids import record_id
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind, Known, NotApplicable
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
from neptune_memory.schema.supersede import (
    LineageError,
    Resolution,
    as_of,
    is_closure,
    lineage_of,
    resolve,
)

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
    dock: NodeRef = DOCK
    ticks: int = 10

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        kind = "inferred" if self.model else AssertionKind.OBSERVED
        return ConsolidatorOutput(
            (
                _draft("located_at", self.dock, self.ticks, kind),
                _draft("evidenced_by", LedgerRecordRef(RECORD), self.ticks, kind),
            )
        )


def _draft(
    predicate: str, obj: ClaimObject, ticks: int = 10, kind: object = AssertionKind.OBSERVED
) -> ClaimDraft:
    return ClaimDraft(
        subject=AMR,
        predicate=predicate,
        object=obj,
        confidence=Known(0.9) if kind == "inferred" else NotApplicable(),
        valid_from=Timestamp(ticks, CLOCK),
        assertion_kind=kind,  # type: ignore[arg-type]
        evidence=(EvidenceRef(content_id(b"bag"), (ByteRange(0, 8),)),),
        records=(RecordId(RECORD),),
    )


PRIORITIES = {"test.locator": 1, "test.other": 2}


def _build(
    version: str,
    tx: LedgerTx,
    consolidator_id: str = "test.locator",
    config: Mapping[str, JsonValue] | None = None,
    **fields: object,
) -> tuple[Claim, ...]:
    consolidator = Locator(version, consolidator_id, **fields)  # type: ignore[arg-type]
    (result,) = rebuild(LEDGER, [(consolidator, config or {})], recorded_at=tx)
    return result.claims


def _resolution(*builds: tuple[Claim, ...]) -> Resolution:
    return resolve([c for b in builds for c in b], CORE_PREDICATES, PRIORITIES)


def _history(*builds: tuple[Claim, ...]) -> tuple[Claim, ...]:
    return _resolution(*builds).claims


def test_upgrade_is_recorded_at_a_new_transaction_and_keeps_the_past() -> None:
    v1, v2 = _build("1", TX1), _build("2", TX2)
    resolution = _resolution(v1, v2)
    history = resolution.claims
    assert {c.id for c in history} == {c.id for c in v1} | {c.id for c in v2}  # nothing deleted
    assert {c.id for c in as_of(resolution, TX1).claims} == {c.id for c in v1}
    assert {c.id for c in as_of(resolution, TX2).claims} == {c.id for c in v2}


def test_upgrade_retires_the_old_lineage_including_many_predicates() -> None:
    v1, v2 = _build("1", TX1), _build("2", TX2)
    by_id = {c.id: c for c in _history(v1, v2)}
    assert all(by_id[c.id].superseded_at == TX2 for c in v1)
    assert all(by_id[c.id].superseded_at == OPEN for c in v2)
    assert all(by_id[c.id].valid_to == OPEN for c in v1)  # valid time is not cut


def test_upgrade_leaves_other_consolidators_alone() -> None:
    other = _build("1", TX1, "test.other")
    resolution = _resolution(_build("1", TX1), other, _build("2", TX2))
    current = {c.id for c in as_of(resolution, TX2).claims}
    assert {c.id for c in other} <= current


def test_two_versions_at_one_transaction_are_refused() -> None:
    with pytest.raises(LineageError, match="two lineages") as caught:
        _history(_build("1", TX1), _build("2", TX1))
    assert caught.value.code == "lineage_clash"


def test_a_lineage_is_never_reused_after_it_was_replaced() -> None:
    v1 = _build("1", TX1)
    with pytest.raises(LineageError, match="reappears") as caught:
        _history(v1, _build("2", TX2), _build("1", ledger_tx(3)))
    assert caught.value.code == "lineage_reuse"
    # The message names the whole lineage, not only the version string.
    consolidator, version, config = lineage_of(v1[0])
    assert all(part in str(caught.value) for part in (consolidator, repr(version), config))


@pytest.mark.parametrize("priorities", [{"test.locator": 2, "test.other": 1}, PRIORITIES])
def test_old_lineage_closure_made_at_the_upgrade_transaction_is_retired(
    priorities: Mapping[str, int],
) -> None:
    dock4 = NodeRef(NodeType.SITE, "site:dock-4")
    claims = [
        *_build("1", TX1, ticks=10),
        *_build("1", TX2, "test.other", dock=dock4, ticks=20),  # narrows x@v1 at TX2
        *_build("2", TX2, ticks=30),
    ]
    resolution = resolve(claims, CORE_PREDICATES, priorities)
    history = resolution.claims
    current = as_of(resolution, TX2).claims
    assert not [c for c in current if lineage_of(c)[:2] == ("test.locator", "1")]
    assert all(
        c.provenance.consolidator_version == "2"
        for c in current
        if c.provenance.consolidator_id == "test.locator"
    )
    narrowed = [c for c in history if is_closure(c) and c.object == DOCK]
    assert all(c.superseded_at == TX2 for c in narrowed)
    # The closure exists only when the lower-priority narrowing arrives before the upgrade.
    assert bool(narrowed) == (priorities["test.other"] < priorities["test.locator"])


def test_a_config_change_is_a_new_lineage_and_retires_the_old_one() -> None:
    old = _build("1", TX1, config={"window": 1})
    new = _build("1", TX2, config={"window": 2})
    current = {c.id for c in as_of(_resolution(old, new), TX2).claims}
    assert current == {c.id for c in new}


def test_a_model_swap_recorded_in_config_retires_the_old_model() -> None:
    m1, m2 = ModelRef("vlm-x", "1"), ModelRef("vlm-x", "2")
    old = _build("1", TX1, config={"model": m1.to_json()}, model=m1)
    new = _build("1", TX2, config={"model": m2.to_json()}, model=m2)
    assert old and new
    current = {c.id for c in as_of(_resolution(old, new), TX2).claims}
    assert current == {c.id for c in new}


def test_re_running_the_same_version_retires_nothing() -> None:
    history = _history(_build("1", TX1), _build("1", TX2))
    assert all(c.superseded_at == OPEN for c in history)
    assert {c.recorded_at for c in history} == {TX1}  # same ids: first recording kept


def test_upgrade_resolution_is_order_free_and_idempotent() -> None:
    claims = [*_build("1", TX1), *_build("2", TX2), *_build("3", ledger_tx(3))]
    forward = resolve(claims, CORE_PREDICATES, PRIORITIES)
    backward = resolve(list(reversed(claims)), CORE_PREDICATES, PRIORITIES)
    assert forward == backward
    assert resolve(forward.claims, CORE_PREDICATES, PRIORITIES) == forward
    assert len(as_of(forward, ledger_tx(3)).claims) == 2
