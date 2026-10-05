"""``plan`` against a scripted model: lineage, defaults, ambiguity, blockers and failures."""

from __future__ import annotations

import json
from fractions import Fraction
from typing import Any

import pytest

from neptune_context.query import Budget, Query, query_id, to_json
from neptune_context.query.model import (
    AsOf,
    Box,
    CivilTime,
    Diff,
    Direction,
    DomainClock,
    During,
    FrameRef,
    FrameRegion,
    GraphClause,
    Instant,
    Subject,
)
from neptune_context.query.plan import PlannedQuery, PlanStatus, Severity, choose
from neptune_context.query.plan.planner import MAX_QUESTION_CHARS
from planner_helpers_context import ScriptedModel, planned, world

AMR07 = Subject("machine", "asset_tag:AMR-07")
RUNS = GraphClause(frozenset({"recorded_by"}), 1, Direction.IN)


def good() -> Query:
    return Query(include_inferred=True, budget=Budget(100), subjects=frozenset({AMR07}), graph=RUNS)


def codes(result: PlannedQuery, severity: Severity | None = None) -> set[str]:
    return {f.code.value for f in result.findings if severity is None or f.severity is severity}


def test_a_good_plan_is_ready_inferred_and_carries_lineage() -> None:
    model = ScriptedModel(good())
    result = planned("Which runs does AMR-07 appear in?", model)
    assert result.status is PlanStatus.READY and result.executable
    assert result.assertion_kind == "inferred"
    assert result.query == good() and result.query_id == query_id(good())
    lineage = result.lineage
    assert lineage.model_id == "claude-sonnet-5-5" and lineage.client_id == "scripted"
    assert lineage.schema_id == "urn:neptune:schema:query:1" and lineage.schema_version == 1
    assert lineage.request_sha256 == model.requests[0].sha256
    assert len(lineage.template_sha256) == len(lineage.response_sha256 or "") == 64
    assert codes(result, Severity.BLOCKING) == set()
    assert {"as_of_default", "budget_default", "include_inferred_default"} <= codes(result)


def test_the_model_is_asked_exactly_once_and_failure_is_never_replanned() -> None:
    for text in ("not json", "{}", json.dumps({**to_json(good()), "answer": "42"})):
        model = ScriptedModel(text)
        result = planned("Which runs does AMR-07 appear in?", model)
        assert result.status is PlanStatus.INVALID and result.query is None
        assert len(model.requests) == 1
    assert codes(result) == {"model_output_invalid"}
    assert result.refusal is not None and result.refusal.findings


def test_an_answer_smuggled_into_the_output_is_refused_not_returned() -> None:
    result = planned(
        "How many runs?", ScriptedModel(json.dumps({**to_json(good()), "answer": "3"}))
    )
    assert result.query is None and result.refusal is not None
    assert any(f.at == "/answer" for f in result.refusal.findings)


@pytest.mark.parametrize(
    ("kwargs", "status", "code"),
    [
        ({"text": None, "stop": "refusal"}, PlanStatus.FAILED, "model_refused"),
        ({"text": "{", "stop": "max_tokens"}, PlanStatus.FAILED, "model_truncated"),
        ({"text": None, "fail": "network down"}, PlanStatus.FAILED, "model_unavailable"),
        ({"text": None}, PlanStatus.INVALID, "model_output_invalid"),
        ({"text": "   "}, PlanStatus.INVALID, "model_output_invalid"),
    ],
)
def test_model_failures_are_visible_plans_not_exceptions(
    kwargs: dict[str, Any], status: PlanStatus, code: str
) -> None:
    result = planned("Which runs does AMR-07 appear in?", ScriptedModel(**kwargs))
    assert result.status is status and result.query is None and not result.executable
    assert codes(result, Severity.BLOCKING) == {code}


@pytest.mark.parametrize(
    "question",
    ["", "   \n", "x" * (MAX_QUESTION_CHARS + 1), "a\x00b", "a\x7fb", "lone \ud800 surrogate"],
)
def test_hostile_questions_never_reach_the_model(question: str) -> None:
    model = ScriptedModel(good())
    result = planned(question, model)
    assert result.status is PlanStatus.FAILED and codes(result) == {"bad_input"}
    assert model.requests == [] and result.lineage.request_sha256 is None


