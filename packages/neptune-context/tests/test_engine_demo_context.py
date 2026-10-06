"""Demo v1, end to end (ADR 0007 §5): the local engine over the acceptance corpus's Memory graph.

The graph is Memory's pipeline-built snapshot of the two-site corpus (``harness/acceptance``:
PLANT-2's arm cell and legged robot, S-007's lift AMRs), frozen under ``golden/`` and read through
Memory's reference reader. The agent's question, "why did the arm-cell incident happen, what
changed", is asked as a typed query, through the SDK and through the MCP server (in process and as
a real stdio subprocess), and must come back cited, scoped and byte-identical every time.

What the snapshot states, and what it does not: the arm ``ARM-3A`` is named by three source
systems (``servicenow.ci:``, ``cmms.asset:``, ``manifest:``) with no identity link between them,
its configuration changes are ServiceNow's and the CMMS's, and the incident is an event node whose
claims name its kind, severity and description, with no link to the arm. Context carries all of
that as stated and adds none of the missing links.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from functools import cache
from typing import TYPE_CHECKING

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from neptune_memory.schema.reference import ReferenceReader

import explain_fixtures_context as X
import retrieve_fixtures_context as F
from neptune_context import pinned
from neptune_context.answer import answer_problems
from neptune_context.engine import LocalEngine, read_graph
from neptune_context.explain import render_markdown
from neptune_context.mcp import build_server
from neptune_context.mcp.__main__ import main
from neptune_context.packets.codec import canonical_bytes
from neptune_context.packets.model import ClaimItem
from neptune_context.query import Budget, Direction, GraphClause, Query, Subject, to_json
from neptune_context.render.agent import render_answer
from neptune_context.render.citations import parse_citations, render_text
from neptune_context.sdk import AsyncClient, Client

if TYPE_CHECKING:
    from pathlib import Path

    from neptune_context.packets.model import ContextPacket

ARM_NAMES = frozenset(Subject(*n) for n in F.DEMO_ARM_NAMES)
ARM_NAMES_IDS = {s.declared_id for s in ARM_NAMES}
INCIDENT = Subject(*F.DEMO_INCIDENT)
WHY_THE_ARM_CELL = Query(
    include_inferred=True,
    budget=Budget(items=60, tokens=24_000),
    subjects=ARM_NAMES | {INCIDENT},
    graph=GraphClause(None, 2, Direction.BOTH),
)


@cache
def demo_reader() -> ReferenceReader:
    return ReferenceReader(F.demo_document())


def ask(query: Query) -> ContextPacket:
    return Client(LocalEngine(demo_reader())).query(query)


def facts(packet: ContextPacket) -> set[tuple[str, str]]:
    return {
        (i.claim.predicate, getattr(i.claim.object, "node_id", ""))
        for i in packet.items
        if isinstance(i, ClaimItem)
    }


def test_why_did_the_arm_cell_incident_happen_and_what_changed() -> None:
    packet = ask(WHY_THE_ARM_CELL)
    found = facts(packet)
    # What changed on the arm: ServiceNow's firmware and tool-centre-point records and the CMMS's.
    assert ("has_configuration", "servicenow.u_after:5.6.0") in found
    assert ("has_configuration", "servicenow.u_after:TCP z=145.5 mm") in found
    assert ("has_configuration", "firmware:5.6.0") in found
    assert ("recorded_by", "manifest:ARM-3A") in found  # the arm's runs
    # The incident: its kind, severity and description, each with the record's evidence.
    incident = [
        i.claim
        for i in packet.items
        if isinstance(i, ClaimItem) and i.claim.subject.node_id == INCIDENT.declared_id
    ]
    assert {c.predicate for c in incident} >= {"event_kind", "has_description", "stated_severity"}
    assert all(c.provenance.evidence for c in incident)
    descriptions = [
        str(c.object.value)  # type: ignore[union-attr]
        for c in incident
        if c.predicate == "has_description"
    ]
    assert descriptions and "light curtain was muted" in descriptions[0]
    # Memory states no link between the incident and the arm: none is made up.
    assert not [c for c in incident if getattr(c.object, "node_id", "") in ARM_NAMES_IDS]
    assert answer_problems(WHY_THE_ARM_CELL, packet) == ()
    text = render_text(packet)
    assert "[E1]" in text and parse_citations(text) == packet.evidence_refs()


def test_every_predicate_the_snapshot_uses_is_inside_the_pin() -> None:
    # graph-schema 2.0.0 vocabulary (ADR 0012): carried as items, never reported as newer than
    # the pin, and the document is a 2.x one, so no "older graph" notice is owed.
    used = {c.predicate for c in F.demo_document().resolution.claims}
    assert used <= pinned.predicates()
    query = Query(
        include_inferred=False,
        budget=Budget(items=80),
        subjects=frozenset({Subject("machine", "cmms.asset:AMR-07")}),
        graph=GraphClause(None, 2, Direction.BOTH),
    )
    packet = ask(query)
    assert not [g for g in packet.gaps if "not in Context's pinned" in g.detail]
    assert packet.memory.graph_schema_version == 2
    assert pinned.older_graph_notice(packet.memory.graph_schema_version) is None
    assert "Graph read:" not in render_answer(packet)
    assert answer_problems(query, packet) == ()


def test_the_legged_robot_answers_too() -> None:
    query = Query(
        include_inferred=True,
        budget=Budget(items=40),
        subjects=frozenset({Subject("machine", "manifest:LEG-01")}),
        graph=GraphClause(None, 2, Direction.BOTH),
    )
    assert ("at_site", "manifest:PLANT-2") in facts(ask(query))  # via the runs it recorded


def test_a_calibration_drift_renders_its_declared_delta() -> None:
    # Memory's pipeline snapshot holds no ``drift`` claim yet, so the calibration fixture graph
    # (graph-schema 2.0.0 ``delta`` values) is what proves it: both renderers state the declared
    # numbers, form and unit, and nothing about their size.
    query = Query(
        include_inferred=False,
        budget=Budget(items=40),
        subjects=frozenset({Subject("sensor", "asset-tag:WCAM-7")}),
    )
    packet = Client(LocalEngine(ReferenceReader(X.document()))).query(query)
    drifts = [i for i in packet.items if isinstance(i, ClaimItem) and i.claim.predicate == "drift"]
    assert drifts and answer_problems(query, packet) == ()
    text = render_answer(packet)
    lines = [ln for ln in text.split("\n") if " drift delta {" in ln]
    assert len(lines) == len(drifts)
    for line in lines:
        assert '"quantity":"parameter"' in line and '(unit "mm", as declared)' in line
        for adjective in ("large", "small", "significant", "high", "low", "exceeds", "within"):
            assert adjective not in line.split()
    assert parse_citations(text) == packet.evidence_refs()
    markdown = [ln for ln in render_markdown(packet).split("\n") if " *drift* " in ln]
    assert len(markdown) == len(drifts)
    assert all('"quantity":"parameter"' in ln and '"value":"mm"' in ln for ln in markdown)


def test_the_demo_answer_is_byte_identical_every_time() -> None:
    first = canonical_bytes(ask(WHY_THE_ARM_CELL))
    fresh = Client(LocalEngine(ReferenceReader(F.demo_document()))).query(WHY_THE_ARM_CELL)
    assert canonical_bytes(fresh) == first


def test_claude_code_asks_over_mcp_and_gets_cited_text() -> None:
    client = AsyncClient(LocalEngine(demo_reader()))
    document = to_json(WHY_THE_ARM_CELL)

    async def go() -> types.CallToolResult:
        async with create_connected_server_and_client_session(
            build_server(client), read_timeout_seconds=timedelta(seconds=20)
        ) as session:
            return await session.call_tool(
                "neptune_query", {"query": document, "include_inferred": True}
            )

    result = asyncio.run(go())
    assert not result.isError
    first = result.content[0]
    assert isinstance(first, types.TextContent)
    assert first.text == render_answer(ask(WHY_THE_ARM_CELL))
    assert any(isinstance(block, types.ResourceLink) for block in result.content)


def test_the_cli_serves_a_memory_graph_over_stdio() -> None:
    graph = F.DEMO_SNAPSHOT  # the .json.gz itself: the CLI reads it as it is
    assert read_graph(graph).head == demo_reader().head
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "neptune_context.mcp", "--memory", str(graph)]
    )

    async def session() -> types.CallToolResult:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as s,
        ):
            await s.initialize()
            return await s.call_tool(
                "neptune_query", {"query": to_json(WHY_THE_ARM_CELL), "include_inferred": True}
            )

    result = asyncio.run(session())
    assert not result.isError
    first = result.content[0]
    assert isinstance(first, types.TextContent) and "[E1]" in first.text


def test_a_2x_document_names_its_release_and_the_packet_its_major(tmp_path: Path) -> None:
    # graph-schema 2.0.0 documents carry the full release string, and the packet names major 2
    # (Memory ADR 0019 §3, Context ADR 0012); the demo snapshot is one. A 1.x document stays
    # major 1: ``test_explain_markdown_context`` reads one.
    current = tmp_path / "current.json"
    data = F.document().to_json()
    assert data["graph_schema"] == "2.0.0" and data["graph_schema_version"] == 2
    current.write_text(json.dumps(data), encoding="utf-8")
    query = Query(
        include_inferred=False,
        budget=Budget(items=20),
        subjects=frozenset({Subject("machine", "asset-tag:AMR-07")}),
    )
    packet = Client(LocalEngine(read_graph(current))).query(query)
    assert packet.memory.graph_schema_version == 2 and packet.items
    assert ask(WHY_THE_ARM_CELL).memory.graph_schema_version == 2
    for release in ("3.0.0", "2.0", "1.6.0"):
        wrong = tmp_path / f"release-{release}.json"
        wrong.write_text(json.dumps({**data, "graph_schema": release}), encoding="utf-8")
        with pytest.raises(ValueError, match="graph_schema"):
            read_graph(wrong)


def test_the_cli_refuses_an_unreadable_graph(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "graph.json"
    broken.write_text('{"kind": "memory.graph"}', encoding="utf-8")
    duplicated = tmp_path / "duplicated.json"
    duplicated.write_text('{"kind": "memory.graph", "kind": "memory.graph"}', encoding="utf-8")
    not_a_number = tmp_path / "nan.json"
    not_a_number.write_text('{"head": NaN}', encoding="utf-8")
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    fifo = tmp_path / "graph.fifo"
    os.mkfifo(fifo)  # a FIFO would block a reader forever: refused before it is opened
    for path in (broken, duplicated, not_a_number, deep, tmp_path / "missing.json", fifo):
        assert main(["--memory", str(path)]) == 2
    assert capsys.readouterr().err.count("neptune mcp:") == 6


def test_a_graph_larger_than_the_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import neptune_context.engine as engine

    assert engine.MAX_GRAPH_BYTES == 256 * 1024 * 1024
    big = tmp_path / "graph.json"
    big.write_text(json.dumps(F.demo_document().to_json()), encoding="utf-8")
    monkeypatch.setattr(engine, "MAX_GRAPH_BYTES", 1024)
    with pytest.raises(ValueError, match="larger than 1024 bytes"):
        read_graph(big)
