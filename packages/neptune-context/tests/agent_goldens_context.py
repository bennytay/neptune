"""Goldens for the agent renderer and the Demo v1 agent transcript (ADR 0009).

``python tests/agent_goldens_context.py`` (from the package directory) rewrites:

- ``golden/agent/<stem>.txt``: the ten persona packets (ADR 0003 §10) rendered by
  ``render_answer``;
- ``golden/agent/planner-recordings.jsonl``: the one planner exchange the transcript replays
  (``synthetic``: a scripted model answer, so the transcript needs no API key);
- ``golden/agent/transcript-arm-cell.json``: an agent driving the six MCP tools over the Demo v1
  corpus snapshot ("why did the arm-cell incident happen, what changed"), each call with the
  text it got back.

The snapshot is ``retrieve_fixtures_context.DEMO_SNAPSHOT``: Memory's pipeline-built graph of the
acceptance corpus, frozen under ``golden/`` by ``scripts/freeze_demo_graph.py`` so Memory's own
regenerations never touch these goldens; refreeze, then regenerate them, to move the demo on.
``neptune_why`` and ``neptune_diff`` answer with the packet's trails (MVL-149, ADR 0010) rendered
by ``render_answer`` (ADR 0011), so their bytes are recorded too. The one step marked
``"exact": false`` is the second ``neptune_query``, checked for citations only.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

import retrieve_fixtures_context as F
from neptune.identity.canonical_json import dumps
from neptune_context.mcp import build_server
from neptune_context.mcp.__main__ import AGENT_DEFAULTS, local_client
from neptune_context.query import Direction, GraphClause, Query, Subject, to_json
from neptune_context.query.plan import (
    ModelRequest,
    ModelResponse,
    RecordingClient,
    ReplayClient,
    dump_recordings,
    load_recordings,
    plan,
)
from neptune_context.render.agent import render_answer
from neptune_context.sdk import entity_index
from sdk_testing_context import STEMS, golden_packet

if TYPE_CHECKING:
    from neptune_context.sdk import AsyncClient

HERE: Final = Path(__file__).resolve().parent
AGENT: Final = HERE / "golden" / "agent"
RECORDINGS: Final = AGENT / "planner-recordings.jsonl"
TRANSCRIPT: Final = AGENT / "transcript-arm-cell.json"
PACKETS: Final = HERE / "golden" / "packets"

QUESTION: Final = "Why did the arm-cell incident happen, and what changed on the arm?"
INCIDENT: Final = Subject(*F.DEMO_INCIDENT)
# What the scripted planner model answers: the typed query an agent would write for QUESTION,
# inside the agent's default budget.
PLANNED: Final = Query(
    include_inferred=True,
    budget=AGENT_DEFAULTS.budget,
    subjects=frozenset(Subject(*n) for n in F.DEMO_ARM_NAMES),
    graph=GraphClause(None, 2, Direction.BOTH),
)


# The clock ServiceNow's change records state their intervals on: the arm's configuration changes.
SERVICENOW_CLOCK: Final = {
    "kind": "domain",
    "domain_id": "rec:sha256:659e221e993c637fb9dc0a93a1cf07880be2dac2688971d18afab81ba82b78d4",
}
# Memory states no link from the incident to the arm and a planner cannot name the incident's
# content-addressed node, so the agent asks about the incident node itself (``F.DEMO_INCIDENT``).
INCIDENT_ONLY: Final = Query(
    include_inferred=True, budget=AGENT_DEFAULTS.budget, subjects=frozenset({INCIDENT})
)


def answer_path(stem: str) -> Path:
    (packet,) = sorted(PACKETS.glob(f"{stem}-*.json"))
    return AGENT / (packet.stem + ".txt")


class ScriptedModel:
    """A model that always answers with ``PLANNED`` (recorded ``synthetic``)."""

    client_id: Final = "scripted"

    def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(dumps(to_json(PLANNED)).decode(), "end", "golden-scripted-planner")


def record_planner() -> str:
    """The planner exchange for QUESTION as JSON Lines (one synthetic recording)."""
    recorder = RecordingClient(ScriptedModel())
    plan(
        QUESTION,
        "head",
        AGENT_DEFAULTS,
        resolver=entity_index(F.demo_document()),
        client=recorder,
    )
    return dump_recordings(recorder.recordings)


def demo_client(recordings: Path = RECORDINGS) -> AsyncClient:
    """The client ``--memory <snapshot> --planner-recordings <recordings>`` serves."""
    return local_client(F.demo_document(), model=ReplayClient(load_recordings(recordings)))


def query_arguments(query: Query, include_inferred: bool) -> dict[str, Any]:
    document = to_json(query)
    assert isinstance(document, dict)
    del document["include_inferred"]
    return {"include_inferred": include_inferred, "query": document}


def steps(packet_query_text: str) -> list[dict[str, Any]]:
    """The agent's calls. ``packet_query_text`` is the answer to the planned query, from which
    the agent copies a claim id and an evidence ref, as a real agent would."""
    changed = next(
        line.split()[-1]
        for line in packet_query_text.splitlines()
        if line.startswith("[I") and " claim " in line and _fact_has(packet_query_text, line)
    )
    evidence = json.loads(
        next(line for line in packet_query_text.splitlines() if line.startswith("[E1] "))[5:]
    )
    return [
        {"tool": "neptune_entities", "arguments": {"include_inferred": False}, "exact": True},
        {
            "tool": "neptune_entities",
            "arguments": {"kind": "configuration", "include_inferred": False},
            "exact": True,
        },
        {
            "tool": "neptune_entities",
            "arguments": {
                "text": "What happened to ARM-3A at PLANT-2?",
                "include_inferred": False,
            },
            "exact": True,
        },
        {"tool": "neptune_plan", "arguments": {"question": QUESTION}, "exact": True},
        {"tool": "neptune_query", "arguments": query_arguments(PLANNED, True), "exact": True},
        {"tool": "neptune_query", "arguments": query_arguments(PLANNED, False), "exact": False},
        {
            "tool": "neptune_why",
            "arguments": {"claim_id": changed, "include_inferred": True},
            "exact": True,
        },
        {
            "tool": "neptune_diff",
            "arguments": {
                "subject": {"kind": F.DEMO_ARM[0], "declared_id": F.DEMO_ARM[1]},
                "before": {"clock": SERVICENOW_CLOCK, "ticks": 1_780_000_000},  # firmware 5.6.0
                "after": {"clock": SERVICENOW_CLOCK, "ticks": 1_790_000_000},  # the TCP change
                "include_inferred": True,
            },
            "exact": True,
        },
        {"tool": "neptune_query", "arguments": query_arguments(INCIDENT_ONLY, True), "exact": True},
        {"tool": "neptune_hydrate", "arguments": {"evidence": evidence}, "exact": True},
        {
            "tool": "neptune_query",
            "arguments": {
                "include_inferred": False,
                "query": {**query_arguments(PLANNED, True)["query"], "include_inferred": True},
            },
            "exact": True,
        },
    ]


def _fact_has(text: str, item_line: str) -> bool:
    """Whether the fact behind an ``Items:`` line is the arm's tool-centre-point change (the
    claim the agent asks ``neptune_why`` about)."""
    number = item_line[2 : item_line.index("]")]
    return any(
        line.startswith(f"{number}. ") and "TCP z=145.5 mm" in line for line in text.splitlines()
    )


def result_json(result: types.CallToolResult) -> dict[str, Any]:
    text = "".join(b.text for b in result.content if isinstance(b, types.TextContent))
    links = [str(b.uri) for b in result.content if isinstance(b, types.ResourceLink)]
    return {"is_error": bool(result.isError), "links": links, "text": text}


async def _call_all(client: AsyncClient, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    async with create_connected_server_and_client_session(
        build_server(client), read_timeout_seconds=timedelta(seconds=60)
    ) as session:
        return [result_json(await session.call_tool(c["tool"], c["arguments"])) for c in calls]


def run_in_process(client: AsyncClient, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return asyncio.run(_call_all(client, calls))


def transcript(client: AsyncClient) -> dict[str, Any]:
    """The whole transcript: the calls and, for exact steps, what came back."""
    first = run_in_process(client, [{"tool": "neptune_query", **_planned_call()}])[0]
    calls = steps(first["text"])
    results = run_in_process(client, calls)
    out = []
    for call, result in zip(calls, results, strict=True):
        entry = {**call, "is_error": result["is_error"]}
        if call["exact"]:
            entry |= {"links": result["links"], "text": result["text"]}
        out.append(entry)
    snapshot = F.DEMO_SNAPSHOT.relative_to(F.ROOT).as_posix()
    return {"question": QUESTION, "snapshot": snapshot, "steps": out}


def _planned_call() -> dict[str, Any]:
    return {"arguments": query_arguments(PLANNED, True)}


def main() -> int:
    AGENT.mkdir(parents=True, exist_ok=True)
    for stem in STEMS:
        answer_path(stem).write_text(render_answer(golden_packet(stem)), encoding="utf-8")
    RECORDINGS.write_text(record_planner(), encoding="utf-8")
    document = transcript(demo_client())
    TRANSCRIPT.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(f"wrote {AGENT}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