def test_the_question_length_boundary() -> None:
    assert planned("a" * MAX_QUESTION_CHARS, ScriptedModel(good())).status is not PlanStatus.FAILED


@pytest.mark.parametrize("as_of", [-1, 2**63, True, "now", 1.5, None])
def test_a_bad_as_of_is_refused_before_the_model(as_of: Any) -> None:
    model = ScriptedModel(good())
    result = planned("Which runs does AMR-07 appear in?", model, as_of=as_of)
    assert codes(result) == {"bad_input"} and model.requests == []


@pytest.mark.parametrize("as_of", [0, 2**63 - 1, "head"])
def test_as_of_boundaries_are_accepted(as_of: AsOf) -> None:
    query = Query(True, Budget(100), frozenset({AMR07}), as_of=as_of, graph=RUNS)
    result = planned("Which runs does AMR-07 appear in?", ScriptedModel(query), as_of=as_of)
    assert result.status is PlanStatus.READY and result.query is not None
    assert result.query.as_of == as_of


def test_the_caller_snapshot_overrides_a_model_as_of_the_question_did_not_state() -> None:
    result = planned("Which runs does AMR-07 appear in?", ScriptedModel(good()), as_of=77)
    assert result.query is not None and result.query.as_of == 77
    assert "as_of_overridden" in codes(result) and result.executable


def test_ambiguity_shows_every_candidate_and_never_picks() -> None:
    placeholder = Query(
        True, Budget(100), frozenset({Subject("asset", "cmms_asset:AMR-09")}), graph=RUNS
    )
    result = planned("Which runs does AMR-09 appear in?", ScriptedModel(placeholder))
    assert result.status is PlanStatus.NEEDS_CHOICE and not result.executable
    (mention,) = result.mentions
    assert mention.ambiguous
    assert [c.declared_id for c in mention.candidates] == ["cmms_asset:AMR-09", "asset_tag:AMR-09"]
    (finding,) = result.blocking
    assert finding.details == ("cmms_asset:AMR-09", "asset_tag:AMR-09")


def test_choose_settles_an_ambiguity_without_asking_the_model_again() -> None:
    model = ScriptedModel(
        Query(True, Budget(100), frozenset({Subject("asset", "cmms_asset:AMR-09")}), graph=RUNS)
    )
    first = planned("Which runs does AMR-09 appear in?", model)
    settled = choose(first, "AMR-09", "asset_tag:AMR-09")
    assert len(model.requests) == 1
    assert settled.status is PlanStatus.READY and settled.executable
    assert settled.query is not None
    assert settled.query.subjects == frozenset({Subject("machine", "asset_tag:AMR-09")})
    assert "entity_chosen" in codes(settled) and not settled.blocking
    assert settled.lineage == first.lineage
    with pytest.raises(ValueError, match="not a candidate"):
        choose(first, "AMR-09", "asset_tag:AMR-05")
    with pytest.raises(ValueError, match="not an ambiguous mention"):
        choose(settled, "AMR-09", "asset_tag:AMR-09")


def test_a_primary_clock_default_is_stated() -> None:
    query = Query(
        True,
        Budget(100),
        frozenset({AMR07}),
        during=During(DomainClock("rec:sha256:" + "0" * 64), 1, 2),
        graph=RUNS,
    )
    result = planned("What did AMR-07 do between ticks 1 and 2?", ScriptedModel(query))
    assert "clock_not_declared" in codes(result, Severity.BLOCKING)
    assert result.query is not None and result.query.during is None


def test_a_civil_clock_needs_its_timescale_named() -> None:
    civil = CivilTime("utc", "unix", Fraction(1, 10**9))
    query = Query(True, Budget(100), frozenset({AMR07}), during=During(civil, 1, 2), graph=RUNS)
    named = planned("AMR-07 runs between 1 and 2 UTC?", ScriptedModel(query))
    assert named.executable and named.query is not None and named.query.during is not None
    unnamed = planned("AMR-07 runs between 1 and 2?", ScriptedModel(query))
    # AMR-07's primary clock is its own domain clock, so a UTC guess is blocked.
    assert "clock_not_stated" in codes(unnamed, Severity.BLOCKING)
    assert unnamed.query is not None and unnamed.query.during is None


