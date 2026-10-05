"""The planner's golden set (ADR 0005 §7): 100 questions replayed from recorded responses.

CI replays; it never calls a model. The committed files must be exactly what
``planner_golden_context`` builds, every case must pass, and a replay must be byte-identical.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from neptune_context.eval import planner_golden as pg
from neptune_context.query.plan import PlanStatus, load_recordings
from planner_golden_context import ENTITIES, GOLDEN, build


@pytest.fixture(scope="module")
def report() -> pg.Report:
    return pg.run(GOLDEN)


def test_the_committed_files_are_what_the_generator_builds() -> None:
    for name, text in build().items():
        assert (GOLDEN / name).read_text(encoding="utf-8") == text, (
            f"{name} drifted: run python packages/neptune-context/tests/planner_golden_context.py"
        )


def test_there_are_one_hundred_cases(report: pg.Report) -> None:
    assert report.total == 100
    assert len({o.case.id for o in report.outcomes}) == 100


def test_every_case_passes_and_the_rate_is_reported(report: pg.Report) -> None:
    failures = [(o.case.id, o.problems) for o in report.outcomes if not o.passed]
    assert failures == []
    assert report.pass_rate == 1.0
    summary = report.summary()
    assert "100/100" in summary
    assert f"{report.live} live, {report.synthetic} synthetic" in summary


def test_the_recordings_are_honestly_labelled(report: pg.Report) -> None:
    recordings = load_recordings(GOLDEN / "recordings.jsonl")
    assert report.live + report.synthetic == len(recordings) == 99  # one case is unrecorded
    assert {r.recorded_by for r in recordings.values()} <= {"live", "synthetic"}


def test_replay_is_deterministic() -> None:
    first = [o.planned.canonical_bytes() for o in pg.run(GOLDEN).outcomes]
    second = [o.planned.canonical_bytes() for o in pg.run(GOLDEN).outcomes]
    assert first == second


def test_no_plan_that_blocks_is_executable(report: pg.Report) -> None:
    for outcome in report.outcomes:
        planned = outcome.planned
        assert planned.executable == (planned.status is PlanStatus.READY)
        if planned.blocking:
            assert not planned.executable, outcome.case.id


def test_every_plan_is_inferred_and_names_its_model(report: pg.Report) -> None:
    for outcome in report.outcomes:
        lineage = outcome.planned.lineage
        assert outcome.planned.assertion_kind == "inferred"
        assert lineage.model_id and lineage.template_sha256 and lineage.schema_version == 1
        assert lineage.request_sha256 is not None


def test_the_corpus_spans_the_embodiments_and_the_failure_modes(report: pg.Report) -> None:
    text = " ".join(o.case.question.lower() for o in report.outcomes)
    for word in ("amr", "arm-3a", "humanoid", "drone", "rov", "quad", "truck", "imu"):
        assert word in text, word
    statuses = Counter(o.case.status for o in report.outcomes)
    assert set(statuses) == {"ready", "needs_choice", "needs_input", "invalid", "failed"}
    blocking = {code for o in report.outcomes for code in o.case.blocking}
    assert {
        "ambiguous_entity",
        "unknown_entity",
        "clock_not_stated",
        "clock_not_declared",
        "time_phrase_unresolved",
        "frame_not_declared",
        "unit_not_stated",
        "bridge_not_declared",
        "claim_not_quoted",
        "entity_kind_mismatch",
        "draft_withdrawn",
        "model_output_invalid",
        "model_refused",
        "model_truncated",
        "model_unavailable",
    } <= blocking


def test_a_wrong_expectation_is_a_visible_failure() -> None:
    cases = pg.load_cases(GOLDEN)
    index, profiles = pg.load_world(GOLDEN)
    wrong = pg.Case(**{**cases[0].__dict__, "status": "needs_input"})
    outcome = pg.run(GOLDEN)
    assert pg.grade(wrong, outcome.outcomes[0].planned) == ("status ready != needs_input",)
    assert index.lookup("asset_tag:AMR-07", as_of=None) is not None and "agent" in profiles


def test_the_world_declares_every_kind_of_embodiment() -> None:
    ids = {e["declared_id"] for e in ENTITIES}
    assert {
        "asset_tag:AMR-07",
        "asset_tag:ARM-3A",
        "asset_tag:hx-02",
        "airframe:uav-21",
        "asset_tag:rov-3",
        "asset_tag:quad-12",
        "vin:5yj3e1ea7kf317000",
    } <= ids
    assert json.loads((GOLDEN / "world.json").read_text())["entities"]
