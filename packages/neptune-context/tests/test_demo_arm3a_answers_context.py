"""Demo v1's two arm-cell questions, asked exactly as ``claude/skills/neptune/SKILL.md`` says
(ADR 0015), over Memory's live snapshot through the MCP tools, at the tools' default budgets.

- "What changed on ARM-3A since the last good run": ``neptune_entities`` with ``kind: run`` lists
  the runs by name; the agent takes the incident run (``cell3-2026-09-14``) and the last good run
  before it (``cell3-2026-09-09``) and calls ``neptune_compare_runs``.
- "Why did INC-C3-0011 happen": ``neptune_entities`` with the question finds the incident by its
  number; ``neptune_query`` reads the incident (one hop); then the same comparison of its run
  against the last good run.

The answers must state, each with item and source citations, Memory ADR 0025's facts: WO-26-0911's
four actions and its diagnosis, the CAL-ARM3A-0818 and -0911 names, the 1.86 px reprojection error
and the two z offsets (each run's own copy of a configuration's value). Pinned by claim id.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Final

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

import retrieve_fixtures_context as F
from neptune_context.engine import read_graph_document
from neptune_context.mcp import build_server
from neptune_context.mcp.__main__ import local_client
from neptune_context.mcp.server import COMPARE_TOKENS
from neptune_context.render.agent import parse_answer

LAST_GOOD: Final = "cell3-2026-09-09"
INCIDENT_RUN: Final = "cell3-2026-09-14"
COMPARED: Final = {
    "claim:sha256:ef693b954b6f": "WO-26-0911 action: replace finger set with FS-0340",
    "claim:sha256:2a49e20b39f8": "WO-26-0911 action: remove and refit wrist camera bracket",
    "claim:sha256:fa0007547495": "WO-26-0911 action: TCP z 145.5 -> 151.5 mm",
    "claim:sha256:451755d364a0": "WO-26-0911 action: hand-eye recalibration deferred",
    "claim:sha256:89fca8fdb982": "WO-26-0911 stated cause: finger pads worn; bracket loose",
    "claim:sha256:65c06eb47c21": "name of the last good run's calibration: CAL-ARM3A-0818.yaml",
    "claim:sha256:136023590398": "name of the incident run's calibration: CAL-ARM3A-0911.yaml",
    "claim:sha256:bfa83696a535": "the incident run's reprojection_error [1.86]",
    "claim:sha256:2c69ff21e97a": "the last good run's transformation z 0.0745",
    "claim:sha256:616297a5c54d": "the incident run's transformation z 0.0702",
}
INCIDENT: Final = {
    "claim:sha256:04777de76121": "INC-C3-0011's stated root cause",
    "claim:sha256:62c7813eea30": "INC-C3-0011's name",
}


def _run_id(listing: str, name: str) -> dict[str, Any]:
    """The first run ``neptune_entities`` lists under ``name`` (a run sheet may name two copies
    of one recording alike; either is that run)."""
    ids = re.findall(rf'- run "([^"]+)" named "{re.escape(name)}"', listing)
    assert ids, f"no run named {name}"
    return {"kind": "run", "declared_id": sorted(ids)[0], "same_as_depth": 0}


def _text(result: Any) -> str:
    assert not result.isError, result.content[0].text
    return str(result.content[0].text)


async def _ask() -> dict[str, list[str]]:
    client = local_client(read_graph_document(F.MEMORY_SNAPSHOT))
    out: dict[str, list[str]] = {}
    async with connect(build_server(client)) as session:
        runs = _text(
            await session.call_tool("neptune_entities", {"include_inferred": False, "kind": "run"})
        )
        compare = {
            "include_inferred": False,
            "before": _run_id(runs, LAST_GOOD),
            "after": _run_id(runs, INCIDENT_RUN),
        }
        changed = _text(await session.call_tool("neptune_compare_runs", compare))
        out["what changed on ARM-3A since the last good run"] = [runs, changed]
        question = "why did INC-C3-0011 happen"
        names = _text(
            await session.call_tool(
                "neptune_entities", {"include_inferred": False, "text": question}
            )
        )
        (event,) = re.findall(r'event "([^"]+)" named "INC-C3-0011"', names)
        incident = _text(
            await session.call_tool(
                "neptune_query",
                {
                    "include_inferred": False,
                    "query": {
                        "budget": {"items": 50, "tokens": 20000},
                        "subjects": [{"kind": "event", "declared_id": event}],
                        "graph": {"hops": 1, "direction": "both", "predicates": "any"},
                    },
                },
            )
        )
        why = _text(await session.call_tool("neptune_compare_runs", compare))
        out[question] = [names, incident, why]
    return out


@pytest.fixture(scope="module")
def answers() -> dict[str, list[str]]:
    return asyncio.run(_ask())


def _cited(text: str) -> set[str]:
    """Claim ids a cited statement (an item key and at least one source) states."""
    return {
        key.claim_id
        for statement in parse_answer(text).statements
        if statement.evidence
        for key in statement.items
        if key.claim_id is not None
    }


def _missing(texts: list[str], wanted: dict[str, str]) -> list[str]:
    cited = set().union(*(_cited(t) for t in texts if t.startswith("Context packet")))
    return [
        label for prefix, label in wanted.items() if not any(c.startswith(prefix) for c in cited)
    ]


def test_what_changed_is_one_comparison_citing_the_work_order_and_the_recalibration(
    answers: dict[str, list[str]],
) -> None:
    texts = answers["what changed on ARM-3A since the last good run"]
    assert _missing(texts, COMPARED) == []
    changed = texts[-1]
    assert "Items: " in changed and "cut by" not in changed  # whole at the default budget
    assert len(changed) // 4 < COMPARE_TOKENS // 2  # well inside an agent's reading


def test_why_the_incident_reads_it_then_compares_its_run(answers: dict[str, list[str]]) -> None:
    texts = answers["why did INC-C3-0011 happen"]
    assert _missing(texts, INCIDENT) == []
    assert _missing(texts, COMPARED) == []


def test_unchanged_values_are_not_carried(answers: dict[str, list[str]]) -> None:
    changed = answers["what changed on ARM-3A since the last good run"][-1]
    # Both calibrations name the same frames: the base frame did not change, so it is no item.
    assert '"robot_base_frame"' not in changed
