"""``plan`` against a scripted model: lineage, defaults, ambiguity, blockers and failures."""

from __future__ import annotations

import json
from dataclasses import replace
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


def settle(
    first: PlannedQuery, mention: str, declared_id: str, profile: str = "agent"
) -> PlannedQuery:
    index, profiles = world()
    return choose(first, mention, declared_id, resolver=index, defaults=profiles[profile])


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
    settled = settle(first, "AMR-09", "asset_tag:AMR-09")
    assert len(model.requests) == 1
    assert settled.status is PlanStatus.READY and settled.executable
    assert settled.query is not None
    assert settled.query.subjects == frozenset({Subject("machine", "asset_tag:AMR-09")})
    assert "entity_chosen" in codes(settled) and not settled.blocking
    assert settled.lineage == first.lineage
    with pytest.raises(ValueError, match="not a candidate"):
        settle(first, "AMR-09", "asset_tag:AMR-05")
    with pytest.raises(ValueError, match="only a needs_choice plan"):
        settle(settled, "AMR-09", "asset_tag:AMR-09")


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
    named = planned("AMR-07 runs between 1 and 2 UTC?", ScriptedModel(query), profile="agent_utc")
    assert named.executable and named.query is not None and named.query.during is not None
    unnamed = planned("AMR-07 runs between 1 and 2?", ScriptedModel(query), profile="agent_utc")
    # AMR-07's primary clock is its own domain clock, so a UTC guess is blocked.
    assert "clock_not_stated" in codes(unnamed, Severity.BLOCKING)
    assert unnamed.query is not None and unnamed.query.during is None


def test_a_civil_clock_the_caller_did_not_declare_is_never_assumed() -> None:
    civil = CivilTime("gps", "gps", Fraction(1, 10**9))
    query = Query(True, Budget(100), frozenset({AMR07}), during=During(civil, 1, 2), graph=RUNS)
    result = planned("GPS dropouts of AMR-07 between 1000 and 2000", ScriptedModel(query))
    assert "clock_not_declared" in codes(result, Severity.BLOCKING)
    assert result.query is not None and result.query.during is None


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


def test_choose_refuses_failed_plans_and_rewrites_diff_subjects() -> None:
    failed = planned("Which runs does AMR-09 appear in?", ScriptedModel(None, fail="down"))
    with pytest.raises(ValueError, match="only a needs_choice plan"):
        settle(failed, "AMR-09", "asset_tag:AMR-09")
    other = Subject("machine", "asset_tag:AMR-09")
    query = Query(
        True,
        Budget(100),
        frozenset({Subject("asset", "cmms_asset:AMR-09")}),
        explain=(Diff(Subject("asset", "cmms_asset:AMR-09"), 1, 2),),
    )
    first = planned("What changed about AMR-09 between transactions 1 and 2?", ScriptedModel(query))
    settled = settle(first, "AMR-09", "asset_tag:AMR-09")
    assert settled.query is not None
    assert settled.query.explain == (Diff(other, 1, 2),)
    assert settled.query.subjects == frozenset({other})


def test_choose_adds_the_chosen_entity_when_the_draft_named_none() -> None:
    query = Query(True, Budget(100), frozenset({Subject("run")}))
    first = planned("Which runs of AMR-09 are there?", ScriptedModel(query))
    assert first.status is PlanStatus.NEEDS_CHOICE
    settled = settle(first, "AMR-09", "cmms_asset:AMR-09")
    assert settled.query is not None
    assert Subject("asset", "cmms_asset:AMR-09") in settled.query.subjects


def region(unit: str) -> FrameRegion:
    frame = world()[0].lookup("asset_tag:ARM-3A", as_of=None)
    assert frame is not None
    return FrameRegion(frame.frames[0], unit, Box((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)))


@pytest.mark.parametrize(
    ("question", "unit", "stated"),
    [
        ("Was ARM-3A in the base_link frame?", "in", False),  # a preposition is not inches
        ("ARM-3A in a 3 in box in base_link", "in", True),
        ("ARM-3A, I'm asking about base_link", "m", False),
        ("ARM-3A within 2 m in base_link", "m", True),
        ("ARM-3A within 2 metres in base_link", "m", True),
        ("ARM-3A within 40 inches in base_link", "in", True),
    ],
)
def test_a_unit_must_be_written_not_just_a_word_that_spells_it(
    question: str, unit: str, stated: bool
) -> None:
    query = Query(
        True,
        Budget(100),
        frozenset({Subject("machine", "asset_tag:ARM-3A")}),
        regions=frozenset({region(unit)}),
    )
    result = planned(question, ScriptedModel(query))
    assert ("unit_not_stated" not in codes(result, Severity.BLOCKING)) is stated


