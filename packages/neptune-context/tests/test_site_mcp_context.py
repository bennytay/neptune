"""``--site``: the ``neptune_site_*`` tools over a site model."""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from typing import TYPE_CHECKING

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from neptune_memory.site.facts import Citation, Fact, Ref, Value
from neptune_memory.site.log import FactLog

from neptune_context.mcp.site import SETUP_TOOLS, SITE_TOOLS, SiteTools, build_site_server

if TYPE_CHECKING:
    from pathlib import Path

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
    "neptune_site_insights",
    "neptune_site_shift_report",
    "neptune_site_propose_action",
    "neptune_site_actions",
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


def test_site_flag_lists_the_site_tools(tmp_path: Path) -> None:
    listed = _list(["--site", str(tmp_path)])
    assert listed[: len(SITE_NAMES)] == SITE_NAMES == list(SITE_TOOLS)
    assert listed[len(SITE_NAMES) :] == list(SETUP_TOOLS)
    assert "neptune_setup_start" in SETUP_TOOLS


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


def _chain(site_dir: Path) -> None:
    """A fault on cv-2, then the arm in the cell it feeds starves: one starvation chain."""

    def fact(subject: str, predicate: str, obj: Ref | Value, t: str | None, line: int) -> Fact:
        cite = Citation("feed/x.jsonl" if t else "static/layout.yaml", f"line {line}", None)
        if t is None:
            return Fact(subject, predicate, obj, t, None, "stated", "layout", (cite,))
        return Fact(subject, predicate, obj, t, None, "observed", "plc", (cite,))

    with FactLog(site_dir) as log:
        log.seen("plc", "2026-10-20T10:06:00")
        log.fact(fact("machine:cv-2", "feeds", Ref("cell:c3"), None, 1))
        log.fact(fact("robot:arm-c3", "located_in", Ref("cell:c3"), None, 2))
        log.fact(fact("machine:cv-2", "health", Value("fault:JAM"), "2026-10-20T10:05:02", 3))
        log.fact(fact("robot:arm-c3", "mode", Value("idle_starved"), "2026-10-20T10:05:30", 4))


def test_site_insight_tools_answer_findings_and_a_report(tmp_path: Path) -> None:
    _chain(tmp_path)
    site = SiteTools(tmp_path)
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


def test_site_propose_action_logs_a_pending_proposal_and_nothing_approves(tmp_path: Path) -> None:
    with FactLog(tmp_path) as log:
        log.seen("fleet", "2026-10-20T10:06:00")
        log.fact(STOPPED)
        log.fact(
            Fact(
                "robot:amr-01",
                "in_zone",
                Ref("zone:aisle-a"),
                "2026-10-20T10:06:00",
                None,
                "observed",
                "fleet",
                (Citation("feed/fleet.jsonl", "line 9", "zone:aisle-a"),),
            )
        )
        log.fact(
            Fact(
                "zone:aisle-b",
                "name",
                Value("Aisle B"),
                None,
                None,
                "stated",
                "layout",
                (Citation("static/layout.yaml", "line 22", "Aisle B"),),
            )
        )
    site = SiteTools(tmp_path)
    args: dict[str, object] = {"action": "send_to", "target": "robot:amr-01"}
    args |= {"zone": "zone:aisle-b"}
    args |= {"reason": "cv-2 is stopped", "cites": [STOPPED.id]}
    text = _text(site.call("neptune_site_propose_action", args))
    assert '- robot:amr-01 proposed_action = "send_to zone:aisle-b"' in text
    assert "· inferred, pending, proposed [S1][S2]" in text
    assert '[S1] actions/proposed.jsonl line 1 "cv-2 is stopped"' in text
    assert "waiting for a person" in _text(site.call("neptune_site_actions", {}))
    assert not any("approve" in name or "reject" in name for name in SITE_TOOLS)


def test_site_setup_tools_onboard_a_folder(tmp_path: Path) -> None:
    folder = tmp_path / "robot"
    folder.mkdir()
    (folder / "log.csv").write_text(
        "Timestamp,Robot,Mode\n2026-03-02T08:00:00,arm-1,running\n2026-03-02T08:00:05,arm-1,stopped\n",
        encoding="utf-8",
    )
    site = SiteTools(tmp_path / "site")
    assert "Files: 1, 0 mapped" in _text(site.call("neptune_setup_start", {"folder": str(folder)}))
    assert "files/log.csv: table" in _text(site.call("neptune_setup_inventory", {}))
    assert 'column: "Mode"' in _text(site.call("neptune_setup_draft", {"file": "files/log.csv"}))
    goal = {"name": "Uptime", "metric": "share of time running", "target": ">= 95 %"}
    site.call("neptune_setup_mission", {"prompt": "Keep arm-1 running.", "goals": [goal]})
    assert "Mission: goal:uptime" in _text(site.call("neptune_setup_build", {}))
    bad = site.call(
        "neptune_setup_save", {"which": "mappings", "text": "tables: [{file: nope.csv}]"}
    )
    assert bad.isError