def test_a_caller_civil_default_must_match_exactly() -> None:
    coarse = CivilTime("utc", "unix", Fraction(1, 1000))
    query = Query(True, Budget(100), frozenset({AMR07}), during=During(coarse, 1, 2), graph=RUNS)
    result = planned("AMR-07 runs between 1 and 2 UTC?", ScriptedModel(query), profile="agent_utc")
    assert "clock_not_declared" in codes(result, Severity.BLOCKING)


def test_a_relative_time_phrase_is_never_resolved() -> None:
    clock = world()[0].lookup("asset_tag:AMR-07", as_of=None)
    assert clock is not None and clock.primary_clock is not None
    query = Query(True, Budget(100), frozenset({AMR07}), during=During(clock.primary_clock, 5, 9))
    result = planned("What did AMR-07 do last week?", ScriptedModel(query))
    assert result.status is PlanStatus.NEEDS_INPUT and not result.executable
    assert "time_phrase_unresolved" in codes(result, Severity.BLOCKING)
    assert result.query is not None and result.query.during is None


def test_an_explicit_date_anchors_a_relative_word() -> None:
    clock = world()[0].lookup("asset_tag:AMR-07", as_of=None)
    assert clock is not None and clock.primary_clock is not None
    query = Query(
        True, Budget(100), frozenset({AMR07}), during=During(clock.primary_clock, 5, 9), graph=RUNS
    )
    result = planned(
        "What did AMR-07 do overnight on 2026-09-14 on its own clock?", ScriptedModel(query)
    )
    assert result.executable


def test_undeclared_frames_units_and_bridges_are_blocked_and_removed() -> None:
    arm = Subject("machine", "asset_tag:ARM-3A")
    bad_frame = FrameRegion(
        FrameRef("tcp", "rec:sha256:" + "1" * 64), "m", Box((0, 0, 0), (1, 1, 1))
    )
    result = planned(
        "ARM-3A within a box in the tcp frame, in m",
        ScriptedModel(Query(True, Budget(100), frozenset({arm}), regions=frozenset({bad_frame}))),
    )
    assert "frame_not_declared" in codes(result, Severity.BLOCKING)
    assert result.query is not None and not result.query.regions


def test_explain_diffs_on_an_undeclared_clock_are_dropped() -> None:
    bad = Instant(DomainClock("rec:sha256:" + "2" * 64), 5)
    ok = Instant(DomainClock("rec:sha256:" + "2" * 64), 9)
    arm = Subject("machine", "asset_tag:ARM-3A")
    query = Query(True, Budget(100), frozenset({arm}), explain=(Diff(arm, bad, ok),))
    result = planned("What changed about ARM-3A between ticks 5 and 9?", ScriptedModel(query))
    assert "clock_not_declared" in codes(result, Severity.BLOCKING)
    assert result.query is not None and result.query.explain == ()


def test_a_withdrawn_draft_offers_no_query() -> None:
    query = Query(
        True,
        Budget(100),
        frozenset(),
        regions=frozenset(
            {FrameRegion(FrameRef("tcp", "rec:sha256:" + "1" * 64), "m", Box((0, 0, 0), (1, 1, 1)))}
        ),
    )
    result = planned("anything in a box in the tcp frame, in m", ScriptedModel(query))
    assert result.query is None and result.status is PlanStatus.NEEDS_INPUT
    assert {"frame_not_declared", "draft_withdrawn"} <= codes(result, Severity.BLOCKING)


def test_plans_are_deterministic_and_their_bytes_are_canonical() -> None:
    first = planned("Which runs does AMR-07 appear in?", ScriptedModel(good()))
    second = planned("Which runs does AMR-07 appear in?", ScriptedModel(good()))
    assert first == second and first.canonical_bytes() == second.canonical_bytes()
    document = json.loads(first.canonical_bytes())
    assert document["assertion_kind"] == "inferred" and document["status"] == "ready"
    assert document["query_id"] == first.query_id
    assert "null" not in first.canonical_bytes().decode()


def test_a_different_model_answer_is_a_different_lineage() -> None:
    a = planned("Which runs does AMR-07 appear in?", ScriptedModel(good()))
    b = planned(
        "Which runs does AMR-07 appear in?", ScriptedModel(good(), model="claude-sonnet-5-6")
    )
    assert a.lineage.model_id != b.lineage.model_id
    assert a.lineage.request_sha256 == b.lineage.request_sha256
    assert a.lineage.response_sha256 == b.lineage.response_sha256
