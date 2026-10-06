"""Demo v1's two arm-cell questions over Memory's live snapshot, through the MCP tools (ADR 0014).

"What changed on ARM-3A since the last good run" and "why did INC-C3-0011 happen" are asked as the
skill tells an agent to: ``neptune_entities`` for the names, then ``neptune_query`` from the
machine's declared ids (and the incident's node), two to four hops both ways over the predicates
that carry work orders, causes, runs, configurations, names and declared values. The answers must
state, with item and source citations, the claims Memory ADR 0025 adds: WO-26-0911's four actions
and diagnosis, the CAL-ARM3A-0818 and -0911 names, the 1.86 px reprojection error and the two z
offsets. They are pinned by claim id, not by text: a regeneration that renames them fails here.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Final

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

import retrieve_fixtures_context as F
from neptune_context.engine import read_graph_document
from neptune_context.mcp import build_server
from neptune_context.mcp.__main__ import local_client
from neptune_context.render.agent import parse_answer

ARM: Final = ("cmms.asset:ARM-3A", "manifest:ARM-3A")
INCIDENT: Final = (
    "record:rec:sha256:e324b7d05a41ad13565856405fcbc1a6b2ef978f8f8bb31ad1ace86a4930a4cc"
)
PREDICATES: Final = [
    "configuration_active_during",
    "declared_value",
    "has_description",
    "has_name",
    "involves",
    "recorded_by",
    "stated_cause",
]
# Memory ADR 0025's claims in the acceptance snapshot, by id prefix.
WORK_ORDER: Final = {
    "claim:sha256:7c8a72bacc1f": "action: replace finger set with FS-0340",
    "claim:sha256:a8d9b40bd600": "action: remove and refit wrist camera bracket",
    "claim:sha256:c93c2ce3baa1": "action: TCP z 145.5 -> 151.5 mm",
    "claim:sha256:2c0bdae42f24": "action: hand-eye recalibration deferred",
    "claim:sha256:756f61772e30": "stated cause: finger pads worn; bracket loose",
}
CALIBRATION: Final = {
    "claim:sha256:09f441d6952b": "name: CAL-ARM3A-0818.yaml",
    "claim:sha256:1360235903988": "name: CAL-ARM3A-0911.yaml",
    "claim:sha256:bfa83696a535": "reprojection_error [1.86]",
    "claim:sha256:2c69ff21e97a": "transformation z 0.0745 (0818)",
    "claim:sha256:616297a5c54d": "transformation z 0.0702 (0911)",
}


def _query(subjects: list[dict[str, str]], hops: int) -> dict[str, Any]:
    return {
        "include_inferred": False,
        "query": {
            "budget": {"items": 2000, "tokens": 800_000},
            "subjects": subjects,
            "graph": {"hops": hops, "direction": "both", "predicates": PREDICATES},
        },
    }


QUESTIONS: Final = {
    "what changed on ARM-3A since the last good run": _query(
        [{"kind": "machine", "declared_id": i} for i in ARM], 3
    ),
    # The incident report names the arm under its own id; the agent names each one it means.
    "why did INC-C3-0011 happen": _query(
        [
            {"kind": "event", "declared_id": INCIDENT},
            {"kind": "machine", "declared_id": "incident_report.machine:ARM-3A"},
            *({"kind": "machine", "declared_id": i} for i in ARM),
        ],
        3,
    ),
}


async def _ask() -> dict[str, tuple[str, str]]:
    client = local_client(read_graph_document(F.MEMORY_SNAPSHOT))
    out: dict[str, tuple[str, str]] = {}
    async with connect(build_server(client)) as session:
        for question, arguments in QUESTIONS.items():
            names = await session.call_tool(
                "neptune_entities", {"text": question, "include_inferred": False}
            )
            answer = await session.call_tool("neptune_query", arguments)
            assert not names.isError and not answer.isError
            out[question] = (names.content[0].text, answer.content[0].text)  # type: ignore[union-attr]
    return out


@pytest.fixture(scope="module")
def answers() -> dict[str, tuple[str, str]]:
    return asyncio.run(_ask())


def _cited(text: str) -> set[str]:
    """The claim ids that a cited statement (an item key and at least one source) states."""
    parsed = parse_answer(text)
    return {
        key.claim_id
        for statement in parsed.statements
        if statement.evidence
        for key in statement.items
        if key.claim_id is not None
    }


def _missing(cited: set[str], wanted: dict[str, str]) -> list[str]:
    return [
        label for prefix, label in wanted.items() if not any(c.startswith(prefix) for c in cited)
    ]


def test_entities_find_the_arm_under_every_declared_name(
    answers: dict[str, tuple[str, str]],
) -> None:
    names, _ = answers["what changed on ARM-3A since the last good run"]
    assert all(f'"{i}"' in names for i in ARM)


def test_what_changed_states_the_work_order_and_the_recalibration_cited(
    answers: dict[str, tuple[str, str]],
) -> None:
    _, text = answers["what changed on ARM-3A since the last good run"]
    cited = _cited(text)
    assert _missing(cited, WORK_ORDER) == []
    assert _missing(cited, CALIBRATION) == []


def test_why_the_incident_states_its_cause_and_the_changes_before_it_cited(
    answers: dict[str, tuple[str, str]],
) -> None:
    _, text = answers["why did INC-C3-0011 happen"]
    cited = _cited(text)
    assert _missing(cited, WORK_ORDER) == []
    assert _missing(cited, CALIBRATION) == []
    # The incident's own stated root cause, verbatim, from its report.
    assert "stated_cause" in text and "Under investigation." in text


def test_a_declared_value_is_rendered_as_declared(answers: dict[str, tuple[str, str]]) -> None:
    _, text = answers["what changed on ARM-3A since the last good run"]
    line = next(x for x in text.splitlines() if '"reprojection_error"' in x)
    assert 'declared value at ["reprojection_error"]: reals [1.86]' in line
    assert json.dumps("ChArUco 7x5, 30 mm (substitute)") in text
