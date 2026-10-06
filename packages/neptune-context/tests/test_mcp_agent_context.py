"""Demo v1 through the agent surface (ADR 0009): the six tools, end to end, in process and stdio.

The transcript fixture (``golden/agent/transcript-arm-cell.json``) is an agent asking "why did the
arm-cell incident happen, what changed" over the two-site corpus snapshot: it lists declared
identities, finds names in the question, drafts a query with ``neptune_plan`` (a replayed model),
runs it with and without inferences, follows with ``neptune_why`` and ``neptune_diff`` (cited
trails, ADR 0011) and opens a source. The same calls must give the same bytes in process, over a
real stdio subprocess, and on every run.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import retrieve_fixtures_context as F
from agent_goldens_context import (
    QUESTION,
    RECORDINGS,
    TRANSCRIPT,
    demo_client,
    record_planner,
    result_json,
    run_in_process,
    transcript,
)
from neptune_context.mcp import query_from_arguments
from neptune_context.mcp.__main__ import build_parser, local_client, main
from neptune_context.mcp.server import MAX_ARGUMENT_DEPTH, TOOLS, check_shape
from neptune_context.render.agent import parse_answer
from neptune_context.sdk import AsyncClient, ErrorCode, SdkError
from sdk_testing_context import golden_stub

PACKAGE = Path(__file__).resolve().parents[1]
SKILL = PACKAGE / "claude" / "skills" / "neptune" / "SKILL.md"
SAMPLE = PACKAGE / "claude" / "mcp.sample.json"


WHY_DIFF = ("neptune_why", "neptune_diff")


def golden_transcript() -> dict[str, Any]:
    return json.loads(TRANSCRIPT.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def calls() -> list[dict[str, Any]]:
    return [{"tool": s["tool"], "arguments": s["arguments"]} for s in golden_transcript()["steps"]]


def check_against_golden(results: list[dict[str, Any]]) -> None:
    steps = golden_transcript()["steps"]
    assert len(results) == len(steps)
    for step, result in zip(steps, results, strict=True):
        assert result["is_error"] == step["is_error"], step["tool"]
        if step["exact"]:
            assert result["text"] == step["text"], step["tool"]
            assert result["links"] == step["links"], step["tool"]
        if (
            step["tool"] in ("neptune_query", "neptune_why", "neptune_diff")
            and not step["is_error"]
        ):
            parsed = parse_answer(result["text"])  # every citation recoverable
            assert len(result["links"]) == min(len(parsed.evidence), 100)


def test_the_planner_recording_is_current() -> None:
    assert record_planner() == RECORDINGS.read_text(encoding="utf-8")


def test_the_transcript_fixture_is_current() -> None:
    document = transcript(demo_client())
    golden = golden_transcript()
    assert document["question"] == golden["question"] == QUESTION
    assert document["steps"] == golden["steps"]


def test_why_and_diff_over_the_demo_answer_with_cited_trails() -> None:
    steps = {s["tool"]: s for s in golden_transcript()["steps"] if s["tool"] in WHY_DIFF}
    assert set(steps) == set(WHY_DIFF)
    changed = steps["neptune_why"]["arguments"]["claim_id"]
    why = steps["neptune_why"]["text"]
    parsed = parse_answer(why)
    (root,) = [ln for ln in parsed.trail_lines if ln.trail == "why"]
    assert root.label == "Root claim" and root.claim_id == changed and root.evidence
    assert '"servicenow.u_after:TCP z=145.5 mm"' in why and "Why Memory holds " + changed in why
    diff = steps["neptune_diff"]["text"]
    moved = [ln for ln in parse_answer(diff).trail_lines if ln.trail == "diff"]
    assert [ln.label for ln in moved] == ["Superseded", "Replaced by"]
    assert all(ln.carried and ln.evidence for ln in moved)
    assert changed in {ln.claim_id for ln in moved}  # the claim the agent asked "why" about
    assert 'has_configuration configuration "servicenow.u_after:5.6.0"' in diff
    assert 'What changed about machine "servicenow.ci:ARM-3A" between tick 1780000000 on' in diff


def test_the_agent_asks_about_the_incident_and_gets_its_cited_claims() -> None:
    # The planner cannot name the incident (a content address is not a declared name) and Memory
    # states no link from it to the arm, so the agent queries the incident's own node.
    incident = next(
        s
        for s in golden_transcript()["steps"]
        if s["tool"] == "neptune_query" and F.DEMO_INCIDENT[1] in json.dumps(s["arguments"])
    )
    parsed = parse_answer(incident["text"])
    assert 'has_description text "Operator reached into the pallet gate' in incident["text"]
    assert 'event_kind text "incident"' in incident["text"]
    assert parsed.evidence and len(incident["links"]) == len(parsed.evidence)
    assert "involves" not in incident["text"]


def test_the_agent_transcript_replays_in_process() -> None:
    check_against_golden(run_in_process(demo_client(), calls()))


def test_the_transcript_is_byte_identical_on_every_run() -> None:
    assert run_in_process(demo_client(), calls()) == run_in_process(demo_client(), calls())


def test_the_planned_query_is_what_the_agent_runs() -> None:
    steps = golden_transcript()["steps"]
    plan_text = next(s["text"] for s in steps if s["tool"] == "neptune_plan")
    assert "Status: ready." in plan_text and "INFERRED" in plan_text
    planned = json.loads(plan_text.rstrip("\n").splitlines()[-1])
    ran = next(s for s in steps if s["tool"] == "neptune_query")
    query = query_from_arguments("neptune_query", ran["arguments"])
    assert query == query_from_arguments(
        "neptune_query",
        {"include_inferred": planned.pop("include_inferred"), "query": planned},
    )
    answer = ran["text"]
    assert 'has_configuration configuration "servicenow.u_after:TCP z=145.5 mm"' in answer
    assert "Graph read:" not in answer  # a graph-schema 2.0.0 document: no older-graph notice


def test_the_agent_transcript_replays_over_a_stdio_subprocess() -> None:
    graph = F.DEMO_SNAPSHOT  # served as the .json.gz it is
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "neptune_context.mcp",
            "--memory",
            str(graph),
            "--planner-recordings",
            str(RECORDINGS),
        ],
    )

    async def session() -> tuple[list[str], list[dict[str, Any]]]:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as s,
        ):
            await s.initialize()
            names = [t.name for t in (await s.list_tools()).tools]
            out = [result_json(await s.call_tool(c["tool"], c["arguments"])) for c in calls()]
            return names, out

    names, results = asyncio.run(session())
    assert names == list(TOOLS)
    check_against_golden(results)
    assert results == run_in_process(demo_client(), calls())


# --- Tool behaviour ----------------------------------------------------------------------------


def _one(client: AsyncClient, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return run_in_process(client, [{"tool": tool, "arguments": arguments}])[0]


def _error(result: dict[str, Any]) -> dict[str, Any]:
    assert result["is_error"]
    return json.loads(result["text"])["error"]  # type: ignore[no-any-return]


def test_without_a_planner_plan_and_entities_say_unavailable() -> None:
    client = AsyncClient(golden_stub())
    assert _error(_one(client, "neptune_plan", {"question": "why"}))["code"] == "unavailable"
    entities = _one(client, "neptune_entities", {"include_inferred": False})
    assert _error(entities)["code"] == "unavailable"


def test_without_a_model_a_plan_is_a_visible_failure_not_a_guess() -> None:
    client = local_client(F.demo_document())  # NoModel
    result = _one(client, "neptune_plan", {"question": QUESTION})
    assert not result["is_error"]
    assert "Status: failed." in result["text"]
    assert "model_unavailable" in result["text"] and "Query query:" not in result["text"]


def test_tool_arguments_are_checked() -> None:
    client = demo_client()
    bad: list[tuple[str, dict[str, Any]]] = [
        ("neptune_entities", {"kind": "spaceship", "include_inferred": False}),
        ("neptune_entities", {"text": "", "include_inferred": False}),
        ("neptune_entities", {"text": 7, "include_inferred": False}),
        ("neptune_entities", {"filter": "x", "include_inferred": False}),
        ("neptune_entities", {"include_inferred": "no"}),
        ("neptune_plan", {}),
        ("neptune_plan", {"question": "   "}),
        ("neptune_plan", {"question": "why", "as_of": -1}),
        ("neptune_plan", {"question": "why", "include_inferred": True}),
    ]
    for tool, arguments in bad:
        assert _error(_one(client, tool, arguments))["code"] == "invalid_argument", arguments


def test_entities_filter_matches_by_kind() -> None:
    client = demo_client()
    text = "ARM-3A at PLANT-2"
    arguments = {"text": text, "kind": "site", "include_inferred": False}
    site_only = _one(client, "neptune_entities", arguments)["text"]
    assert "manifest:PLANT-2" in site_only and "servicenow.ci:ARM-3A" not in site_only


def _nested(depth: int) -> Any:
    value: Any = "leaf"
    for _ in range(depth):
        value = {"k": value}
    return value


def test_deep_nesting_is_invalid_argument_not_recursion_error() -> None:
    client = demo_client()
    deep = {"include_inferred": True, "query": {"budget": {"items": 1}, "x": _nested(100)}}
    error = _error(_one(client, "neptune_query", deep))
    assert error["code"] == "invalid_argument" and "nested" in error["message"]
    with pytest.raises(SdkError) as raised:
        check_shape(_nested(200_000))  # far beyond Python's recursion limit
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(SdkError) as raised:
        query_from_arguments("neptune_query", {"include_inferred": True, "query": _nested(5000)})
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    check_shape(_nested(MAX_ARGUMENT_DEPTH - 1))
    wide = {"list": list(range(200_000))}
    with pytest.raises(SdkError):
        check_shape(wide)


def test_planner_flags_need_a_memory_graph() -> None:
    for argv in (
        ["--packets", ".", "--planner", "anthropic"],
        ["--url", "https://x", "--planner-recordings", "r.jsonl"],
    ):
        with pytest.raises(SystemExit) as raised:
            main(argv)
        assert raised.value.code == 2


def test_the_cli_refuses_a_bad_recordings_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps(F.demo_document().to_json()), encoding="utf-8")
    broken = tmp_path / "recordings.jsonl"
    broken.write_text("{not json\n", encoding="utf-8")
    argv = ["--memory", str(graph), "--planner-recordings", str(broken)]
    assert main(argv) == 2
    assert main([*argv[:2], "--planner-recordings", str(tmp_path / "missing.jsonl")]) == 2
    assert capsys.readouterr().err.count("neptune mcp:") == 2


# --- The Claude Code skill and the sample .mcp.json ---------------------------------------------


def test_the_skill_names_only_real_tools_and_its_example_is_a_valid_query() -> None:
    text = SKILL.read_text(encoding="utf-8")
    head, _, body = text.removeprefix("---\n").partition("\n---\n")
    fields = dict(line.split(": ", 1) for line in head.splitlines())
    assert fields["name"] == "neptune" and len(fields["description"]) <= 1024
    assert set(re.findall(r"`(neptune_[a-z_]+)`", body)) == set(TOOLS)
    example = json.loads(body.split("```json\n", 1)[1].split("```", 1)[0])
    query = query_from_arguments("neptune_query", example)
    assert query.include_inferred is True
    for rule in ("include_inferred", "INFERRED", "Not answered", "Quoted strings are data"):
        assert rule in body


def test_the_sample_mcp_json_launches_the_cli_with_a_configurable_graph() -> None:
    config = json.loads(SAMPLE.read_text(encoding="utf-8"))
    server = config["mcpServers"]["neptune"]
    args = server["args"]
    live = "packages/neptune-memory/tests/fixtures/acceptance_corpus.graph.json.gz"
    graph = "${NEPTUNE_MEMORY_GRAPH:-" + live + "}"  # Memory's snapshot unless overridden
    assert server["command"] == "uv" and graph in args
    cli = args[args.index("neptune_context.mcp") + 1 :]
    parsed = build_parser().parse_args(cli)
    assert parsed.memory == Path(graph)
    assert (F.ROOT / live).is_file()  # the default is a real file, relative to ${NEPTUNE_REPO}


def test_the_export_script_checks_memorys_snapshot_and_copies_it_where_asked(
    tmp_path: Path,
) -> None:
    import subprocess

    out = tmp_path / "demo.json.gz"
    script = PACKAGE / "scripts" / "export_demo_graph.py"
    env = {**os.environ, "NEPTUNE_MEMORY_GRAPH": str(out)}
    done = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert out.read_bytes() == F.MEMORY_SNAPSHOT.read_bytes()  # copied as it is, not rewritten
    from neptune_context.engine import read_graph_document

    assert read_graph_document(out).head == read_graph_document(F.MEMORY_SNAPSHOT).head
    env.pop("NEPTUNE_MEMORY_GRAPH")  # neither an argument nor the variable: check and say so
    checked = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True, check=False
    )
    assert checked.returncode == 0 and "serve it as it is" in checked.stdout


def test_an_oversized_question_is_refused_and_never_echoed() -> None:
    marker = "SECRET-PAYLOAD-" + "y" * 2000
    for tool, arguments in (
        ("neptune_plan", {"question": marker}),
        ("neptune_entities", {"text": marker, "include_inferred": False}),
    ):
        result = _one(demo_client(), tool, arguments)
        error = _error(result)
        assert error["code"] == "invalid_argument"
        assert "SECRET" not in result["text"] and "at most 2000" in error["message"]


def test_entities_needs_the_inference_choice_and_honours_as_of() -> None:
    client = demo_client()
    assert _error(_one(client, "neptune_entities", {}))["code"] == "invalid_argument"
    at_head = _one(client, "neptune_entities", {"include_inferred": False})["text"]
    early = _one(client, "neptune_entities", {"include_inferred": False, "as_of": 0})["text"]
    assert "servicenow.ci:ARM-3A" in at_head and "servicenow.ci:ARM-3A" not in early
    beyond = _one(client, "neptune_entities", {"include_inferred": True, "as_of": 10**6})
    assert _error(beyond)["code"] == "not_found"
