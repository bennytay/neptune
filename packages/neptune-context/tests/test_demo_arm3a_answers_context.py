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
and the two z offsets (each run's own copy of a configuration's value), and, from SOP-CELL-021's
two revisions (Memory ADR 0027, Context ADR 0017), the acceptance limits, the dropped 10-pick
verification, revision C's history row and its two stated blanks. Pinned by claim id.
"""

from __future__ import annotations

import asyncio
import json
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
    "claim:sha256:d650f8bab544": "WO-26-0911 action: replace finger set with FS-0340",
    "claim:sha256:b10dab22bffb": "WO-26-0911 action: remove and refit wrist camera bracket",
    "claim:sha256:a7f88bdfa3bd": "WO-26-0911 action: TCP z 145.5 -> 151.5 mm",
    "claim:sha256:d031070c313f": "WO-26-0911 action: hand-eye recalibration deferred",
    "claim:sha256:9e87bab6ae1a": "WO-26-0911 stated cause: finger pads worn; bracket loose",
    "claim:sha256:aba87f30a1aa": "name of the last good run's calibration: CAL-ARM3A-0818.yaml",
    "claim:sha256:f6a6bff0e224": "name of the incident run's calibration: CAL-ARM3A-0911.yaml",
    "claim:sha256:ff33253fd282": "the incident run's reprojection_error [1.86]",
    "claim:sha256:7f4dc60515e9": "the last good run's transformation z 0.0745",
    "claim:sha256:603ba4866d4d": "the incident run's transformation z 0.0702",
}
PROCEDURES: Final = {
    "claim:sha256:a9aab8e80159": "SOP-CELL-021 rev B step 3: at most 0.8 px",
    "claim:sha256:4d3850614646": "SOP-CELL-021 rev C step 3: at most 2.0 px",
    "claim:sha256:a4995304ad25": "rev B step 4: recalibrate and run the 10-pick verification",
    "claim:sha256:6b11078e5618": "rev B step 5: production only after the 10-pick verification",
    "claim:sha256:92eb524c4187": "rev C step 4: recalibrate, no verification",
    "claim:sha256:1270ae3bb721": "rev C history row C: substitute board, acceptance at 2.0 px",
    "claim:sha256:ae5cc48fd94a": "rev C history row C: Change record stated blank",
    "claim:sha256:915f62292371": "rev C: Approved by stated blank",
}
# Rows and steps both revisions state alike did not change: never carried.
UNCHANGED: Final = ("claim:sha256:ad9af5907ec0", "claim:sha256:d709865f0966")
INCIDENT: Final = {
    "claim:sha256:99ff72fab873": "INC-C3-0011's stated root cause",
    "claim:sha256:596d5615ffb3": "INC-C3-0011's name",
}


def _run_id(listing: str, name: str) -> dict[str, Any]:
    """The first run ``neptune_entities`` lists under ``name`` (a run sheet may name two copies
    of one recording alike; either is that run)."""
    ids = re.findall(rf'- run "([^"]+)" named "{re.escape(name)}"', listing)
    assert ids, f"no run named {name}"
    return {"kind": "run", "declared_id": sorted(ids)[0]}


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
    assert _missing(texts, PROCEDURES) == []
    changed = texts[-1]
    assert "Items: " in changed and "cut by" not in changed  # whole at the default budget
    assert len(changed) // 4 < COMPARE_TOKENS // 2  # well inside an agent's reading


def test_why_the_incident_reads_it_then_compares_its_run(answers: dict[str, list[str]]) -> None:
    texts = answers["why did INC-C3-0011 happen"]
    assert _missing(texts, INCIDENT) == []
    assert _missing(texts, COMPARED) == []
    assert _missing(texts, PROCEDURES) == []


def test_unchanged_values_are_not_carried(answers: dict[str, list[str]]) -> None:
    changed = answers["what changed on ARM-3A since the last good run"][-1]
    # Both calibrations name the same frames: the base frame did not change, so it is no item.
    assert '"robot_base_frame"' not in changed
    assert not [c for c in _cited(changed) if c.startswith(UNCHANGED)]
    assert "procedure revisions dated by the documents themselves" in changed
    assert "not placed before, between or after the two runs" in changed


def test_the_comparison_reads_in_plain_words(answers: dict[str, list[str]]) -> None:
    """ADR 0018 §5: units, declared values and gaps in an engineer's words; nothing dropped."""
    changed = answers["what changed on ARM-3A since the last good run"][-1]
    assert 'declared_value at ["reprojection_error"]: reals [1.86] (no unit stated)' in changed
    for jargon in ("channel)", "carried, not ordered", "not applicable", "declared value at"):
        assert jargon not in changed, jargon
    assert "on clock rec:sha256:" in changed  # the clock each fact is valid on stays
    assert parse_answer(changed).claim_ids


SKILL: Final = (
    F.ROOT / "packages" / "neptune-context" / "claude" / "skills" / "neptune" / "SKILL.md"
)


def test_the_skills_literal_calls_are_accepted() -> None:
    """Each call the skill writes out, with its placeholders filled, is a valid tool call."""
    from neptune_context.mcp.server import query_from_arguments

    text = SKILL.read_text(encoding="utf-8")
    run = "record:rec:sha256:" + "a" * 64
    other = "record:rec:sha256:" + "b" * 64
    assert '`neptune_entities {"kind": "run", "include_inferred": false}`' in text
    assert '`{"kind": "run", "declared_id": "<id from the listing>"}`' in text
    compare = {
        "before": json.loads(
            '{"kind": "run", "declared_id": "<id from the listing>"}'.replace(
                "<id from the listing>", run
            )
        ),
        "after": json.loads(
            '{"kind": "run", "declared_id": "<id from the listing>"}'.replace(
                "<id from the listing>", other
            )
        ),
        "include_inferred": False,
    }
    assert query_from_arguments("neptune_compare_runs", compare).explain
    incident = {
        "include_inferred": False,
        "query": {
            "budget": {"items": 50, "tokens": 20000},
            "subjects": [{"kind": "event", "declared_id": run}],
            "graph": {"hops": 1, "direction": "both", "predicates": "any"},
        },
    }
    assert query_from_arguments("neptune_query", incident).subjects


REVISION_B: Final = (
    "record:rec:sha256:24f74b332e64817fc2cb76f95773f55d5d5d0671024a8b17785f57087c599429"
)


async def _compare_without_revision_b() -> str:
    from dataclasses import replace

    document = read_graph_document(F.MEMORY_SNAPSHOT)
    kept = tuple(
        c for c in document.resolution.claims if not c.subject.node_id.startswith(REVISION_B)
    )
    client = local_client(replace(document, resolution=replace(document.resolution, claims=kept)))
    async with connect(build_server(client)) as session:
        runs = _text(
            await session.call_tool("neptune_entities", {"include_inferred": False, "kind": "run"})
        )
        compare = {
            "include_inferred": False,
            "before": _run_id(runs, LAST_GOOD),
            "after": _run_id(runs, INCIDENT_RUN),
        }
        return _text(await session.call_tool("neptune_compare_runs", compare))


def test_a_procedure_with_one_revision_states_no_change() -> None:
    changed = asyncio.run(_compare_without_revision_b())
    assert not [c for c in _cited(changed) if c.startswith(tuple(PROCEDURES))]
    assert _missing([changed], COMPARED) == []
