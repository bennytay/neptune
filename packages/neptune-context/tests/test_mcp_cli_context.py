"""``python -m neptune_context.mcp``: argument handling and a real stdio session in a subprocess."""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from context_packet_goldens import PACKETS
from neptune_context.mcp.__main__ import main
from neptune_context.render.citations import render_text
from sdk_testing_context import golden_packet

CLAIM = "claim:sha256:03ef80551292669e368d326b22bd44b2a3c5a6461298c94469f16ad71110ad4a"


def test_the_cli_refuses_bad_targets_with_exit_code_two(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NEPTUNE_TOKEN", raising=False)
    assert main(["--packets", "/nonexistent/neptune-packets"]) == 2
    assert main(["--url", "ftp://example.org"]) == 2
    monkeypatch.setenv("NEPTUNE_TOKEN", "tok-secret")
    assert main(["--url", "http://example.org"]) == 2  # a token never travels over plain http
    err = capsys.readouterr().err
    assert err.count("neptune mcp:") == 3
    assert "tok-secret" not in err


def test_an_unreadable_packet_file_is_exit_code_two(
    tmp_path: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "packets"  # type: ignore[operator]
    (folder / "q01.json").mkdir(parents=True)  # a directory where a packet file should be
    assert main(["--packets", str(folder)]) == 2
    assert "neptune mcp:" in capsys.readouterr().err


def test_the_cli_needs_exactly_one_target() -> None:
    for argv in ([], ["--url", "https://x", "--packets", "."]):
        with pytest.raises(SystemExit) as raised:
            main(argv)
        assert raised.value.code == 2


def test_a_real_stdio_session_lists_tools_and_answers_a_question() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "neptune_context.mcp", "--packets", str(PACKETS)],
    )

    async def session() -> tuple[list[str], types.CallToolResult]:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as s,
        ):
            await s.initialize()
            names = [t.name for t in (await s.list_tools()).tools]
            result = await s.call_tool(
                "neptune_why", {"claim_id": CLAIM, "include_inferred": False}
            )
            return names, result

    names, result = asyncio.run(session())
    assert names == ["neptune_query", "neptune_why", "neptune_diff", "neptune_hydrate"]
    assert not result.isError
    first = result.content[0]
    assert isinstance(first, types.TextContent)
    assert first.text == render_text(golden_packet("q04"))
