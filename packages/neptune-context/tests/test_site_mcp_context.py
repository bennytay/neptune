"""``--site``: the ``neptune_site_*`` tools over a site model (site model v3, spec §8)."""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from typing import TYPE_CHECKING

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from neptune_memory.site.facts import Citation, Fact, Value
from neptune_memory.site.log import FactLog

from context_packet_goldens import PACKETS
from neptune_context.mcp.site import SITE_TOOLS, SiteTools, build_site_server

if TYPE_CHECKING:
    from pathlib import Path

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
    "neptune_site_changes",
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


def _text(result: types.CallToolResult) -> str:
    assert not result.isError
    first = result.content[0]
    assert isinstance(first, types.TextContent)
    return first.text


def test_site_tool_answers_with_cited_lines(tmp_path: Path) -> None:
    with FactLog(tmp_path) as log:
        log.seen("plc.cv2", "2026-10-20T10:05:02")
        log.fact(STOPPED)
    server = build_site_server(SiteTools(tmp_path))

    async def go() -> types.CallToolResult:
        async with create_connected_server_and_client_session(server) as session:
            return await session.call_tool("neptune_site_state_at", {"entity": "machine:cv-2"})

    text = _text(asyncio.run(go()))
    assert '- machine:cv-2 mode = "stopped" since 10:05:02 · observed [S1]' in text
    assert "[S1] feed/plc_cv2.jsonl line 812" in text


def test_site_tools_apply_what_the_log_appends_between_calls(tmp_path: Path) -> None:
    """A live site: entries appended after the first call show up in the next one."""
    site = SiteTools(tmp_path)
    with FactLog(tmp_path) as log:
        log.seen("plc.cv2", "2026-10-20T10:05:02")
        log.fact(STOPPED)
    first = site.model()
    assert "stopped" in _text(site.call("neptune_site_state_at", {"entity": "machine:cv-2"}))

    running = Fact(
        "machine:cv-2",
        "mode",
        Value("running"),
        "2026-10-20T10:41:00",
        None,
        "observed",
        "plc.cv2",
        (Citation("feed/plc_cv2.jsonl", "line 1250", "running"),),
    )
    with FactLog(tmp_path) as log:
        log.seen("plc.cv2", "2026-10-20T10:41:00")
        log.fact(running)
    assert site.model() is first  # applied in place, not folded again
    state = _text(site.call("neptune_site_state_at", {"entity": "machine:cv-2"}))
    assert '- machine:cv-2 mode = "running" since 10:41:00' in state
    changes = _text(site.call("neptune_site_changes", {"since_seq": 2}))
    assert changes.startswith("changes · at 2026-10-20T10:41:00 · log seq 4")
    assert '- machine:cv-2 mode = "running"' in changes and "stopped" not in changes
    assert "[S1] feed/plc_cv2.jsonl line 1250" in changes
