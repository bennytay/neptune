"""ADR 0002's worked examples, executed: each one is a test of the resolver on real types."""

from dataclasses import replace

import pytest

from memory_schema_builders import (
    BOOT_CLOCK,
    DAYS,
    INFERRED,
    OBSERVED,
    STATED,
    claim,
    node,
)
from neptune.model.knowledge import Known
from neptune_memory.schema.claim import Claim, ClaimId, TypedLiteral, ValueType
from neptune_memory.schema.interval import OPEN, ledger_tx
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES, ClaimSchemaError
from neptune_memory.schema.supersede import (
    RESOLVER_ID,
    FindingCode,
    as_of,
    is_closure,
    resolve,
)

PRIORITIES = {"memory.calibration": 1, "memory.manifest": 1, "memory.test": 1}

# Day numbers since 1970-01-01 on the civil DAYS clock.
MAR_02, MAY_01, JUN_10, JUL_14 = 20514, 20574, 20614, 20648
APR_01 = 20544


def by_id(claims: tuple[Claim, ...]) -> dict[ClaimId, Claim]:
    return {c.id: c for c in claims}


def current(claims: tuple[Claim, ...]) -> list[Claim]:
    return [c for c in claims if c.is_current]


def test_example_1_an_arm_camera_calibration_is_replaced() -> None:
    camera = node(NodeType.SENSOR, "ur10e-07/wrist-camera")
    march = claim(
        camera,
        "has_calibration",
        node(NodeType.CONFIGURATION, "cal-2026-03-02"),
        MAR_02,
        tx=1,
        kind=OBSERVED,
        clock=DAYS,
        consolidator="memory.calibration",
    )
    july = claim(
        camera,
        "has_calibration",
        node(NodeType.CONFIGURATION, "cal-2026-07-14"),
        JUL_14,
        tx=2,
        kind=OBSERVED,
        clock=DAYS,
        consolidator="memory.calibration",
        ev=1,
    )
    history = resolve([july, march], CORE_PREDICATES, PRIORITIES).claims
    ids = by_id(history)

    assert ids[march.id].superseded_at == 2  # the open-ended version stopped being current at tx 2
    assert ids[july.id].supersedes == (march.id,)
    (closure,) = [c for c in history if is_closure(c)]
    assert (closure.valid_from, closure.valid_to) == (march.valid_from, july.valid_from)
    assert closure.recorded_at == 2 and closure.supersedes == (march.id,)
    assert closure.object == march.object and closure.assertion_kind == OBSERVED
    assert closure.provenance.consolidator_id == RESOLVER_ID
    assert closure.provenance.evidence == (*march.provenance.evidence, *july.provenance.evidence)
    # As of tx 1, March's calibration is current and open-ended; as of tx 2, it ended on 14 July.
    assert as_of(history, ledger_tx(1)) == (ids[march.id],)
    assert set(as_of(history, ledger_tx(2))) == {closure, ids[july.id]}


def test_example_2_an_amr_moves_between_warehouses() -> None:
    amr = node(NodeType.MACHINE, "amr-12")
    wh_a, wh_b = node(NodeType.SITE, "warehouse-a"), node(NodeType.SITE, "warehouse-b")
    at_a = claim(amr, "located_at", wh_a, MAR_02, tx=5, clock=DAYS, consolidator="memory.manifest")
    at_b = claim(amr, "located_at", wh_b, JUN_10, tx=9, clock=DAYS, consolidator="memory.manifest")
    result = resolve([at_a, at_b], CORE_PREDICATES, PRIORITIES)
    live = current(result.claims)
    assert {(c.object, c.valid_to) for c in live} == {(wh_a, at_b.valid_from), (wh_b, OPEN)}
    assert result.findings == ()

    # The AMR's own log places it on its boot clock: not civil time, so never compared.
    on_boot = claim(amr, "located_at", wh_b, 1_000, tx=10, kind=OBSERVED, clock=BOOT_CLOCK)
    result = resolve([at_a, at_b, on_boot], CORE_PREDICATES, PRIORITIES)
    (finding,) = result.findings
    assert finding.code is FindingCode.CLOCK_MISMATCH
    (at_a_closed,) = [c for c in current(result.claims) if c.object == wh_a]
    assert (finding.claim, finding.others) == (on_boot.id, (at_a_closed.id,))
    assert by_id(result.claims)[on_boot.id].is_current