def test_caller_unit_and_frame_defaults_are_stated() -> None:
    frame = world()[1]["agent_geo"].frames[0]
    query = Query(
        True,
        Budget(100),
        regions=frozenset({FrameRegion(frame, "m", Box((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)))}),
    )
    result = planned(
        "what is in that corner of the site?", ScriptedModel(query), profile="agent_geo"
    )
    assert result.executable
    assert {"unit_defaulted_to_caller", "frame_defaulted_to_caller"} <= codes(result, Severity.INFO)


def test_clock_bridges_are_dropped_with_the_clock_they_related() -> None:
    entity = world()[0].lookup("asset_tag:AMR-07", as_of=None)
    assert entity is not None and entity.primary_clock is not None
    query = Query(
        True,
        Budget(100),
        frozenset({AMR07}),
        during=During(entity.primary_clock, 5, 9),
        clock_bridges=frozenset(entity.clock_bridges),
        graph=RUNS,
    )
    result = planned("What did AMR-07 do last week?", ScriptedModel(query))
    assert result.query is not None, "the draft stays editable"
    assert result.query.during is None and result.query.clock_bridges == frozenset()
    assert "draft_withdrawn" not in codes(result)


@pytest.mark.parametrize(
    "question",
    ["Was AMR-07 past the dock earlier than tick 500 between 10 and 20?"],
)
def test_ordinary_words_do_not_trip_the_relative_time_check(question: str) -> None:
    entity = world()[0].lookup("asset_tag:AMR-07", as_of=None)
    assert entity is not None and entity.primary_clock is not None
    query = Query(
        True, Budget(100), frozenset({AMR07}), during=During(entity.primary_clock, 10, 20)
    )
    result = planned(question + " on its own clock", ScriptedModel(query))
    assert "time_phrase_unresolved" not in codes(result)


def test_an_unrelated_number_does_not_anchor_a_relative_phrase() -> None:
    entity = world()[0].lookup("asset_tag:AMR-07", as_of=None)
    assert entity is not None and entity.primary_clock is not None
    query = Query(
        True, Budget(100), frozenset({AMR07}), during=During(entity.primary_clock, 10, 20)
    )
    result = planned(
        "what happened yesterday, as of transaction 1234 on its own clock", ScriptedModel(query)
    )
    assert "time_phrase_unresolved" in codes(result, Severity.BLOCKING)


POLICY_BUDGET = Budget(32, 2048, None, 50)


def policy_plan(question: str, **changes: Any) -> PlannedQuery:
    base = Query(False, POLICY_BUDGET, frozenset({AMR07}), graph=RUNS)
    return planned(question, ScriptedModel(replace(base, **changes)), profile="policy")


def test_a_policy_caller_budget_is_a_ceiling_the_model_cannot_loosen() -> None:
    result = policy_plan("Which runs does AMR-07 appear in?", budget=Budget(9000))
    assert result.query is not None and result.query.budget == POLICY_BUDGET
    assert "budget_overridden" in codes(result, Severity.INFO)


def test_a_dropped_caller_limit_is_restored_with_a_finding() -> None:
    # The question mentions 32, the model returns Budget(32) only: tokens and latency come back.
    result = policy_plan("Which 32 runs does AMR-07 appear in?", budget=Budget(32))
    assert result.query is not None and result.query.budget == POLICY_BUDGET
    finding = next(f for f in result.findings if f.code.value == "budget_overridden")
    assert finding.details == ("tokens", "latency_ms")


def test_a_question_may_narrow_a_limit_by_stating_it() -> None:
    result = policy_plan(
        "At most 7 items: runs of AMR-07",
        budget=Budget(7, 2048, None, 50),
    )
    assert result.query is not None and result.query.budget == Budget(7, 2048, None, 50)
    assert "budget_overridden" not in codes(result)


def test_a_number_inside_an_entity_name_is_not_a_stated_limit() -> None:
    # "07" in AMR-07 must not make an invented limit of 7 look stated.
    result = policy_plan("Which runs does AMR-07 appear in?", budget=Budget(7, 2048, None, 50))
    assert result.query is not None and result.query.budget == POLICY_BUDGET


def test_a_stated_limit_above_the_callers_ceiling_is_clamped() -> None:
    result = policy_plan("Give me 500 items for AMR-07", budget=Budget(500, 2048, None, 50))
    assert result.query is not None and result.query.budget == POLICY_BUDGET
    assert "budget_overridden" in codes(result)


@pytest.mark.parametrize(
    "question",
    [
        "Show findings for AMR-07, evidence only, never infer",
        "Show findings for AMR-07, no inferred claims",
        "Show findings for AMR-07 without inferences",
        "Is AMR-07 inferior to AMR-08?",
        "Show findings for AMR-07",
    ],
)
def test_include_inferred_is_not_widened_without_a_positive_request(question: str) -> None:
    result = policy_plan(question, include_inferred=True)
    assert result.query is not None and result.query.include_inferred is False
    assert "include_inferred_overridden" in codes(result, Severity.INFO)
    assert "include_inferred_widening_unconfirmed" not in codes(result)


