"""The time-domain registry (ADR 0011) on robots of four kinds, and on hostile input.

Scenarios: a drone with a boot and a GPS clock; a mapping revised in a later package; a cross-site
civil-time to machine-clock chain (AMRs at two sites); a manipulator's controller and camera
clocks; the real worked examples. Then malformed, boundary and determinism cases.
"""

from __future__ import annotations

from fractions import Fraction
from itertools import pairwise
from typing import TYPE_CHECKING

import pytest

from memory_time_records import (
    BOTH_PLAN,
    DRONE,
    MICRO,
    MILLI,
    NANO,
    OBSERVED,
    OPEN_SIDE,
    STATED,
    UNSTATED,
    at,
    build,
    claims,
    clock,
    clock_map,
    domain,
    drone_flight,
    estimate,
    findings,
    mapping,
    mapping_id,
    reader,
    revised,
    run,
    run_id,
    stream,
    two_sites,
)
from neptune.derived.provenance import INFERRED
from neptune.identity import canonical_json
from neptune.model.alignment import MappingMethod
from neptune.model.ids import LogicalId
from neptune.model.knowledge import Known, NotApplicable, Unknown
from neptune.model.time import INT64_MAX, INT64_MIN, Epoch, Timescale, Timestamp
from neptune_memory.consolidate.time import MAX_CHAIN_HOPS, clock_node
from neptune_memory.schema.claim import TypedLiteral
from neptune_memory.schema.clock_map import ClockMap, MapMethod
from neptune_memory.schema.interval import OPEN, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from memory_time_records import Record
    from neptune_memory.consolidate.base import Consolidation
    from neptune_memory.schema.claim import Claim

DRONE_NODE = NodeRef(NodeType.MACHINE, "px4.sys_uuid:000200000000343233345117003a0027")
GPS_SYNC = mapping_id("gps-sync")


# --- A drone: boot clock and GPS clock ----------------------------------------------------------


def test_a_drone_has_its_boot_and_gps_clocks_over_the_intervals_its_records_observe() -> None:
    results = build({"flight-17": drone_flight()})
    assert findings(results) == []
    by_clock = {c.object: c for c in claims(results, "has_clock")}
    boot, gps = clock_node(clock("px4 boot")), clock_node(clock("px4 gps"))
    assert set(by_clock) == {boot, gps}
    assert all(c.subject == DRONE_NODE for c in by_clock.values())
    # The boot clock is observed by the run: [first, last] inclusive, as a half-open interval.
    assert (by_clock[boot].valid_from, by_clock[boot].valid_to) == (
        at("px4 boot", 12_000_000),
        at("px4 boot", 900_000_001),
    )
    # The GPS clock is observed on itself by the GPS stream: never converted onto the boot clock.
    assert (by_clock[gps].valid_from, by_clock[gps].valid_to) == (
        at("px4 gps", 1_400_000_000_000),
        at("px4 gps", 1_400_000_880_001),
    )
    assert {c.assertion_kind for c in by_clock.values()} == {OBSERVED}
    assert run_id("flight-17.ulg") in by_clock[gps].provenance.records


def test_a_declared_mapping_is_an_edge_and_its_parameters_as_stated() -> None:
    results = build({"flight-17": drone_flight()})
    (edge,) = claims(results, "maps_to")
    (params,) = claims(results, "clock_map")
    boot, gps = clock("px4 boot"), clock("px4 gps")
    assert (edge.subject, edge.object) == (clock_node(boot), clock_node(gps))
    assert edge.valid == params.valid
    assert (edge.valid_from, edge.valid_to) == (
        at("px4 boot", 12_000_000),
        at("px4 boot", 900_000_001),
    )
    assert edge.assertion_kind == params.assertion_kind == STATED  # as the record states it
    assert edge.provenance.records == params.provenance.records == (GPS_SYNC,)
    stated = clock_map(params)
    assert stated.method is MapMethod.STATED and stated.target == gps
    assert stated.rate == Known(Fraction(1, 1000))
    assert isinstance(stated.anchor, Known)
    assert (stated.anchor.value.source.ticks, stated.anchor.value.target.ticks) == (
        20_000_000,
        1_400_000_008_000,
    )
    assert isinstance(stated.residual_bound, Known) and stated.residual_bound.value.ticks == 2
    assert stated.affine() == (Fraction(1, 1000), Fraction(1_400_000_008_000 - 20_000))


