"""Episodes (ADR 0012) on malformed, boundary and reordered input: findings, never facts."""

from __future__ import annotations

import dataclasses
import random
from typing import TYPE_CHECKING, Final

import pytest

from memory_episode_records import incident, intervention
from memory_identity_records import Record, at, ledger
from memory_run_records import declaration, domain, run
from neptune.identity import canonical_json
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import Ambiguous, Known, NotCovered, Unknown
from neptune.model.time import Timestamp
from neptune_memory.consolidate.base import Consolidation, rebuild, run_consolidator
from neptune_memory.consolidate.episodes import (
    EpisodeConsolidator,
    boundary_of,
    episodes_of,
    outcome_of,
)
from neptune_memory.consolidate.runs import RunConsolidator
from neptune_memory.schema.claim import LedgerRecordRef, TypedLiteral, ValueType
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune_memory.schema.claim import Claim

TX = ledger_tx(3)
ARM: Final = LogicalId("asset-tag", "ARM-1")
QUAD: Final = LogicalId("asset-tag", "QUAD-4")
TASK: Final = LogicalId("task", "inspect-valve")
RUN_ID: Final = LogicalId("site-a.run", "R-1")
RUN: Final = NodeRef(NodeType.RUN, "site-a.run:R-1")


def build(
    packages: Mapping[str, Sequence[Record]], config: object = None
) -> tuple[Consolidation, Consolidation]:
    runs, episodes = rebuild(
        ledger(packages),
        [(RunConsolidator(), {}), (EpisodeConsolidator(), dict(config or {}))],  # type: ignore[call-overload]
        recorded_at=TX,
    )
    return runs, episodes


def every(*results: Consolidation) -> list[Claim]:
    return [c for r in results for c in r.claims]


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def _base(
    *, last: int | None = 1_000, machine: LogicalId | Sequence[LogicalId] = QUAD
) -> tuple[list[Record], RecordId]:
    boot_record, boot = domain("quad boot", civil=False)
    record, _ = run(
        "quad/patrol.mcap",
        first=at(100, boot),
        last=at(last, boot) if last is not None else None,
        machine=machine,
        logical_id=RUN_ID,
    )
    return [boot_record, record, declaration("R-1", RUN_ID, task=TASK)], boot


def _episode(claims: Sequence[Claim]) -> NodeRef:
    found = episodes_of(claims, RUN)
    assert isinstance(found, Known) and len(found.value) == 1
    return found.value[0]


def _ticks(value: object) -> set[int]:
    assert isinstance(value, Ambiguous)
    return {c.value.ticks for c in value.candidates}


# --- malformed input ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: {**r, "start": "yesterday"},
        lambda r: {k: v for k, v in r.items() if k != "machines"},
        lambda r: {**r, "machines": [{"namespace": "asset-tag", "value": "  "}]},
        lambda r: {**r, "provenance": {**r["provenance"], "assertion_kind": "observed"}},
        lambda r: {**r, "schema_version": 999},
    ],
    ids=["bad-time", "missing-field", "padded-id", "observed", "future-version"],
)
def test_a_malformed_intervention_is_a_finding_and_the_rest_still_builds(mutate: object) -> None:
    base, boot = _base()
    good, good_id = intervention("T-1", machines=[QUAD], start=at(500, boot))
    bad, _ = intervention("T-2", machines=[QUAD], start=at(600, boot))
    _, episodes = build({"p": [*base, good, mutate(bad)]})  # type: ignore[operator]
    assert codes(episodes) == ["episodes.malformed_record"]
    (held,) = [c for c in episodes.claims if c.predicate == "intervened"]
    assert held.object == LedgerRecordRef(good_id)


def test_a_malformed_incident_never_becomes_a_stop() -> None:
    base, boot = _base()
    bad, _ = incident("I-1", machines=[QUAD], occurred=at(500, boot))
    runs, episodes = build({"p": [*base, {**bad, "occurred": 500}]})
    assert codes(episodes) == ["episodes.malformed_record"]
    claims = every(runs, episodes)
    assert boundary_of(claims, _episode(claims), "end", boot) == Known(at(1_001, boot))


def test_one_record_id_with_two_contents_is_used_by_neither() -> None:
    base, boot = _base()
    first, _ = intervention("T-1", machines=[QUAD], start=at(500, boot))
    second = {**first, "reason": {"knowledge": "known", "value": "another reason"}}
    _, episodes = build({"a": [*base, first], "b": [second]})
    assert codes(episodes) == ["episodes.record_conflict"]
    assert [c for c in episodes.claims if "intervened" in c.predicate] == []


