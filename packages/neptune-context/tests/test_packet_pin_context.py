"""The packet reader reads at Context's pin (ADR 0007 §6): newer upstream values are refused.

Memory's live codec accepts any value its own code knows, and Memory may be ahead of the
graph-schema version Context pinned. A packet carrying such a value must not decode as valid:
the pinned packet schema does not describe it.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from neptune_memory.schema.claim import Claim, TypedLiteral, ValueType
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import Resolution as History

import retrieve_fixtures_context as F
from neptune.model.units import unit_from_text
from neptune_context import pinned, pins
from neptune_context.engine import LocalEngine
from neptune_context.packets.codec import canonical_bytes, decode
from neptune_context.packets.findings import PacketFindingCode, PacketRefused
from neptune_context.packets.model import (
    BudgetUse,
    Channel,
    ChannelHit,
    ClaimItem,
    ContextPacket,
    GapCode,
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
    code, at, message = refused_at(packet_with(F.BEYOND_PIN))
    assert (code, at) == (PacketFindingCode.SHAPE, "/items/0/claim")
    assert f"'{F.BEYOND_PIN}'" in message
    assert f"graph-schema {pins.GRAPH_SCHEMA_VERSION}" in message


def _with(extra: Claim, subject: Subject) -> ContextPacket:
    claims = sorted([*F.claims(), extra], key=lambda c: (c.recorded_at, c.id))
    document = GraphDocument(
        History(tuple(claims), (F.finding(claims),)), F.RESOLVER_CONFIG, F.HEAD
    )
    query = Query(include_inferred=False, budget=Budget(items=50), subjects=frozenset({subject}))
    return LocalEngine(ReferenceReader(document)).query(query)


def test_a_claim_off_its_predicates_pinned_signature_is_a_gap_and_refused() -> None:
    # drift's only range is delta (graph-schema 1.7.0-2.0.0); calibrated_with takes sensors.
    sensor = F.node(NodeType.SENSOR, "asset-tag:LIDAR-7")
    quantity = TypedLiteral(ValueType.QUANTITY, 4.3, unit_from_text("mm"))
    off_range = F.claim(sensor, "drift", quantity, F.MAR_1)
    cfg = F.node(NodeType.CONFIGURATION, "cfg:lidar-7")
    off_domain = F.claim(F.AMR, "calibrated_with", cfg, F.MAR_1)
    cases = (
        (off_range, Subject("sensor", "asset-tag:LIDAR-7"), "'drift' with a 'quantity' object"),
        (off_domain, Subject("machine", str(F.AMR.node_id)), "'calibrated_with' on a 'machine'"),
    )
    for bad, subject, reason in cases:
        packet = _with(bad, subject)
        assert bad.id not in packet.claim_ids
        (gap,) = [g for g in packet.gaps if bad.id in g.refs]
        assert gap.code is GapCode.NOT_COVERED and reason in gap.detail
        assert f"pinned graph-schema {pins.GRAPH_SCHEMA_VERSION}" in gap.detail
        forged = dataclasses.replace(
            packet,
            items=(ClaimItem.of(bad, HIT),),
            findings=(),
            superseded_since=(),
            gaps=(),
            budget=BudgetUse.measured(packet.budget.limits, (ClaimItem.of(bad, HIT),)),
        )
        code, at, message = refused_at(forged)
        assert (code, at) == (PacketFindingCode.SHAPE, "/items/0/claim")
        assert reason in message


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