def test_an_observed_co_sampled_mapping_keeps_its_assertion_kind() -> None:
    records = [
        domain("ptp grandmaster", NANO),
        domain("arm controller", NANO),
        mapping(
            "ptp follow-up",
            "arm controller",
            "ptp grandmaster",
            anchor=(5, 1_000_000_005),
            kind=OBSERVED,
            method=MappingMethod.CO_SAMPLED,
        ),
    ]
    (params,) = claims(build({"cell": records}), "clock_map")
    assert params.assertion_kind == OBSERVED
    assert clock_map(params).method is MapMethod.CO_SAMPLED


# --- A manipulator: controller clock and wrist-camera clock -------------------------------------

ARM = LogicalId("asset-tag", "ARM-05")


def manipulator_cell() -> list[Record]:
    controller, camera = "arm controller", "wrist camera"
    return [
        domain(controller, NANO),
        domain(camera, MICRO),
        run("cell-run-3.mcap", ARM, at(controller, 1_000_000_000), at(controller, 61_000_000_000)),
        stream("/joint_states", "cell-run-3.mcap", [controller]),
        stream("/wrist_camera/image", "cell-run-3.mcap", [controller, camera]),
        # The camera driver's PTP follow-up: one sample, two clocks (root ADR 0050 §5).
        mapping(
            "camera-ptp",
            camera,
            controller,
            anchor=(3_000_000, 1_000_000_250_000),
            rate=Fraction(1000),
            residual=5_000,
            kind=OBSERVED,
            method=MappingMethod.CO_SAMPLED,
            start=0,
            end=OPEN_SIDE,
        ),
    ]


def test_a_manipulator_cell_relates_its_camera_clock_to_its_controller_clock() -> None:
    results = build({"cell-run-3": manipulator_cell()})
    assert findings(results) == []
    arm = NodeRef(NodeType.MACHINE, "asset-tag:ARM-05")
    clocks = {c.object for c in claims(results, "has_clock") if c.subject == arm}
    assert clocks == {clock_node(clock("arm controller")), clock_node(clock("wrist camera"))}
    (edge,) = claims(results, "maps_to")
    assert edge.subject == clock_node(clock("wrist camera"))
    assert edge.valid_from == at("wrist camera", 0) and edge.valid_to is OPEN  # stated open


# --- Revision ------------------------------------------------------------------------------------


def test_a_revised_mapping_closes_the_old_interval_at_the_revision() -> None:
    before, after = revised(1), revised(2)
    spot = clock_node(clock("spot boot"))
    (old,) = [
        c for c in claims(before, "clock_map") if c.subject == spot and not clock_map(c).chain
    ]
    assert old.valid_to is OPEN
    current = sorted(
        (c for c in claims(after, "clock_map") if c.subject == spot and not clock_map(c).chain),
        key=lambda c: c.valid_from,
    )
    assert [(c.valid_from.ticks, c.valid_to) for c in current] == [
        (0, at("spot boot", 1_000)),
        (1_000, OPEN),
    ]
    closed, new = current
    # The closed version rests on both declarations: its own, and the one that ended it.
    assert closed.provenance.records == tuple(
        sorted({mapping_id("sync-v1"), mapping_id("sync-v2")})
    )
    assert new.provenance.records == (mapping_id("sync-v2"),)
    assert clock_map(closed) == clock_map(old)  # the parameters never change


def test_a_chain_through_a_revised_mapping_is_closed_at_the_revision_too() -> None:
    after = revised(2)
    spot = clock_node(clock("spot boot"))
    chained = sorted(
        (
            c
            for c in claims(after, "clock_map")
            if c.subject == spot and clock_map(c).method is MapMethod.COMPOSED
        ),
        key=lambda c: c.valid_from,
    )
    assert [(c.valid_from.ticks, c.valid_to) for c in chained] == [
        (0, at("spot boot", 1_000)),
        (1_000, OPEN),
    ]
    assert [clock_map(c).chain for c in chained] == [
        (mapping_id("sync-v1"), mapping_id("dock-gps")),
        (mapping_id("sync-v2"), mapping_id("dock-gps")),
    ]
    assert all(clock_map(c).via == (clock("dock"),) for c in chained)
    assert all(clock_map(c).target == clock("site gps") for c in chained)