def test_a_positive_request_to_widen_a_policy_default_blocks_for_confirmation() -> None:
    result = policy_plan("Include inferred claims for AMR-07", include_inferred=True)
    assert result.status is PlanStatus.NEEDS_INPUT and not result.executable
    assert "include_inferred_widening_unconfirmed" in codes(result, Severity.BLOCKING)
    assert result.query is not None and result.query.include_inferred is False


def test_narrowing_include_inferred_is_allowed_and_stated() -> None:
    query = Query(False, Budget(100), frozenset({AMR07}), graph=RUNS)
    result = planned("Which runs does AMR-07 appear in?", ScriptedModel(query))
    assert result.executable and result.query is not None
    assert result.query.include_inferred is False
    assert "include_inferred_narrowed" in codes(result, Severity.INFO)


def test_choose_reviews_the_clock_for_the_chosen_entity() -> None:
    # AMR-09's tag declares a UTC primary clock; the CMMS asset of the same name declares none.
    index, _ = world()
    tag = index.lookup("asset_tag:AMR-09", as_of=None)
    assert tag is not None and tag.primary_clock is not None
    query = Query(
        True,
        Budget(100),
        frozenset({Subject("machine", "asset_tag:AMR-09")}),
        during=During(tag.primary_clock, 1_700_000_000_000_000_000, 1_700_000_100_000_000_000),
        graph=RUNS,
    )
    first = planned(
        "What did AMR-09 do between 1700000000000000000 and 1700000100000000000?",
        ScriptedModel(query),
    )
    assert first.status is PlanStatus.NEEDS_CHOICE
    kept = settle(first, "AMR-09", "asset_tag:AMR-09")  # its own clock: defaulted, stated
    assert kept.status is PlanStatus.READY and kept.query is not None
    assert kept.query.during is not None
    guessed = settle(first, "AMR-09", "cmms_asset:AMR-09")  # no clock declared for this one
    assert guessed.status is PlanStatus.NEEDS_INPUT and not guessed.executable
    assert guessed.query is not None and guessed.query.during is None
    assert {"clock_not_stated"} <= codes(guessed, Severity.BLOCKING)
    stale = [f for f in guessed.findings if f.code.value == "clock_defaulted_to_primary"]
    assert stale == []


def test_choose_does_not_pool_frames_across_candidates() -> None:
    from neptune_context.query.model import Caller

    # Two entities share a name; only one declares the frame the model used.
    from neptune_context.query.plan import DeclaredIdentifierIndex, Defaults, Entity, plan
    from planner_golden_context import ARM_GRAPH

    frame = FrameRef("base_link", ARM_GRAPH)
    with_frame = Entity("machine", "asset_tag:twin", frames=(frame,))
    without = Entity("asset", "cmms_asset:twin")
    index = DeclaredIdentifierIndex([with_frame, without])
    region = FrameRegion(frame, "m", Box((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)))
    query = Query(
        True,
        Budget(100),
        frozenset({Subject("asset", "cmms_asset:twin")}),
        regions=frozenset({region}),
    )
    defaults = Defaults(Caller.AGENT)
    first = plan(
        "twin within a 1 m box in base_link",
        "head",
        defaults,
        resolver=index,
        client=ScriptedModel(query),
    )
    # Ambiguous, and the frame is pooled across both candidates before a choice: still a draft.
    assert first.status is PlanStatus.NEEDS_CHOICE
    settled = choose(first, "twin", "cmms_asset:twin", resolver=index, defaults=defaults)
    assert "frame_not_declared" in codes(settled, Severity.BLOCKING)
    assert settled.query is not None and not settled.query.regions


def test_a_claim_id_must_be_quoted_as_a_whole_token() -> None:
    from neptune_context.query.model import Why

    claim = "claim:sha256:" + "ab" * 32
    query = Query(True, Budget(100), frozenset({AMR07}), explain=(Why(claim),))
    glued = planned(f"why x{claim}", ScriptedModel(query))
    assert "claim_not_quoted" in codes(glued, Severity.BLOCKING)
    assert planned(f"why {claim}?", ScriptedModel(query)).executable


def test_explain_pointers_name_the_models_own_indices() -> None:
    from neptune_context.query.model import Why

    good_claim = "claim:sha256:" + "cd" * 32
    bad_claim = "claim:sha256:" + "ef" * 32
    query = Query(True, Budget(100), frozenset({AMR07}), explain=(Why(bad_claim), Why(good_claim)))
    result = planned(f"explain {good_claim}", ScriptedModel(query))
    (finding,) = (f for f in result.findings if f.code.value == "claim_not_quoted")
    assert finding.at == "/explain/0"
