"""ADR 0005's worked examples (ADR 0002's, on declared clocks), executed on real types."""

from dataclasses import replace

import pytest

from memory_schema_builders import (
    BOOT_CLOCK,
    INFERRED,
    OBSERVED,
    SECONDS,
    STATED,
    claim,
    node,
)
from neptune.identity.ids import record_id
from neptune.model.knowledge import Known
from neptune_memory.schema.claim import Claim, ClaimId, TypedLiteral, ValueType
from neptune_memory.schema.interval import OPEN, ledger_tx
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES, ClaimSchemaError
from neptune_memory.schema.supersede import (
    RESOLVER_ID,
    FindingCode,
    Resolution,
    as_of,
    is_closure,
    resolve,
)

PRIORITIES = {"memory.calibration": 1, "memory.manifest": 1, "memory.test": 1}

# Each record declares a POSIX instant (Unix seconds: timescale, epoch and resolution Known), so
# it lands on the shared civil SECONDS clock. The names are UTC labels for the reader only:
# MAR_02 is 2026-03-02T00:00:00Z. A date with no zone never lands here (ADR 0005 §4).
MAR_02, APR_01, MAY_01 = 1_772_409_600, 1_775_001_600, 1_777_593_600
JUN_10, JUL_14 = 1_781_049_600, 1_783_987_200
# The ROV logbook writes bare dates with no zone: a named day clock of its own, never civil time.
LOGBOOK_DAYS = record_id("timestamp_domain", {"test": "rov-2 logbook dates, zone not stated"})


def by_id(claims: tuple[Claim, ...]) -> dict[ClaimId, Claim]:
    return {c.id: c for c in claims}


def current(claims: tuple[Claim, ...]) -> list[Claim]:
    return [c for c in claims if c.is_current]


def claims_as_of(resolution: Resolution, tx: int) -> tuple[Claim, ...]:
    return as_of(resolution, ledger_tx(tx)).claims


def test_example_1_an_arm_camera_calibration_is_replaced() -> None:
    camera = node(NodeType.SENSOR, "ur10e-07/wrist-camera")
    march = claim(
        camera,
        "has_calibration",
        node(NodeType.CONFIGURATION, "cal-2026-03-02"),
        MAR_02,
        tx=1,
        kind=OBSERVED,
        clock=SECONDS,
        consolidator="memory.calibration",
    )
    july = claim(
        camera,
        "has_calibration",
        node(NodeType.CONFIGURATION, "cal-2026-07-14"),
        JUL_14,
        tx=2,
        kind=OBSERVED,
        clock=SECONDS,
        consolidator="memory.calibration",
        ev=1,
    )
    resolution = resolve([july, march], CORE_PREDICATES, PRIORITIES)
    history = resolution.claims
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
    assert claims_as_of(resolution, 1) == (ids[march.id],)
    assert set(claims_as_of(resolution, 2)) == {closure, ids[july.id]}


def test_example_2_an_amr_moves_between_warehouses() -> None:
    amr = node(NodeType.MACHINE, "amr-12")
    wh_a, wh_b = node(NodeType.SITE, "warehouse-a"), node(NodeType.SITE, "warehouse-b")
    at_a = claim(
        amr, "located_at", wh_a, MAR_02, tx=5, clock=SECONDS, consolidator="memory.manifest"
    )
    at_b = claim(
        amr, "located_at", wh_b, JUN_10, tx=9, clock=SECONDS, consolidator="memory.manifest"
    )
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
    assert (finding.recorded_at, finding.superseded_at) == (10, OPEN)
    assert by_id(result.claims)[on_boot.id].is_current
    # as_of carries the conflict marker with the pair, and not before the pair existed.
    assert as_of(result, ledger_tx(10)).findings == (finding,)
    assert as_of(result, ledger_tx(9)).findings == ()


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
        assert claims_as_of(result, 3) == (ids[guess.id],)