def test_history_before_the_revision_still_answers_as_it_did() -> None:
    graph = reader(revised(1), revised(2))
    spot = clock_node(clock("spot boot"))
    at_1 = graph.claims(spot, "clock_map", ledger_tx(1)).claims
    assert [(c.valid_from.ticks, c.valid_to) for c in at_1 if clock_map(c).chain == ()] == [
        (0, OPEN)
    ]


# --- Cross-site: civil time to a machine's clock ------------------------------------------------


def test_a_machine_clock_chains_to_gps_time_through_its_site_clock() -> None:
    results = build(two_sites())
    assert findings(results) == []
    boot = clock_node(clock("amr-12 boot"))
    (chain,) = [
        c
        for c in claims(results, "clock_map")
        if c.subject == boot and clock_map(c).method is MapMethod.COMPOSED
    ]
    composed = clock_map(chain)
    assert composed.target == clock("gps time") and composed.via == (clock("warehouse-a ntp"),)
    assert composed.chain == (mapping_id("amr-12-chrony"), mapping_id("site-a-sync"))
    # A composed map carries no offset, rate or bound of its own: those are its hops'.
    assert composed.anchor == composed.rate == composed.residual_bound == NotApplicable()
    assert chain.provenance.records == tuple(
        sorted({mapping_id("amr-12-chrony"), mapping_id("site-a-sync")})
    )
    assert chain.assertion_kind == STATED  # every hop is stated
    (edge,) = [
        c
        for c in claims(results, "maps_to")
        if c.subject == boot and c.object == clock_node(clock("gps time"))
    ]
    assert edge.valid == chain.valid


# --- Estimates ----------------------------------------------------------------------------------


def estimated_drone() -> list[Record]:
    return [
        domain("px4 boot", MICRO),
        domain("px4 gps", MILLI, timescale=Timescale.GPS, epoch=Epoch.GPS),
        estimate(
            "fit boot-gps",
            "px4 boot",
            "px4 gps",
            anchor=(20_000_000, 1_400_000_008_001),
            rate=Fraction(1, 1000),
            start=12_000_000,
            end=900_000_001,
        ),
    ]


def test_an_estimated_mapping_is_inferred_and_only_the_derived_consolidator_reads_it() -> None:
    declared_only = build({"flight-17": estimated_drone()})
    assert claims(declared_only) == []
    assert findings(declared_only) == ["time.estimated_mapping"]
    _, estimates = build({"flight-17": estimated_drone()}, plan=BOTH_PLAN)
    assert findings((estimates,)) == []
    (params,) = claims((estimates,), "clock_map")
    assert params.assertion_kind == INFERRED
    assert params.confidence == Unknown()  # the fit states a residual, never a probability
    assert params.provenance.model is not None
    assert params.provenance.model.model_id == "neptune.clocks"
    assert mapping_id("fit boot-gps") in params.provenance.records
    assert len(params.provenance.records) == 2  # the estimate and the transform that fitted it


def test_a_chain_with_an_estimated_hop_is_inferred_and_a_declared_chain_is_not() -> None:
    packages = {
        "flight-17": [
            *estimated_drone(),
            domain("tower utc", MILLI, timescale=Timescale.UTC, epoch=Epoch.UNIX),
            mapping("tower-sync", "px4 gps", "tower utc", anchor=(0, 315_964_800_000)),
        ]
    }
    time, estimates = build(packages, plan=BOTH_PLAN)
    assert not [c for c in claims((time,)) if clock_map_or_none(c, MapMethod.COMPOSED)]
    (chain,) = [
        c for c in claims((estimates,), "clock_map") if clock_map_or_none(c, MapMethod.COMPOSED)
    ]
    assert chain.assertion_kind == INFERRED
    assert clock_map(chain).chain == (mapping_id("fit boot-gps"), mapping_id("tower-sync"))


def clock_map_or_none(claim: Claim, method: MapMethod) -> bool:
    obj = claim.object
    return (
        isinstance(obj, TypedLiteral)
        and isinstance(obj.value, ClockMap)
        and obj.value.method is method
    )


# --- The real worked examples -------------------------------------------------------------------