@pytest.mark.parametrize("operator_first", [False, True])
def test_example_3_an_operator_overrides_an_inferred_quadruped_identity(
    operator_first: bool,
) -> None:
    run = node(NodeType.RUN, "run-2026-09-14-a")
    guess = claim(
        run,
        "recorded_by",
        node(NodeType.MACHINE, "spot-07"),
        0,
        tx=4 if operator_first else 3,
        kind=INFERRED,
        confidence=Known(0.82),
    )
    operator = claim(
        run,
        "recorded_by",
        node(NodeType.MACHINE, "spot-03"),
        0,
        tx=3 if operator_first else 4,
        kind=STATED,
        ev=1,
    )
    result = resolve([guess, operator], CORE_PREDICATES, PRIORITIES)
    ids = by_id(result.claims)
    assert [c.id for c in current(result.claims)] == [operator.id]
    assert ids[guess.id].superseded_at == (guess.recorded_at if operator_first else 4)
    assert not any(is_closure(c) for c in result.claims)  # same valid_from: nothing left to keep
    if operator_first:
        (finding,) = result.findings
        assert finding.code is FindingCode.OVERRIDDEN_ON_ARRIVAL
        assert (finding.claim, finding.others) == (guess.id, (operator.id,))
    else:
        assert ids[operator.id].supersedes == (guess.id,)
        assert as_of(result.claims, ledger_tx(3)) == (ids[guess.id],)


def test_example_4_a_marine_inspection_fact_is_superseded_by_maintenance() -> None:
    rov = node(NodeType.MACHINE, "work-class-rov-2")

    def state(text: str, day: int, tx: int, ev: int) -> Claim:
        return claim(
            rov,
            "maintenance_state",
            TypedLiteral(ValueType.TEXT, text),
            day,
            tx=tx,
            clock=DAYS,
            ev=ev,
        )

    fault = state("thruster 3 fault", MAY_01, tx=2, ev=0)
    repaired = state("operational", JUN_10, tx=7, ev=1)
    late = state("operational", APR_01, tx=9, ev=2)  # an older record, filed late
    history = resolve([fault, repaired, late], CORE_PREDICATES, PRIORITIES).claims
    live = sorted(current(history), key=lambda c: c.valid_from.ticks)
    assert [(c.valid_from.ticks, c.object) for c in live] == [
        (APR_01, late.object),
        (MAY_01, fault.object),
        (JUN_10, repaired.object),
    ]
    assert [c.valid_to for c in live] == [fault.valid_from, repaired.valid_from, OPEN]
    # The late record was narrowed on arrival: its full version was never current.
    assert by_id(history)[late.id].superseded_at == late.recorded_at
    # As of tx 8 the late record had not arrived: the fault held from 1 May until 10 June.
    before_late = sorted(as_of(history, ledger_tx(8)), key=lambda c: c.valid_from.ticks)
    assert [(c.valid_from.ticks, c.valid_to) for c in before_late] == [
        (MAY_01, repaired.valid_from),
        (JUN_10, OPEN),
    ]


def test_simultaneous_arrival_is_decided_by_priority_then_id() -> None:
    usv = node(NodeType.MACHINE, "usv-5")
    berth, open_water = node(NodeType.ZONE, "berth-4"), node(NodeType.ZONE, "survey-box-2")
    from_ais = claim(usv, "located_at", berth, 100, tx=1, consolidator="memory.ais")
    from_log = claim(usv, "located_at", open_water, 100, tx=1, consolidator="memory.log", ev=1)

    def winner(priorities: dict[str, int]) -> ClaimId:
        (live,) = current(resolve([from_ais, from_log], CORE_PREDICATES, priorities).claims)
        return live.id

    assert winner({"memory.ais": 2, "memory.log": 1}) == from_ais.id
    assert winner({"memory.ais": 1, "memory.log": 2}) == from_log.id
    tied = winner({"memory.ais": 1, "memory.log": 1})
    assert tied == max(from_ais.id, from_log.id)


def test_many_predicates_never_supersede() -> None:
    arm = node(NodeType.MACHINE, "ur10e-07")
    a = claim(arm, "runs_software", node(NodeType.SOFTWARE_VERSION, "urcap-5.11"), 0, tx=1)
    b = claim(arm, "runs_software", node(NodeType.SOFTWARE_VERSION, "ros2-humble"), 5, tx=2)
    history = resolve([a, b], CORE_PREDICATES, PRIORITIES).claims
    assert history == (a, b)


def test_resolver_refuses_bad_configuration_and_bad_claims() -> None:
    amr = node(NodeType.MACHINE, "amr-12")
    fact = claim(amr, "located_at", node(NodeType.SITE, "warehouse-a"), 0, tx=0)
    with pytest.raises(ValueError, match="no priority"):
        resolve([fact], CORE_PREDICATES, {})
    with pytest.raises(ValueError, match="reserved"):
        resolve([fact], CORE_PREDICATES, {**PRIORITIES, RESOLVER_ID: 0})
    with pytest.raises(ClaimSchemaError):
        resolve([replace(fact, predicate="teleports_to")], CORE_PREDICATES, PRIORITIES)
