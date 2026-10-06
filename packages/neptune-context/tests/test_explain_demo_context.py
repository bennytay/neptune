"""Demo v1 (ADR 0010): "why did the arm-cell incident happen, what changed" over the acceptance
corpus's Memory snapshot (the graph ``test_engine_demo_context.py`` answers queries over).

The arm ``ARM-3A`` in cell 3 at PLANT-2 ran configuration 1.4, then nothing stated, then 1.5,
and from 26 Sep 2026 no stated configuration at all; its incident is a later event on the cell
controller's clock. ``diff`` must surface that change from Memory's claims alone, ``why`` must
cite the incident's evidence, and the MCP tools must answer with the same packets.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from functools import cache
from typing import TYPE_CHECKING

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

import retrieve_fixtures_context as F
from neptune_context.answer import answer_problems
from neptune_context.engine import LocalEngine
from neptune_context.explain import IndexedReader, render_markdown
from neptune_context.mcp import build_server
from neptune_context.packets.codec import canonical_bytes
from neptune_context.packets.model import ClaimItem
from neptune_context.packets.trails import Change, DiffTrail, WhyTrail
from neptune_context.query import Budget, DomainClock, Instant, Query, Subject
from neptune_context.query.model import Diff, Why
from neptune_context.render.citations import render_text
from neptune_context.sdk import AsyncClient, Client

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim

    from neptune_context.packets.model import ContextPacket

ARM = Subject("machine", "asset-tag:ARM-3A", same_as_depth=1)
# The clock the cell's configuration records state their intervals on.
CONFIG_CLOCK = DomainClock(
    "rec:sha256:3c66443acb144517a1c805cdb5cd1649c33d69364a19e8cbfcc6ac87bf108cc2"
)
IN_1_4 = Instant(CONFIG_CLOCK, 1_772_200_000_000_000_000)  # cfg-c3-1.4 in force
IN_1_5 = Instant(CONFIG_CLOCK, 1_788_000_000_000_000_000)  # cfg-c3-1.5 in force
AFTER_1_5 = Instant(CONFIG_CLOCK, 1_789_100_000_000_000_000)  # 1.5 has ended


@cache
def reader() -> IndexedReader:
    return IndexedReader(F.demo_document())


def ask(query: Query) -> ContextPacket:
    packet = Client(LocalEngine(reader())).query(query)
    assert answer_problems(query, packet) == ()
    return packet


def what_changed(before: int | Instant, after: int | Instant) -> Query:
    return Query(
        include_inferred=True,
        budget=Budget(items=80),
        subjects=frozenset({ARM}),
        explain=(Diff(ARM, before, after),),
    )


def configuration(packet: ContextPacket, claim_id: str) -> str:
    claim: Claim = next(
        i.claim for i in packet.items if isinstance(i, ClaimItem) and i.claim.id == claim_id
    )
    return claim.object.node_id  # type: ignore[union-attr]


def changes(packet: ContextPacket) -> list[tuple[str, Change, tuple[str, ...]]]:
    (trail,) = packet.trails
    assert isinstance(trail, DiffTrail)
    return [
        (c.predicate, c.change, tuple(configuration(packet, i) for i in (*c.before, *c.after)))
        for c in trail.changes
        if c.predicate == "has_configuration"
    ]


def test_what_changed_before_the_incident_the_stated_configuration_ended() -> None:
    packet = ask(what_changed(IN_1_5, AFTER_1_5))
    assert changes(packet) == [("has_configuration", Change.CLOSED, ("cfg:cfg-c3-1.5",))]
    (trail,) = packet.trails
    assert isinstance(trail, DiffTrail)
    opened = {c.predicate for c in trail.changes if c.change is Change.OPENED}
    assert opened == {"configuration_unknown"}  # from then on no configuration is stated
    text = render_markdown(packet)
    assert "### configuration\\_unknown" in text and '"cfg:cfg-c3-1.5"' in text


def test_what_changed_between_the_two_configurations() -> None:
    packet = ask(what_changed(IN_1_4, IN_1_5))
    assert changes(packet) == [
        ("has_configuration", Change.CLOSED, ("cfg:cfg-c3-1.4",)),
        ("has_configuration", Change.OPENED, ("cfg:cfg-c3-1.5",)),
    ]
    # The incident, the arm's location and the camera mount sit on other clocks: named only.
    assert any(str(g.code) == "other_clock" and g.at == "/explain/0" for g in packet.gaps)


def test_what_memory_learned_about_the_arm_across_the_corpus() -> None:
    packet = ask(what_changed(1, int(reader().head)))
    (trail,) = packet.trails
    assert isinstance(trail, DiffTrail)
    opened = sorted({c.predicate for c in trail.changes if c.change is Change.OPENED})
    assert {"has_configuration", "involves", "recorded_by", "configuration_unknown"} <= set(opened)
    assert all(c.change is Change.OPENED for c in trail.changes)  # nothing was superseded
    found = {
        configuration(packet, i)
        for c in trail.changes
        if c.predicate == "has_configuration"
        for i in c.after
    }
    assert found == {"cfg:cfg-c3-1.4", "cfg:cfg-c3-1.5"}


def test_why_the_incident_involves_the_arm_cites_its_record() -> None:
    involves = next(
        c
        for c in F.demo_document().resolution.claims
        if c.predicate == "involves" and getattr(c.object, "node_id", "") == "asset-tag:ARM-3A"
    )
    query = Query(include_inferred=True, budget=Budget(items=20), explain=(Why(involves.id),))
    packet = ask(query)
    (trail,) = packet.trails
    assert isinstance(trail, WhyTrail) and trail.steps[0].claim == involves.id
    assert trail.steps[0].evidence == involves.provenance.evidence
    assert involves.id in packet.claim_ids
    text = render_markdown(packet)
    assert f"neptune://claim/{involves.id}?as_of=5" in text


def test_the_demo_answers_are_byte_identical_every_time() -> None:
    query = what_changed(IN_1_5, AFTER_1_5)
    first = ask(query)
    fresh = Client(LocalEngine(IndexedReader(F.demo_document()))).query(query)
    assert canonical_bytes(fresh) == canonical_bytes(first)
    assert render_markdown(fresh) == render_markdown(first)


def test_claude_code_gets_why_and_diff_over_mcp_unchanged() -> None:
    client = AsyncClient(LocalEngine(reader()))
    involves = next(
        c
        for c in F.demo_document().resolution.claims
        if c.predicate == "involves" and getattr(c.object, "node_id", "") == "asset-tag:ARM-3A"
    )
    subject = {"kind": "machine", "declared_id": "asset-tag:ARM-3A", "same_as_depth": 1}

    async def go() -> tuple[types.CallToolResult, types.CallToolResult]:
        async with create_connected_server_and_client_session(
            build_server(client), read_timeout_seconds=timedelta(seconds=20)
        ) as session:
            why = await session.call_tool(
                "neptune_why", {"claim_id": involves.id, "include_inferred": True}
            )
            diff = await session.call_tool(
                "neptune_diff",
                {
                    "subject": subject,
                    "before": 1,
                    "after": 5,
                    "include_inferred": True,
                    "max_items": 80,
                },
            )
            return why, diff

    why, diff = asyncio.run(go())
    assert not why.isError and not diff.isError
    expected = Client(LocalEngine(reader())).why(involves.id, include_inferred=True)
    first = why.content[0]
    assert isinstance(first, types.TextContent) and first.text == render_text(expected)
    assert expected.trails and involves.id in first.text
    text = diff.content[0]
    assert isinstance(text, types.TextContent) and "has_configuration" in text.text