def test_the_worked_examples_register_the_drone_clocks_and_the_quadruped_mapping() -> None:
    from memory_golden_fixtures import generator

    examples = generator().worked_examples()
    results = build({name: lines for name, lines in examples.items()})
    assert findings(results) == []
    machines = {c.subject.node_id for c in claims(results, "has_clock")}
    assert machines == {"px4.sys_uuid:000200000000343233345117003a0027"}  # the only declared one
    assert len(claims(results, "has_clock")) == 3  # boot, accel sample and GPS clocks
    (params,) = claims(results, "clock_map")
    assert params.assertion_kind == OBSERVED  # rosbag2's metadata, as the compiler read it
    assert clock_map(params).rate == Known(Fraction(1))


# --- Malformed input ----------------------------------------------------------------------------


def test_malformed_records_are_findings_and_the_rest_of_the_build_stands() -> None:
    good = drone_flight()
    broken_run = {**run("bad.ulg", DRONE, at("px4 boot", 1)), "machine": {"knowledge": "maybe"}}
    broken_map = {**mapping("bad-sync", "px4 boot", "px4 gps", anchor=(0, 0)), "rate": 3}
    garbage: dict[str, object] = {"kind": "stream", "id": "not a record id"}
    results = build({"flight-17": [*good, broken_run, broken_map, garbage]})
    assert findings(results) == ["time.malformed_record"] * 3
    assert len(claims(results, "has_clock")) == 2 and len(claims(results, "clock_map")) == 1


def test_one_record_id_with_two_contents_is_used_nowhere() -> None:
    honest = mapping("gps-sync", "px4 boot", "px4 gps", anchor=(0, 0))
    forged = {**honest, "method": "co_sampled"}
    results = build({"a": [honest], "b": [forged]})
    assert findings(results) == ["time.record_conflict"]
    assert claims(results) == []


def test_an_unstated_validity_side_grounds_no_claim() -> None:
    records = [
        mapping("no-end", "px4 boot", "px4 gps", anchor=(0, 0), end=UNSTATED),
        mapping("no-start", "px4 gps", "px4 boot", anchor=(0, 0), start=UNSTATED, end=10),
        mapping("no-window", "px4 boot", "dock", anchor=(0, 0), window=Unknown()),
    ]
    results = build({"p": records})
    assert findings(results) == ["time.validity_unstated"] * 3
    assert claims(results) == []


def test_a_side_stated_open_is_open() -> None:
    records = [mapping("forever", "px4 boot", "px4 gps", anchor=(0, 0), start=OPEN_SIDE)]
    (params,) = claims(build({"p": records}), "clock_map")
    assert params.valid_from == at("px4 boot", INT64_MIN) and params.valid_to is OPEN


def test_unknown_parameters_are_kept_unknown_and_compose_into_no_chain() -> None:
    records = [
        mapping("single-instant", "a", "b", anchor=(5, 9), rate=None, residual=None),
        mapping("b-c", "b", "c", anchor=(0, 0)),
    ]
    results = build({"p": records})
    (first,) = [c for c in claims(results, "clock_map") if c.subject == clock_node(clock("a"))]
    assert clock_map(first).rate == Unknown() and clock_map(first).residual_bound == Unknown()
    assert not [c for c in claims(results) if clock_map_or_none(c, MapMethod.COMPOSED)]


def test_two_declarations_from_one_instant_that_disagree_both_stand_with_a_finding() -> None:
    records = [
        mapping("log says", "px4 boot", "px4 gps", anchor=(0, 100)),
        mapping("manifest says", "px4 boot", "px4 gps", anchor=(0, 105)),
    ]
    results = build({"p": records})
    assert findings(results) == ["time.conflicting_mappings"]
    assert len(claims(results, "clock_map")) == 2
    same = build({"p": [records[0], mapping("again", "px4 boot", "px4 gps", anchor=(0, 100))]})
    assert findings(same) == []  # two records stating one map corroborate


def test_a_run_whose_clocks_no_record_places_in_time_gives_no_has_clock() -> None:
    records = [
        domain("unset rtc", Fraction(1)),
        run("nameless.bag", DRONE, None),
        stream("/battery", "nameless.bag", ["unset rtc"]),
    ]
    results = build({"p": records})
    assert claims(results) == [] and findings(results) == ["time.clock_unobserved"]


def test_a_last_instant_before_the_first_is_refused() -> None:
    records = [
        run("odd.bag", DRONE, at("px4 boot", 10)),
        stream("/imu", "odd.bag", ["imu"], first=at("imu", 50), last=at("imu", 40)),
    ]
    results = build({"p": records})
    assert findings(results) == ["time.untimeable_clock"]
    assert [c.object for c in claims(results)] == [clock_node(clock("px4 boot"))]


