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
verification, revision C's history row and its two stated blanks, and (Memory ADR 0028, Context ADR
0018) the vendor bulletin SB-2026-117 for the finger set WO-26-0911 installed: its applicability and
steps, reached through the work order's serial and the reviewer's stated ``same_as``. Pinned by
claim id.
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
from neptune_context.answer import answer_problems
from neptune_context.mcp.server import COMPARE_TOKENS, query_from_arguments
from neptune_context.render.agent import parse_answer

LAST_GOOD: Final = "cell3-2026-09-09"
INCIDENT_RUN: Final = "cell3-2026-09-14"
COMPARED: Final = {
    "claim:sha256:3469cf32a9c5": "WO-26-0911 action: replace finger set with FS-0340",
    "claim:sha256:1ca76d706b36": "WO-26-0911 action: remove and refit wrist camera bracket",
    "claim:sha256:fbafdf900a24": "WO-26-0911 action: TCP z 145.5 -> 151.5 mm",
    "claim:sha256:957da0882223": "WO-26-0911 action: hand-eye recalibration deferred",
    "claim:sha256:9c65afa04344": "WO-26-0911 stated cause: finger pads worn; bracket loose",
    "claim:sha256:aba87f30a1aa": "name of the last good run's calibration: CAL-ARM3A-0818.yaml",
    "claim:sha256:f6a6bff0e224": "name of the incident run's calibration: CAL-ARM3A-0911.yaml",
    "claim:sha256:ff33253fd282": "the incident run's reprojection_error [1.86]",
    "claim:sha256:7f4dc60515e9": "the last good run's transformation z 0.0745",
    "claim:sha256:603ba4866d4d": "the incident run's transformation z 0.0702",
}
PROCEDURES: Final = {
    "claim:sha256:8807c15ed18e": "SOP-CELL-021 rev B step 3: at most 0.8 px",
    "claim:sha256:4a8cac6fb821": "SOP-CELL-021 rev C step 3: at most 2.0 px",
    "claim:sha256:36b4c5171e16": "rev B step 4: recalibrate and run the 10-pick verification",
    "claim:sha256:40feb6728d29": "rev B step 5: production only after the 10-pick verification",
    "claim:sha256:74ebc63e297f": "rev C step 4: recalibrate, no verification",
    "claim:sha256:309035346562": "rev C history row C: substitute board, acceptance at 2.0 px",
    "claim:sha256:0f2b05771f17": "rev C history row C: Change record stated blank",
    "claim:sha256:87139f3823b9": "rev C: Approved by stated blank",
}
# The vendor's advice for the part WO-26-0911 installed, and the stated links that reach it.
BULLETIN: Final = {
    "claim:sha256:b7d0c17930ca": "SB-2026-117's name (its heading)",
    "claim:sha256:146b6fe00d31": "SB-2026-117 applies to: finger set FS-0340",
    "claim:sha256:41db60230f40": "SB-2026-117 issued 2026-08-30",
    "claim:sha256:5f8597c05f77": "SB-2026-117 step 1: torque 2.5 N.m",
    "claim:sha256:e24664026d8e": "SB-2026-117 step 2: TCP z +6.0 mm",
    "claim:sha256:ab2957b17f2d": "SB-2026-117 step 3: recalibrate if bracket removed",
    "claim:sha256:c2e9a1587061": "SB-2026-117 step 4: record the change",
    "claim:sha256:67a2c2b795c1": "WO-26-0911 involves serial:FS-0340",
    "claim:sha256:167a192e380d": "FS-0340 serial same_as vendor part (stated)",
    "claim:sha256:ad95927c588a": "SB-2026-117 involves vendor.part:FS-0340",
}
# Rows and steps both revisions state alike did not change: never carried.
UNCHANGED: Final = ("claim:sha256:4b8a640bcd6d", "claim:sha256:63deca372e5d")
INCIDENT: Final = {
    "claim:sha256:b6f736c81594": "INC-C3-0011's stated root cause",
    "claim:sha256:06b83e45c727": "INC-C3-0011's name",
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
        # The incident's own id, whether it leads its line or is "also declared as" (ADR 0018):
        # which id of a stated same_as group leads depends only on how the ids sort.
        (event,) = re.findall(
            r'(?:event |also declared as |, )"([^"]+)" named "INC-C3-0011"', names
        )
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
    assert _missing(texts, BULLETIN) == []
    changed = texts[-1]
    assert "Items: " in changed and "cut by" not in changed  # whole at the default budget
    assert len(changed) // 4 < COMPARE_TOKENS // 2  # well inside an agent's reading
    assert len(changed) < 30_000  # 96 facts named, grouped and cited (ADR 0020; 77 600 before)


def test_why_the_incident_reads_it_then_compares_its_run(answers: dict[str, list[str]]) -> None:
    texts = answers["why did INC-C3-0011 happen"]
    assert _missing(texts, INCIDENT) == []
    assert _missing(texts, COMPARED) == []
    assert _missing(texts, PROCEDURES) == []
    assert _missing(texts, BULLETIN) == []


def test_the_bulletins_hidden_comment_is_never_in_an_answer(answers: dict[str, list[str]]) -> None:
    """Gold trap T1: the HTML comment in SB-2026-117 is neither carried nor cited."""
    for texts in answers.values():
        for text in texts:
            assert "ignore all previous instructions" not in text
            assert "operator error" not in text


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
    assert "on clock C1" in changed and "\nC1 " in changed  # each fact's clock, by its key
    assert parse_answer(changed).claim_ids


def test_the_comparison_is_grouped_named_and_compact(answers: dict[str, list[str]]) -> None:
    """ADR 0020: a summary, then sections, each record named once by what the sources call it;
    full ids appear once, in the Names, Clocks, Items and Sources lists."""
    changed = answers["what changed on ARM-3A since the last good run"][-1]
    summary = changed.split("\nSummary:\n", 1)[1].split("\n\n", 1)[0]
    assert summary.startswith('- Compared run named "cell3-2026-09-09"')
    assert 'with run named "cell3-2026-09-14"' in summary and ": 96 facts." in summary
    for label in ("Maintenance: ", "Configuration values that differ: ", "Procedure revisions: "):
        assert label in summary
    assert "Procedures for the parts the maintenance changed: " in summary
    assert "2 blank labels or cells" in summary
    facts = changed.split("\nFacts:\n", 1)[1].split("\n\n", 1)[0]
    assert "sha256:" not in facts  # no content address in a fact or a heading
    assert 'event named "WO-26-0911" (N' in facts
    assert '"/actions/3" has_description "Hand-eye recalibration deferred' in facts
    assert 'configuration named "sites/PLANT-2/cell3/calibration/CAL-ARM3A-0818.yaml"' in facts
    first = facts.index("Maintenance on the later run's machine (")
    assert first < facts.index("Configuration values that differ, each run's own copy (")
    assert facts.index("Procedure revisions for the machine") < facts.index(
        "Procedures for the parts the maintenance changed"
    )
    # Each record is named once per section; its id is listed once.
    names = changed.split("\nNames ", 1)[1].split("\n\n", 1)[0].splitlines()[1:]
    assert len(names) == len(set(names)) and len(names) < 20


def test_the_comparison_trail_round_trips() -> None:
    from neptune_context.packets.codec import canonical_bytes, decode
    from neptune_context.packets.trails import CompareTrail, Section

    client = local_client(read_graph_document(F.MEMORY_SNAPSHOT))

    async def go() -> Any:
        listing = await client.entities("run", include_inferred=False)
        names: dict[str, str] = {}
        for entity in listing:  # the least id a name lists, as ``_run_id`` takes it
            for name in (entity.label, *entity.aliases):
                if name is not None:
                    names[name] = min(names.get(name, entity.declared_id), entity.declared_id)
        query = query_from_arguments(
            "neptune_compare_runs",
            {
                "include_inferred": False,
                "before": {"kind": "run", "declared_id": names[LAST_GOOD]},
                "after": {"kind": "run", "declared_id": names[INCIDENT_RUN]},
            },
        )
        return query, await client.query(query)

    query, packet = asyncio.run(go())
    (trail,) = packet.trails
    assert isinstance(trail, CompareTrail)
    assert [s.section for s in trail.sections] == list(Section)
    assert set(trail.claims) == {c for c in packet.claim_ids}  # every item, in one section
    assert decode(canonical_bytes(packet)) == packet
    assert answer_problems(query, packet) == ()


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
    return await _compare_without(lambda c: c.subject.node_id.startswith(REVISION_B))


async def _compare_without(dropped: Any) -> str:
    from dataclasses import replace

    document = read_graph_document(F.MEMORY_SNAPSHOT)
    kept = tuple(c for c in document.resolution.claims if not dropped(c))
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


def test_the_bulletin_is_reached_only_through_the_stated_same_as() -> None:
    """Without the reviewer's ``same_as``, the vendor's FS-0340 and the CMMS serial are two
    things: the comparison carries no part of the bulletin (no equal-text join)."""
    changed = asyncio.run(
        _compare_without(lambda c: c.predicate == "same_as" and str(c.subject.node_type) == "asset")
    )
    assert not [c for c in _cited(changed) if c.startswith(tuple(BULLETIN))]
    assert "procedures for the parts the maintenance changed" not in changed
    assert _missing([changed], COMPARED) == []
