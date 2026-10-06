"""The packet reader reads at Context's pin (ADR 0007 §6): newer upstream values are refused.

Memory's live codec accepts any value its own code knows, and Memory may be ahead of the
graph-schema version Context pinned. A packet carrying such a value must not decode as valid:
the pinned packet schema does not describe it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import retrieve_fixtures_context as F
from neptune_context import pinned
from neptune_context.engine import LocalEngine
from neptune_context.packets.codec import canonical_bytes, decode
from neptune_context.packets.findings import PacketFindingCode, PacketRefused
from neptune_context.packets.model import (
    BudgetUse,
    Channel,
    ChannelHit,
    ClaimItem,
    ContextPacket,
    Relevance,
)
from neptune_context.query import Budget, Query, Subject

if TYPE_CHECKING:
    import pytest

HIT = Relevance(1.0, (ChannelHit(Channel.GRAPH, 1, 1.0),))


def packet_with(predicate: str) -> ContextPacket:
    base = LocalEngine(F.reader()).query(
        Query(
            include_inferred=False,
            budget=Budget(items=50),
            subjects=frozenset({Subject("machine", "asset-tag:AMR-07")}),
        )
    )
    (claim,) = [c for c in F.claims() if c.predicate == predicate and c.is_current][:1]
    items = (ClaimItem.of(claim, HIT),)
    return ContextPacket(
        query_id=base.query_id,
        as_of=base.as_of,
        head=base.head,
        during=None,
        memory=base.memory,
        ledger=base.ledger,
        produced_by=base.produced_by,
        inference_included=False,
        budget=BudgetUse.measured(base.budget.limits, items),
        items=items,
    )


def refused_at(packet: ContextPacket) -> tuple[PacketFindingCode, str, str]:
    result = decode(canonical_bytes(packet))
    assert isinstance(result, PacketRefused)
    (finding,) = result.findings
    return finding.code, finding.at, finding.message


def test_a_claim_with_a_predicate_beyond_the_pin_is_refused() -> None:
    code, at, message = refused_at(packet_with("drift"))
    assert (code, at) == (PacketFindingCode.SHAPE, "/items/0/claim")
    assert "'drift'" in message and "graph-schema 1.6.0" in message


def test_a_node_type_beyond_the_pin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    packet = packet_with("zone_of")
    assert not isinstance(decode(canonical_bytes(packet)), PacketRefused)
    monkeypatch.setattr(pinned, "node_types", lambda: frozenset({"site", "machine"}))
    code, at, message = refused_at(packet)
    assert (code, at) == (PacketFindingCode.SHAPE, "/items/0/claim")
    assert "node type 'zone'" in message


def test_a_value_type_and_a_finding_code_beyond_the_pin_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = packet_with("has_name")
    monkeypatch.setattr(pinned, "value_types", lambda: frozenset({"integer"}))
    assert "value type 'text'" in refused_at(packet)[2]
    monkeypatch.undo()
    with_finding = LocalEngine(F.reader()).query(
        Query(
            include_inferred=False,
            budget=Budget(items=50),
            subjects=frozenset({Subject("machine", "fleet-id:amr-7")}),
        )
    )
    assert with_finding.findings
    monkeypatch.setattr(pinned, "finding_codes", lambda: frozenset({"overridden_on_arrival"}))
    code, at, message = refused_at(with_finding)
    assert (code, at) == (PacketFindingCode.SHAPE, "/findings/0")
    assert "finding code 'clock_mismatch'" in message