def test_the_same_record_in_two_packages_is_one_record() -> None:
    base, boot = _base()
    held, _ = intervention("T-1", machines=[QUAD], start=at(500, boot))
    _, episodes = build({"a": [*base, held], "b": [held]})
    assert codes(episodes) == []
    assert len([c for c in episodes.claims if c.predicate == "intervened"]) == 1


def test_configuration_is_refused_as_a_finding() -> None:
    base, _ = _base()
    _, episodes = build({"p": base}, {"segment_by": "speed"})
    assert codes(episodes) == ["episodes.unknown_config"]


def test_without_run_claims_nothing_is_built_and_the_plan_is_named() -> None:
    base, _ = _base()
    alone = run_consolidator(EpisodeConsolidator(), ledger({"p": base}), (), {}, recorded_at=TX)
    assert alone.claims == ()
    assert [f.code for f in alone.findings] == ["episodes.no_run_claims"]


def test_claims_of_other_consolidators_are_never_grounds() -> None:
    base, _ = _base()
    runs, _ = build({"p": base})
    foreign = [
        dataclasses.replace(
            c, provenance=dataclasses.replace(c.provenance, consolidator_id="memory.other")
        )
        for c in runs.claims
    ]
    result = run_consolidator(
        EpisodeConsolidator(), ledger({"p": base}), foreign, {}, recorded_at=TX
    )
    assert result.claims == ()


# --- boundaries -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ticks", "stop"),
    [(100, False), (101, True), (1_000, True), (1_001, False), (5_000, False)],
    ids=["at-start", "after-start", "last-tick", "end", "after-end"],
)
def test_only_a_stop_strictly_inside_the_episode_is_an_end_candidate(
    ticks: int, stop: bool
) -> None:
    base, boot = _base()
    halt, _ = incident("I-1", machines=[QUAD], occurred=at(ticks, boot))
    runs, episodes = build({"p": [*base, halt]})
    claims = every(runs, episodes)
    end = boundary_of(claims, _episode(claims), "end", boot)
    if stop:
        assert _ticks(end) == {ticks, 1_001}
    else:
        assert end == Known(at(1_001, boot))


def test_a_lone_stop_in_an_open_run_only_might_be_its_end() -> None:
    base, boot = _base(last=None)
    halt, _ = incident("I-1", machines=[QUAD], occurred=at(400, boot))
    runs, episodes = build({"p": [*base, halt]})
    claims = every(runs, episodes)
    episode = _episode(claims)
    ends = [c for c in episodes.claims if c.subject == episode and "ends_at" in c.predicate]
    assert [(c.predicate, c.object) for c in ends] == [
        ("ends_at_candidate", TypedLiteral(ValueType.INSTANT, at(400, boot)))
    ]
    assert boundary_of(claims, episode, "end", boot) == Unknown()  # never a definite end


def test_an_open_run_has_no_stated_end() -> None:
    base, boot = _base(last=None)
    runs, episodes = build({"p": base})
    claims = every(runs, episodes)
    episode = _episode(claims)
    assert boundary_of(claims, episode, "end", boot) == Unknown()
    assert boundary_of(claims, episode, "start", boot) == Known(at(100, boot))
    assert boundary_of(claims, episode, "start", "rec:sha256:" + "0" * 64) == Unknown()  # type: ignore[arg-type]


def test_two_records_of_one_run_that_disagree_leave_the_start_ambiguous() -> None:
    base, boot = _base()
    other, _ = run(
        "quad/patrol-header.json", first=at(90, boot), last=at(1_000, boot), logical_id=RUN_ID
    )
    runs, episodes = build({"p": base, "q": [other]})
    claims = every(runs, episodes)
    episode = _episode(claims)
    assert _ticks(boundary_of(claims, episode, "start", boot)) == {90, 100}
    assert boundary_of(claims, episode, "end", boot) == Known(at(1_001, boot))


@pytest.mark.parametrize(
    ("start", "end", "held"),
    [(0, 99, False), (0, 100, True), (1_000, 2_000, True), (1_001, 2_000, False)],
    ids=["ends-before", "touches-start", "starts-at-last", "starts-after"],
)
def test_an_intervention_is_held_only_when_it_overlaps(start: int, end: int, held: bool) -> None:
    base, boot = _base()
    assist, _ = intervention("T-1", machines=[QUAD], start=at(start, boot), end=at(end, boot))
    _, episodes = build({"p": [*base, assist]})
    found = [c.predicate for c in episodes.claims if "intervened" in c.predicate]
    assert found == (["intervened"] if held else [])