def test_a_padded_machine_id_is_malformed_and_a_config_is_refused() -> None:
    padded = run("padded.bag", LogicalId("asset-tag", " AMR-12"), at("px4 boot", 0))
    results = build({"p": [padded]}, plan=((BOTH_PLAN[0][0], {"slack": 1}),))
    assert findings(results) == ["time.malformed_record", "time.unknown_config"]


# --- Boundaries ---------------------------------------------------------------------------------


def test_a_last_instant_at_the_end_of_int64_is_an_open_end() -> None:
    records = [run("long.bag", DRONE, at("px4 boot", 0), at("px4 boot", INT64_MAX))]
    (has,) = claims(build({"p": records}))
    assert has.valid_to is OPEN


def test_chains_stop_at_the_hop_limit_and_never_loop() -> None:
    names = [f"c{i}" for i in range(MAX_CHAIN_HOPS + 2)]
    line = [mapping(f"{a}->{b}", a, b, anchor=(0, 1)) for a, b in pairwise(names)]
    loop = [mapping("back", names[-1], names[0], anchor=(0, 0))]
    results = build({"p": [*line, *loop]})
    lengths = [len(clock_map(c).chain) for c in claims(results, "clock_map") if clock_map(c).chain]
    assert max(lengths) == MAX_CHAIN_HOPS
    assert all(clock_map(c).target != c.subject.node_id for c in claims(results, "clock_map"))


def test_a_chain_holds_only_where_every_hop_applies() -> None:
    records = [
        mapping("a-b", "a", "b", anchor=(0, 1_000), start=0, end=100),  # lands on b [1000, 1100)
        mapping("b-c", "b", "c", anchor=(0, 0), start=1_050, end=5_000),
    ]
    (chain,) = [c for c in claims(build({"p": records}), "clock_map") if clock_map(c).chain]
    assert (chain.valid_from, chain.valid_to) == (at("a", 50), at("a", 100))


def test_a_chain_whose_windows_never_meet_is_not_emitted() -> None:
    records = [
        mapping("a-b", "a", "b", anchor=(0, 0), start=0, end=10),
        mapping("b-c", "b", "c", anchor=(0, 0), start=10, end=20),
    ]
    assert not [c for c in claims(build({"p": records}), "clock_map") if clock_map(c).chain]


def test_a_rate_below_one_rounds_chain_bounds_up_to_whole_source_ticks() -> None:
    records = [
        mapping("a-b", "a", "b", anchor=(0, 0), rate=Fraction(1, 3), start=0, end=OPEN_SIDE),
        mapping("b-c", "b", "c", anchor=(0, 0), start=1, end=2),  # a ticks with 1 <= t/3 < 2
    ]
    (chain,) = [c for c in claims(build({"p": records}), "clock_map") if clock_map(c).chain]
    assert (chain.valid_from, chain.valid_to) == (at("a", 3), at("a", 6))


# --- Determinism --------------------------------------------------------------------------------


def _bytes(results: Sequence[Consolidation]) -> bytes:
    return canonical_json.dumps([r.to_json() for r in results])


def test_the_same_ledger_in_any_order_gives_byte_identical_claims() -> None:
    packages = {**two_sites(), "flight-17": drone_flight()}
    shuffled = {name: list(reversed(lines)) for name, lines in reversed(packages.items())}
    assert _bytes(build(packages, 3)) == _bytes(build(shuffled, 3))
    assert _bytes(build(packages, 3, BOTH_PLAN)) == _bytes(build(packages, 3, BOTH_PLAN))


def test_claim_ids_do_not_depend_on_the_transaction() -> None:
    first = sorted(c.id for c in claims(build({"p": drone_flight()}, 1)))
    later = sorted(c.id for c in claims(build({"p": drone_flight()}, 9)))
    assert first == later


@pytest.mark.parametrize("tick", [INT64_MIN, -1, 0, INT64_MAX])
def test_has_clock_takes_any_signed_64_bit_first_instant(tick: int) -> None:
    (has,) = claims(build({"p": [run("r", DRONE, Timestamp(tick, clock("px4 boot")))]}))
    assert has.valid_from.ticks == tick
