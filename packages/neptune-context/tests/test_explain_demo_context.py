"""Demo v1 (ADR 0010): "why did the arm-cell incident happen, what changed" over the acceptance
corpus's Memory snapshot (the graph ``test_engine_demo_context.py`` answers queries over).

Memory's pipeline-built snapshot states the arm ``ARM-3A``'s configuration under ServiceNow's
name for it: firmware 5.6.0 from March 2026, replaced from tick 1787057400 on ServiceNow's clock by
the tool-centre-point change ``TCP z=145.5 mm``. Its incident is an event node whose claims carry
the record's evidence; Memory states no link from the incident to the arm, and Context adds none.
``diff`` must surface the configuration change from Memory's claims alone, ``why`` must cite the
incident's evidence, and the MCP tools must answer with the same packets.
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
from neptune_context.render.agent import render_answer
from neptune_context.sdk import AsyncClient, Client

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim

    from neptune_context.packets.model import ContextPacket

ARM = Subject("machine", "servicenow.ci:ARM-3A")
# The clock ServiceNow's change records state their intervals on.
CONFIG_CLOCK = DomainClock(
    "rec:sha256:659e221e993c637fb9dc0a93a1cf07880be2dac2688971d18afab81ba82b78d4"
)
BEFORE_5_6_0 = Instant(CONFIG_CLOCK, 1_770_000_000)  # firmware 5.6.0 not yet in force
IN_5_6_0 = Instant(CONFIG_CLOCK, 1_780_000_000)  # firmware 5.6.0 in force
AFTER_TCP_CHANGE = Instant(CONFIG_CLOCK, 1_790_000_000)  # the TCP change in force
HEAD = 2


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


def incident_description() -> Claim:
    """The incident's own description claim: what Memory states about the event, with its record."""
    return next(
        c
        for c in F.demo_document().resolution.claims
        if c.predicate == "has_description" and c.subject.node_type == "event"
    )


def test_what_changed_across_the_tcp_change_the_firmware_configuration_is_superseded() -> None:
    packet = ask(what_changed(IN_5_6_0, AFTER_TCP_CHANGE))
    assert changes(packet) == [
        (
            "has_configuration",
            Change.SUPERSEDED,
            ("servicenow.u_after:5.6.0", "servicenow.u_after:TCP z=145.5 mm"),
        )
    ]
    text = render_markdown(packet)
    assert "**superseded**" in text and "replaced by" in text and "TCP z=145.5 mm" in text


def test_what_changed_before_the_firmware_the_first_configuration_opens() -> None:
    packet = ask(what_changed(BEFORE_5_6_0, IN_5_6_0))
    assert changes(packet) == [("has_configuration", Change.OPENED, ("servicenow.u_after:5.6.0",))]


def test_what_memory_learned_about_the_arm_across_the_corpus() -> None:
    packet = ask(what_changed(1, HEAD))
    (trail,) = packet.trails
    assert isinstance(trail, DiffTrail)
    assert all(c.change is Change.OPENED for c in trail.changes)  # nothing was superseded
    found = {
        configuration(packet, i)
        for c in trail.changes
        if c.predicate == "has_configuration"
        for i in c.after
    }
    assert found == {"servicenow.u_after:5.6.0", "servicenow.u_after:TCP z=145.5 mm"}


def test_why_the_incident_is_described_cites_its_record() -> None:
    described = incident_description()
    query = Query(include_inferred=True, budget=Budget(items=20), explain=(Why(described.id),))
    packet = ask(query)
    (trail,) = packet.trails
    assert isinstance(trail, WhyTrail) and trail.steps[0].claim == described.id
    assert trail.steps[0].evidence == described.provenance.evidence
    assert described.provenance.evidence
    assert described.id in packet.claim_ids
    text = render_markdown(packet)
    assert f"neptune://claim/{described.id}?as_of={HEAD}" in text


def test_the_demo_answers_are_byte_identical_every_time() -> None:
    query = what_changed(IN_5_6_0, AFTER_TCP_CHANGE)
    first = ask(query)
    fresh = Client(LocalEngine(IndexedReader(F.demo_document()))).query(query)
    assert canonical_bytes(fresh) == canonical_bytes(first)
    assert render_markdown(fresh) == render_markdown(first)


def test_claude_code_gets_why_and_diff_over_mcp_unchanged() -> None:
    client = AsyncClient(LocalEngine(reader()))
    described = incident_description()
    subject = {"kind": "machine", "declared_id": "servicenow.ci:ARM-3A"}

    async def go() -> tuple[types.CallToolResult, types.CallToolResult]:
        async with create_connected_server_and_client_session(
            build_server(client), read_timeout_seconds=timedelta(seconds=20)
        ) as session:
            why = await session.call_tool(
                "neptune_why", {"claim_id": described.id, "include_inferred": True}
            )
            diff = await session.call_tool(
                "neptune_diff",
                {
                    "subject": subject,
                    "before": 1,
                    "after": HEAD,
                    "include_inferred": True,
                    "max_items": 80,
                },
            )
            return why, diff

    why, diff = asyncio.run(go())
    assert not why.isError and not diff.isError
    expected = Client(LocalEngine(reader())).why(described.id, include_inferred=True)
    first = why.content[0]
    assert isinstance(first, types.TextContent) and first.text == render_answer(expected)
    assert expected.trails and described.id in first.text
    text = diff.content[0]
    assert isinstance(text, types.TextContent) and "has_configuration" in text.text