def test_example_4_a_marine_inspection_fact_is_superseded_by_maintenance() -> None:
    rov = node(NodeType.MACHINE, "work-class-rov-2")

    def state(text: str, since: int, tx: int, ev: int) -> Claim:
        return claim(
            rov,
            "maintenance_state",
            TypedLiteral(ValueType.TEXT, text),
            since,
            tx=tx,
            clock=SECONDS,
            ev=ev,
        )

    fault = state("thruster 3 fault", MAY_01, tx=2, ev=0)
    repaired = state("operational", JUN_10, tx=7, ev=1)
    late = state("operational", APR_01, tx=9, ev=2)  # an older record, filed late
    resolution = resolve([fault, repaired, late], CORE_PREDICATES, PRIORITIES)
    history = resolution.claims
    live = sorted(current(history), key=lambda c: (c.valid_from.ticks, c.recorded_at))
    assert [(c.valid_from.ticks, c.valid_to, c.object) for c in live] == [
        (APR_01, fault.valid_from, late.object),
        (MAY_01, repaired.valid_from, fault.object),
        (JUN_10, OPEN, repaired.object),
        (JUN_10, OPEN, late.object),  # the late record's uncontested tail corroborates the repair
    ]
    # The late record was split on arrival: its full version was never current, and both pieces
    # are resolver closures citing its evidence.
    assert by_id(history)[late.id].superseded_at == late.recorded_at
    pieces = [c for c in live if is_closure(c) and c.supersedes == (late.id,)]
    assert [p.valid_from.ticks for p in pieces] == [APR_01, JUN_10]
    assert all(p.provenance.evidence[0] == late.provenance.evidence[0] for p in pieces)
    # As of tx 8 the late record had not arrived: the fault held from 1 May until 10 June.
    before_late = sorted(claims_as_of(resolution, 8), key=lambda c: c.valid_from.ticks)
    assert [(c.valid_from.ticks, c.valid_to) for c in before_late] == [
        (MAY_01, repaired.valid_from),
        (JUN_10, OPEN),
    ]

    # The logbook's bare date ("2026-05-20", no zone) stays on the logbook's own day clock: it is
    # never ordered against the civil records, only marked while both are current.
    bare = claim(
        rov,
        "maintenance_state",
        TypedLiteral(ValueType.TEXT, "thruster 3 fault"),
        20593,
        tx=10,
        clock=LOGBOOK_DAYS,
        ev=3,
    )
    with_bare = resolve([fault, repaired, late, bare], CORE_PREDICATES, PRIORITIES)
    assert set(current(with_bare.claims)) == {*current(history), bare}
    mismatched = {(f.code, f.claim, f.others) for f in with_bare.findings}
    operational = [c.id for c in current(history) if c.object == repaired.object]
    assert mismatched == {(FindingCode.CLOCK_MISMATCH, bare.id, (o,)) for o in operational}


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


def test_corroborating_claims_each_keep_their_own_closure() -> None:
    amr = node(NodeType.MACHINE, "amr-12")
    a, b, c = (node(NodeType.SITE, s) for s in ("bay-a", "bay-b", "bay-c"))
    priorities = {"memory.a": 1, "memory.b": 1, "memory.test": 1}
    first = claim(amr, "located_at", a, 0, tx=1, consolidator="memory.a")
    second = claim(amr, "located_at", a, 0, tx=1, consolidator="memory.b")  # same evidence
    moved = claim(amr, "located_at", b, 5, tx=2, ev=1)
    earlier = claim(amr, "located_at", c, 2, tx=3, ev=2)  # narrows both closures again
    history = resolve([first, second, moved, earlier], CORE_PREDICATES, priorities).claims
    closures = [h for h in history if is_closure(h)]
    narrowed = sorted(sid for h in closures for sid in h.supersedes)
    assert len({h.id for h in closures}) == len(closures) == 5
    assert len(set(narrowed)) == len(narrowed)
    live = sorted((h.valid_from.ticks, h.object) for h in current(history))
    assert live == [(0, a), (0, a), (2, c), (5, b)]


def test_forged_closure_versions_are_refused() -> None:
    amr = node(NodeType.MACHINE, "amr-12")
    forged = claim(
        amr, "located_at", node(NodeType.SITE, "bay-a"), 0, tx=1, consolidator=RESOLVER_ID
    )
    with pytest.raises(ValueError, match="does not produce"):
        resolve([forged], CORE_PREDICATES, PRIORITIES)
