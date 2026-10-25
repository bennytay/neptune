"""``--site``: the ``neptune_site_*`` tools over a site model (site model v3, spec §8)."""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from typing import TYPE_CHECKING

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from neptune_memory.site.facts import Citation, Fact, LogEntry, Ref, Seen, Value
from neptune_memory.site.views import fold

from context_packet_goldens import PACKETS
from neptune_context.mcp.site import SITE_TOOLS, SiteTools, build_site_server

if TYPE_CHECKING:
    from pathlib import Path

    from neptune_memory.site.views import SiteModel

V2_TOOLS = [
    "neptune_query",
    "neptune_why",
    "neptune_diff",
    "neptune_compare_runs",
    "neptune_hydrate",
    "neptune_plan",
    "neptune_entities",
]
SITE_NAMES = [
    "neptune_site_resolve",
    "neptune_site_state_at",
    "neptune_site_find",
    "neptune_site_timeline",
    "neptune_site_why",
    "neptune_site_diff",
    "neptune_site_gaps",
    "neptune_site_impact",
    "neptune_site_search_evidence",
    "neptune_site_sql",
    "neptune_site_insights",
    "neptune_site_shift_report",
]


def _list(args: list[str]) -> list[str]:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "neptune_context.mcp", *args]
    )

    async def session() -> list[str]:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as s,
        ):
            await s.initialize()
            return [t.name for t in (await s.list_tools()).tools]

    return asyncio.run(session())


def test_site_flag_alone_lists_the_site_tools(tmp_path: Path) -> None:
    assert _list(["--site", str(tmp_path)]) == SITE_NAMES == list(SITE_TOOLS)


def test_site_flag_adds_the_site_tools_beside_the_v2_tools(tmp_path: Path) -> None:
    assert _list(["--packets", str(PACKETS), "--site", str(tmp_path)]) == V2_TOOLS + SITE_NAMES


STOPPED = Fact(
    subject="machine:cv-2",
    predicate="mode",
    object=Value("stopped"),
    valid_from="2026-10-20T10:05:02",
    valid_to=None,
    kind="observed",
    source="plc.cv2",
    citations=(Citation("feed/plc_cv2.jsonl", "line 812", '"state": "stopped"'),),
)


def _one_fact(site_dir: Path) -> SiteModel:
    """The smallest site model: one observed mode, folded by the real views."""
    seen = Seen("plc.cv2", "2026-10-20T10:05:02")
    return fold([LogEntry(1, "seen", seen=seen), LogEntry(2, "fact", fact=STOPPED)])


def test_site_tool_answers_with_cited_lines(tmp_path: Path) -> None:
    server = build_site_server(SiteTools(tmp_path, loader=_one_fact))

    async def go() -> types.CallToolResult:
        async with create_connected_server_and_client_session(server) as session:
            return await session.call_tool("neptune_site_state_at", {"entity": "machine:cv-2"})

    result = asyncio.run(go())
    assert not result.isError
    first = result.content[0]
    assert isinstance(first, types.TextContent)
    assert '- machine:cv-2 mode = "stopped" since 10:05:02 · observed [S1]' in first.text
    assert "[S1] feed/plc_cv2.jsonl line 812" in first.text


def _chain(site_dir: Path) -> SiteModel:
    """A fault on cv-2, then the arm in the cell it feeds starves: one starvation chain."""

    def fact(subject: str, predicate: str, obj: Ref | Value, t: str | None, line: int) -> Fact:
        cite = Citation("feed/x.jsonl" if t else "static/layout.yaml", f"line {line}", None)
        if t is None:
            return Fact(subject, predicate, obj, t, None, "stated", "layout", (cite,))
        return Fact(subject, predicate, obj, t, None, "observed", "plc", (cite,))

    facts = [
        fact("machine:cv-2", "feeds", Ref("cell:c3"), None, 1),
        fact("robot:arm-c3", "located_in", Ref("cell:c3"), None, 2),
        fact("machine:cv-2", "health", Value("fault:JAM"), "2026-10-20T10:05:02", 3),
        fact("robot:arm-c3", "mode", Value("idle_starved"), "2026-10-20T10:05:30", 4),
    ]
    entries = [LogEntry(1, "seen", seen=Seen("plc", "2026-10-20T10:06:00"))]
    entries += [LogEntry(i + 2, "fact", fact=f) for i, f in enumerate(facts)]
    return fold(entries)


def test_site_insight_tools_answer_findings_and_a_report(tmp_path: Path) -> None:
    site = SiteTools(tmp_path, loader=_chain)
    found = site.call("neptune_site_insights", {})
    report = site.call(
        "neptune_site_shift_report", {"start": "2026-10-20T10:00:00", "end": "2026-10-20T11:00:00"}
    )
    texts = []
    for result in (found, report):
        assert not result.isError
        first = result.content[0]
        assert isinstance(first, types.TextContent)
        texts.append(first.text)
    assert "starvation chain: machine:cv-2 fault:JAM at 10:05:02 → robot:arm-c3" in texts[0]
    assert "· inferred [S" in texts[0]
    assert "Chains (inferred):" in texts[1]
    assert "What happened:" in texts[1]