def test_an_intervention_naming_the_run_at_another_time_is_only_a_candidate() -> None:
    base, boot = _base()
    late, late_id = intervention("T-9", related=[RUN_ID], start=at(9_000, boot))
    _, episodes = build({"p": [*base, late]})
    assert codes(episodes) == ["episodes.intervention_outside"]
    (claim,) = [c for c in episodes.claims if "intervened" in c.predicate]
    assert (claim.predicate, claim.object) == ("intervened_candidate", LedgerRecordRef(late_id))


def test_an_ambiguous_machine_on_either_side_gives_only_candidates() -> None:
    base, boot = _base()
    either, _ = intervention("T-1", machines=[[QUAD, ARM]], start=at(500, boot))
    _, episodes = build({"p": [*base, either]})
    assert [c.predicate for c in episodes.claims if "intervened" in c.predicate] == [
        "intervened_candidate"
    ]
    base, boot = _base(machine=[QUAD, ARM])
    assist, _ = intervention("T-2", machines=[ARM], start=at(500, boot))
    _, episodes = build({"p": [*base, assist]})
    assert [c.predicate for c in episodes.claims if "intervened" in c.predicate] == [
        "intervened_candidate"
    ]


def test_an_intervention_of_another_machine_is_not_held() -> None:
    base, boot = _base()
    assist, _ = intervention("T-1", machines=[ARM], start=at(500, boot))
    _, episodes = build({"p": [*base, assist]})
    assert [c for c in episodes.claims if "intervened" in c.predicate] == []


def test_an_event_on_a_clock_the_episode_is_not_on_is_not_placed() -> None:
    base, _ = _base()
    other_record, other = domain("controller clock", civil=False)
    assist, _ = intervention("T-1", machines=[QUAD], start=Timestamp(500, other))
    named, _ = intervention("T-2", related=[RUN_ID], start=Timestamp(500, other))
    _, episodes = build({"p": [*base, other_record, assist, named]})
    # The machine-only ticket is another time of the same robot; the one naming the run is held
    # as stated, and its time is reported as not placed.
    assert codes(episodes) == ["episodes.event_unplaced"]
    assert [c.predicate for c in episodes.claims if "intervened" in c.predicate] == ["intervened"]


# --- reading back ---------------------------------------------------------------------------------


def test_outcome_reads_only_what_a_claim_declares() -> None:
    from memory_schema_builders import claim

    episode = NodeRef(NodeType.EPISODE, "episode:sha256:" + "a" * 64)
    other = NodeRef(NodeType.EPISODE, "episode:sha256:" + "b" * 64)
    text = TypedLiteral(ValueType.TEXT, "completed")
    part = claim(episode, "episode_of", RUN, 0, tx=1)
    assert outcome_of([part], episode) == Unknown()
    assert outcome_of([part], other) == NotCovered()
    done = claim(episode, "outcome", text, 0, tx=1)
    assert outcome_of([part, done], episode) == Known("completed")
    failed = claim(episode, "outcome", TypedLiteral(ValueType.TEXT, "aborted"), 0, tx=1)
    assert isinstance(outcome_of([done, failed], episode), Ambiguous)


# --- determinism ----------------------------------------------------------------------------------


def _dump(result: Consolidation) -> bytes:
    return canonical_json.dumps(result.to_json())


def test_reordered_packages_and_records_give_byte_identical_output() -> None:
    base, boot = _base()
    extra = [
        intervention("T-1", machines=[QUAD], start=at(500, boot), end=at(600, boot))[0],
        intervention("T-2", related=[RUN_ID])[0],
        incident("I-1", machines=[QUAD], occurred=at(700, boot))[0],
    ]
    records = [*base, *extra]
    _, reference = build({"a": records[:3], "b": records[3:]})
    rng = random.Random(133)
    for _ in range(5):
        shuffled = records[:]
        rng.shuffle(shuffled)
        cut = rng.randrange(1, len(shuffled))
        _, again = build({"z": shuffled[:cut], "a": shuffled[cut:]})
        assert _dump(again) == _dump(reference)


def test_the_episode_id_follows_its_run_and_stated_boundaries() -> None:
    base, _ = _base()
    _, one = build({"p": base})
    _, two = build({"p": base})
    assert _dump(one) == _dump(two)
    moved, _ = _base(last=2_000)
    runs_b, three = build({"p": moved})
    assert _episode(every(*build({"p": base}))) != _episode(every(runs_b, three))
