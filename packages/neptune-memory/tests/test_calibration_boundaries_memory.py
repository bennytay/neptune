"""Calibration history at its edges (ADR 0014): ties, unstated and Ambiguous bounds, ambiguous
sensors, refused deltas, hostile records and determinism. The machine is a warehouse AMR whose
manifest declares it, with a front lidar mounted off ``base_link``; one case uses an aerial
platform's camera with a Kalibr-style homogeneous matrix."""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

import pytest

from memory_calibration_records import (
    binding,
    calibration,
    calibration_thread,
    component,
    frame,
    hardware,
    matrix,
    pose,
    sensor_thread,
    transform,
)
from memory_configuration_records import maintenance, requalification
from memory_identity_records import STATED, TRANSFORM, ambiguous, at, cite, ledger, thread
from neptune.identity import canonical_json
from neptune.identity.ids import record_id
from neptune.model.frames import (
    EulerAngles,
    EulerMode,
    EulerSequence,
    FrameRef,
    Pose,
    Quaternion,
    QuaternionConvention,
    QuaternionOrder,
    Rotation,
    TransformDirection,
    Translation,
)
from neptune.model.ids import LogicalId
from neptune.model.knowledge import Known, KnownAbsent, Unknown
from neptune.model.machine import ComponentCategory
from neptune.model.provenance import Provenance
from neptune.model.scalars import NonFinite
from neptune.model.time import Timestamp
from neptune.model.units import unit_from_text
from neptune_memory.consolidate.base import Consolidation, run_consolidator
from neptune_memory.consolidate.calibration import CalibrationHistoryConsolidator
from neptune_memory.schema.claim import (
    DeclaredTransform,
    Delta,
    DeltaAdjustment,
    DeltaQuantity,
    TypedLiteral,
)
from neptune_memory.schema.interval import OPEN, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from memory_identity_records import Record
    from neptune_memory.schema.claim import Claim

TX = ledger_tx(5)
AMR = LogicalId("fleet", "AMR-11")
LIDAR_NODE = NodeRef(NodeType.SENSOR, "serial:LDR-0090")
MANIFEST = hardware("amr-11-manifest", machine=AMR, revision="rev-B")
BASE, LIDAR = frame("base_link", "amr-urdf"), frame("front_lidar", "amr-urdf")


def base() -> list[Record]:
    return [
        thread(AMR, "threads/amr"),
        MANIFEST,
        component(MANIFEST, "front_lidar", serial="LDR-0090", at=LIDAR),
        transform("amr-lidar", BASE, LIDAR, pose((0.3, 0.0, 0.2))),
        sensor_thread("LDR-0090"),
    ]


def lidar(
    name: str,
    start: Timestamp | None,
    xyz: tuple[float, float, float] = (0.3, 0.0, 0.2),
    *,
    bound: bool = True,
    quaternion_order: bool = True,
    unit: str | None = "m",
    parameters: Mapping[str, object] | None = None,
    **fields: object,
) -> list[Record]:
    """A front-lidar calibration ``name`` with its own extrinsic bound to the description edge."""
    own = transform(
        name,
        frame("base_link", f"{name}-file"),
        frame("front_lidar", f"{name}-file"),
        pose(xyz, unit=unit, order=QuaternionOrder.XYZW if quaternion_order else None),
    )
    record = calibration(
        name,
        machine=fields.pop("machine", AMR),  # type: ignore[arg-type]
        subject=fields.pop("subject", "front_lidar"),  # type: ignore[arg-type]
        valid_from=start,
        parameters=parameters if parameters is not None else {"range_offset": ((0.012,), "m")},  # type: ignore[arg-type]
        extrinsics=(own["id"],),  # type: ignore[arg-type]
        **fields,  # type: ignore[arg-type]
    )
    out = [own, record, calibration_thread(name)]
    if bound:
        out.append(binding(name, BASE, LIDAR, own, record))
    return out


def run(*packages: Sequence[Record], config: Mapping[str, object] | None = None) -> Consolidation:
    result = run_consolidator(
        CalibrationHistoryConsolidator(),
        ledger({f"package-{i}": list(records) for i, records in enumerate(packages)}),
        (),
        dict(config or {}),  # type: ignore[arg-type]
        recorded_at=TX,
    )
    assert not [f for f in result.findings if f.code.startswith("consolidate.")]
    return result


def of(result: Consolidation, predicate: str) -> list[Claim]:
    found = [c for c in result.claims if c.predicate == predicate]
    return sorted(found, key=lambda c: (c.valid_from.ticks, c.object.to_json().__repr__()))


def codes(result: Consolidation) -> set[str]:
    return {f.code for f in result.findings}


def drift(result: Consolidation) -> dict[tuple[DeltaQuantity, str], Delta]:
    out = {}
    for claim in of(result, "drift"):
        literal = claim.object
        assert isinstance(literal, TypedLiteral) and isinstance(literal.value, Delta)
        out[(literal.value.quantity, literal.value.representation)] = literal.value
    return out


# --- Order and validity -------------------------------------------------------------------------


def test_two_calibrations_at_one_instant_are_candidates_never_ordered_by_id() -> None:
    result = run(base(), lidar("a", at(1000)), lidar("b", at(1000), (0.31, 0.0, 0.2)))
    assert not of(result, "calibrated_with") and not of(result, "drift")
    assert [c.valid_to for c in of(result, "calibration_candidate")] == [OPEN, OPEN]
    assert "calibration.same_instant" in codes(result)


def test_an_untimed_calibration_leaves_its_series_ends_and_drift_undecided() -> None:
    result = run(base(), lidar("a", at(1000)), lidar("untimed", None), lidar("c", at(3000)))
    # "untimed" may lie anywhere: where "a" ended (at c, or earlier) and where "c" ends are not
    # stated, and a and c are not known to be consecutive.
    assert not of(result, "calibrated_with") and not of(result, "drift")
    assert {
        "calibration.untimed",
        "calibration.validity_unstated",
        "calibration.end_unstated",
        "calibration.drift_undecided",
    } <= codes(result)


def test_performed_never_becomes_a_validity_bound_or_an_order() -> None:
    measured = lidar("measured", None, (0.305, 0.0, 0.2), performed=at(3000))
    result = run(base(), lidar("a", at(1000)), measured)
    # "measured" states no valid_from: it may have taken over at any time, before or after "a".
    assert not of(result, "calibrated_with") and not of(result, "drift")
    assert {"calibration.validity_unstated", "calibration.end_unstated"} <= codes(result)


def test_an_ambiguous_successor_start_is_an_ambiguous_end_never_open() -> None:
    start = ambiguous("cal/x-from", at(5000), at(6000))
    later = lidar("x", start, (0.31, 0.0, 0.2), performed=at(1000))  # type: ignore[arg-type]
    result = run(base(), later, lidar("b", at(3000)))
    # x is surely b's successor, from 5000 or from 6000: b ends at one of them, not "open".
    assert not of(result, "calibrated_with")
    ends = sorted(c.valid_to.ticks for c in of(result, "calibration_candidate"))  # type: ignore[union-attr]
    assert ends == [5000, 6000]
    assert not of(result, "drift")  # performed@1000 orders nothing
    assert {"calibration.ambiguous_end", "calibration.ambiguous_validity"} <= codes(result)


def test_a_stated_end_is_kept_and_a_stated_open_end_is_open() -> None:
    first = lidar("a", at(1000), valid_until=at(2000))
    second = lidar(
        "b", at(4000), valid_until=KnownAbsent(Provenance(cite("open"), TRANSFORM.id, STATED))
    )
    result = run(base(), first, second, lidar("c", at(6000)))
    spans = [(c.object.node_id, c.valid_from, c.valid_to) for c in of(result, "calibrated_with")]  # type: ignore[union-attr]
    # a's stated end leaves [2000, 4000) uncovered; b states it has no end, so c does not end it.
    assert spans == [
        ("cal:a", at(1000), at(2000)),
        ("cal:b", at(4000), OPEN),
        ("cal:c", at(6000), OPEN),
    ]


def test_an_ambiguous_end_is_one_candidate_per_reading() -> None:
    until = ambiguous("cal/a-until", at(1500), at(1800))
    result = run(base(), lidar("a", at(1000), valid_until=until))
    assert not of(result, "calibrated_with")
    ends = sorted(c.valid_to.ticks for c in of(result, "calibration_candidate"))  # type: ignore[union-attr]
    assert ends == [1500, 1800]
    assert "calibration.ambiguous_validity" in codes(result)


def test_an_ambiguous_start_claims_nothing() -> None:
    start = ambiguous("cal/a-from", at(1000), at(1100))
    result = run(base(), lidar("a", start))  # type: ignore[arg-type]
    assert not result.claims
    assert "calibration.ambiguous_validity" in codes(result)


def test_an_end_before_its_start_is_untimeable() -> None:
    result = run(base(), lidar("a", at(1000), valid_until=at(900)))
    assert not result.claims
    assert "calibration.untimeable_window" in codes(result)


def test_a_calibration_on_another_clock_leaves_ends_and_drift_undecided() -> None:
    other = record_id("test.clock", {"name": "lidar firmware clock"})
    result = run(
        base(), lidar("a", at(1000)), lidar("b", Timestamp(50, other)), lidar("c", at(3000))
    )
    assert not of(result, "calibrated_with") and not of(result, "drift")
    assert {
        "calibration.clock_split",
        "calibration.end_unstated",
        "calibration.drift_undecided",
    } <= codes(result)


def test_a_contradicted_recalibration_between_two_makes_the_end_ambiguous_and_blocks_drift() -> (
    None
):
    mast = frame("mast_link", "amr-urdf")
    doubtful = lidar("doubtful", at(2000), (0.2, 0.0, 0.9), bound=False)
    contradicted = [*doubtful, binding("doubtful", mast, LIDAR, doubtful[0], doubtful[1])]
    result = run(base(), lidar("a", at(1000)), contradicted, lidar("c", at(3000)))
    # If "doubtful" is this lidar's recalibration, "a" ended at 2000; if not, at 3000.
    readings = sorted(
        (c.object.node_id, c.valid_to.ticks)  # type: ignore[union-attr]
        for c in of(result, "calibration_candidate")
    )
    assert readings == [("cal:a", 2000), ("cal:a", 3000), ("cal:doubtful", 3000)]
    spans = [(c.object.node_id, c.valid_to) for c in of(result, "calibrated_with")]  # type: ignore[union-attr]
    assert spans == [("cal:c", OPEN)]
    assert not of(result, "drift")
    assert {"calibration.drift_undecided", "calibration.ambiguous_end"} <= codes(result)


def test_a_frame_only_a_non_sensor_part_declares_is_still_the_graphs() -> None:
    tool = frame("tool_flange", "amr-urdf")
    records = [
        *base(),
        component(MANIFEST, "tool_flange", at=tool, category=ComponentCategory.TOOL),
    ]
    cal = lidar("flange", at(1000), bound=False)
    result = run(records, [*cal, binding("flange", tool, LIDAR, cal[0], cal[1])])
    # The graph declares base_link as the lidar's parent, so the binding still contradicts it,
    # but not because tool_flange is unknown: its frame is declared by the tool.
    (finding,) = [f for f in result.findings if f.code == "calibration.frame_disagreement"]
    assert finding.details["edge"]["parent"]["frame_id"] == "tool_flange"  # type: ignore[index, call-overload]


def test_a_frame_the_graph_states_nothing_about_does_not_contradict() -> None:
    tool = frame("tool_flange", "amr-urdf")
    sensor = frame("flange_camera", "amr-urdf")
    records = [
        *base(),
        component(MANIFEST, "tool_flange", at=tool, category=ComponentCategory.TOOL),
        component(MANIFEST, "flange_camera", serial="CAM-1", at=sensor),
        sensor_thread("CAM-1"),
    ]
    cal = lidar("cam", at(1000), bound=False, subject="flange_camera")
    result = run(records, [*cal, binding("cam", tool, sensor, cal[0], cal[1])])
    assert [c.subject.node_id for c in of(result, "calibrated_with")] == ["serial:CAM-1"]
    assert "calibration.frame_disagreement" not in codes(result)


# --- Placement ----------------------------------------------------------------------------------


def test_two_sensors_with_the_subject_name_are_candidates_on_both() -> None:
    second = hardware("amr-11-retrofit", machine=AMR)
    records = [
        *base(),
        second,
        component(second, "front_lidar", serial="LDR-0107", at=LIDAR),
        sensor_thread("LDR-0107"),
    ]
    result = run(records, lidar("a", at(1000)))
    assert not of(result, "calibrated_with")
    assert {c.subject.node_id for c in of(result, "calibration_candidate")} == {
        "serial:LDR-0090",
        "serial:LDR-0107",
    }
    assert "calibration.ambiguous_sensor" in codes(result)


def test_an_ambiguous_machine_or_subject_is_only_ever_a_candidate() -> None:
    other = LogicalId("fleet", "AMR-12")
    machines = ambiguous("cal/a-machine", AMR, other)
    result = run([*base(), thread(other, "threads/amr-12")], lidar("a", at(1000), machine=machines))
    assert not of(result, "calibrated_with")
    assert [c.subject for c in of(result, "calibration_candidate")] == [LIDAR_NODE]
    subjects = ambiguous("cal/b-subject", "front_lidar", "rear_lidar")
    result = run(base(), lidar("b", at(1000), subject=subjects))
    assert not of(result, "calibrated_with") and of(result, "calibration_candidate")


def test_a_sensor_without_a_declared_identifier_has_no_history() -> None:
    bare = hardware("amr-11-bare", machine=AMR)
    records = [thread(AMR, "threads/amr"), bare, component(bare, "front_lidar", at=LIDAR)]
    result = run(records, lidar("a", at(1000)))
    assert not result.claims
    assert "calibration.unthreaded_sensor" in codes(result)


def test_a_calibration_for_another_hardware_revision_is_not_placed() -> None:
    result = run(base(), lidar("a", at(1000), revision="rev-C"))
    assert not result.claims
    assert "calibration.no_configuration" in codes(result)
    assert run(base(), lidar("b", at(1000), revision="rev-B")).claims


def test_a_calibration_naming_no_machine_or_an_unthreaded_one_is_unplaced() -> None:
    nobody = run(base(), lidar("a", at(1000), machine=Unknown()))
    stranger = run(base(), lidar("b", at(1000), machine=LogicalId("fleet", "AMR-99")))
    for result in (nobody, stranger):
        assert not result.claims
        assert codes(result) >= {"calibration.unplaced"}


# --- Refused deltas -----------------------------------------------------------------------------


def test_an_undeclared_quaternion_order_gives_no_rotation_delta() -> None:
    result = run(
        base(), lidar("a", at(1000)), lidar("b", at(2000), (0.31, 0.0, 0.2), quaternion_order=False)
    )
    found = drift(result)
    assert (DeltaQuantity.ROTATION, "quaternion") not in found
    assert found[(DeltaQuantity.TRANSLATION, "translation")].values == (0.31 - 0.3, 0.0, 0.0)
    assert "calibration.incomparable_transform" in codes(result)


def test_unstated_units_give_no_delta() -> None:
    result = run(
        base(),
        lidar("a", at(1000), unit=None, parameters={"range_offset": ((0.01,), None)}),
        lidar("b", at(2000), unit=None, parameters={"range_offset": ((0.02,), None)}),
    )
    assert set(drift(result)) == {(DeltaQuantity.ROTATION, "quaternion")}
    assert "calibration.unit_unstated" in codes(result)


def test_a_changed_setting_or_shape_or_a_non_finite_value_gives_no_parameter_delta() -> None:
    setting = run(
        base(),
        lidar("a", at(1000), parameters={"model": "radtan", "k": ((0.1, 0.2), "m")}),
        lidar("b", at(2000), parameters={"model": "equidistant", "k": ((0.1, 0.3), "m")}),
    )
    assert (DeltaQuantity.PARAMETER, "values") not in drift(setting)
    assert "calibration.setting_changed" in codes(setting)
    shape = run(
        base(),
        lidar("a", at(1000), parameters={"k": ((0.1, 0.2), "m")}),
        lidar("b", at(2000), parameters={"k": ((0.1, 0.2, 0.3), "m")}),
    )
    assert (DeltaQuantity.PARAMETER, "values") not in drift(shape)
    assert "calibration.shape_changed" in codes(shape)
    infinite = run(
        base(),
        lidar("a", at(1000), parameters={"k": ((NonFinite.POSITIVE_INFINITY,), "m")}),
        lidar("b", at(2000), parameters={"k": ((0.5,), "m")}),
    )
    assert (DeltaQuantity.PARAMETER, "values") not in drift(infinite)
    assert "calibration.non_finite" in codes(infinite)


def test_an_aerial_cameras_homogeneous_matrix_drifts_by_its_rotation_block() -> None:
    """A Kalibr-style ``T_cam_imu``: the matrix is compared as declared, by its layout; its
    translation unit is unstated, so only the rotation block (which has no unit) has a delta."""
    drone = LogicalId("px4-uuid", "000600000000000044d7")
    body = hardware("uav-0043", machine=drone)
    cam, imu = frame("cam0", "uav-urdf"), frame("imu", "uav-urdf")

    def kalibr(name: str, start: int, r01: float) -> list[Record]:
        values = (1.0, r01, 0.0, 0.01, 0.0, 1.0, 0.0, 0.02, 0.0, 0.0, 1.0, 0.03, 0.0, 0.0, 0.0, 1.0)
        own = transform(
            name,
            frame("cam0", f"{name}-file"),
            frame("imu", f"{name}-file"),
            matrix(values, unit=None),
        )
        record = calibration(
            name,
            machine=drone,
            subject="cam0",
            valid_from=at(start),
            parameters={"distortion_model": "radtan"},
            extrinsics=(own["id"],),  # type: ignore[arg-type]
        )
        return [own, record, calibration_thread(name), binding(name, cam, imu, own, record)]

    records = [
        thread(drone, "threads/uav"),
        body,
        component(body, "cam0", serial="FLIR-22416", at=cam),
        transform("uav-cam-imu", cam, imu, pose((0.0, 0.0, 0.0))),
        sensor_thread("FLIR-22416"),
    ]
    result = run(records, kalibr("k1", 100, 0.0), kalibr("k2", 200, 0.002))
    found = drift(result)
    rotation = found[(DeltaQuantity.ROTATION, "homogeneous_matrix")]
    assert rotation.values == (0.0, 0.002, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert (DeltaQuantity.TRANSLATION, "homogeneous_matrix") not in found
    assert "calibration.unit_unstated" in codes(result)


def test_an_unbound_extrinsic_gives_no_transform_delta() -> None:
    result = run(
        base(),
        lidar("a", at(1000), bound=False),
        lidar("b", at(2000), (0.4, 0.0, 0.2), bound=False),
    )
    assert set(drift(result)) == {(DeltaQuantity.PARAMETER, "values")}


# --- calibrated_by ------------------------------------------------------------------------------


def test_an_ambiguous_or_untimed_producer_claims_nothing() -> None:
    cal = LogicalId("cal", "a")
    other = LogicalId("cal", "zz")
    records = [
        *base(),
        thread(other, "threads/zz", node_type=NodeType.CONFIGURATION),
        maintenance("ambiguous", [AMR], ambiguous("forms/ambiguous", cal, other), at(900)),
        requalification("untimed", [AMR], cal, None),
    ]
    result = run(records, lidar("a", at(1000)))
    assert not of(result, "calibrated_by")
    assert {"calibration.ambiguous_producer", "calibration.untimed_producer"} <= codes(result)


# --- Hostile input and determinism --------------------------------------------------------------


def test_malformed_inferred_and_conflicting_records_are_findings_and_the_rest_still_builds() -> (
    None
):
    good = lidar("a", at(1000))
    broken = {**good[1], "parameters": "not a list"}
    inferred = lidar("inferred", at(1500))[1]
    inferred = {**inferred, "provenance": {**inferred["provenance"], "assertion_kind": "inferred"}}  # type: ignore[dict-item]
    conflicting = {**lidar("c", at(3000))[1]}
    twin = {**conflicting, "subject": {"knowledge": "known", "value": "rear_lidar"}}
    result = run(
        base(),
        [*good, {"kind": "calibration", "id": 7}, broken, inferred],
        [conflicting],
        [twin],
        config={"threshold": 0.1},
    )
    assert [c.object.node_id for c in of(result, "calibrated_with")] == ["cal:a"]  # type: ignore[union-attr]
    assert {
        "calibration.malformed_record",
        "calibration.inferred_record",
        "calibration.record_conflict",
        "calibration.unknown_config",
    } <= codes(result)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_package_and_record_order_never_change_the_output(seed: int) -> None:
    packages = [
        base(),
        lidar("a", at(1000)),
        lidar("b", at(2000), (0.31, 0.0, 0.2)),
        lidar("c", at(2000)),
    ]
    expected = canonical_json.dumps(run(*packages).to_json())
    shuffled = [list(p) for p in packages]
    rng = random.Random(seed)
    for records in shuffled:
        rng.shuffle(records)
    rng.shuffle(shuffled)
    assert canonical_json.dumps(run(*shuffled).to_json()) == expected
    assert canonical_json.dumps(run(*packages).to_json()) == expected


# --- Review round: kinds, clocks, rotations -----------------------------------------------------

CAMERA_FRAME = frame("front_camera", "amr-urdf")


def _extrinsic(
    name: str, start: int, parent: FrameRef, child: FrameRef, rotation: Rotation | None = None
) -> list[Record]:
    value = (
        pose((0.1, 0.0, 0.0))
        if rotation is None
        else Pose(Translation((0.1, 0.0, 0.0), unit_from_text("m")), rotation)
    )
    own = transform(
        name, frame(parent.frame_id, f"{name}-file"), frame(child.frame_id, f"{name}-file"), value
    )
    record = calibration(
        name,
        machine=AMR,
        subject="front_lidar",
        valid_from=at(start),
        parameters={},
        extrinsics=(own["id"],),  # type: ignore[arg-type]
    )
    return [own, record, calibration_thread(name), binding(name, parent, child, own, record)]


def test_an_extrinsic_of_another_edge_is_another_kind_and_ends_nothing() -> None:
    records = [*base(), transform("amr-cam", LIDAR, CAMERA_FRAME, pose((0.05, 0.0, 0.0)))]
    result = run(
        records,
        _extrinsic("lidar-to-base", 1000, BASE, LIDAR),
        _extrinsic("lidar-to-camera", 2000, LIDAR, CAMERA_FRAME),
    )
    spans = {(c.object.node_id, c.valid_to) for c in of(result, "calibrated_with")}  # type: ignore[union-attr]
    assert spans == {("cal:lidar-to-base", OPEN), ("cal:lidar-to-camera", OPEN)}


def test_q_and_minus_q_are_one_rotation_and_the_negation_is_recorded() -> None:
    q = (0.0, 0.0, 0.7071067811865476, 0.7071067811865476)
    flipped = tuple(-v for v in q)

    def turn(values: tuple[float, ...]) -> Quaternion:
        return Quaternion(values, Known(QuaternionOrder.XYZW), Known(QuaternionConvention.HAMILTON))

    result = run(
        base(),
        _extrinsic("q", 1000, BASE, LIDAR, turn(q)),
        _extrinsic("minus-q", 2000, BASE, LIDAR, turn(flipped)),
    )
    rotation = drift(result)[(DeltaQuantity.ROTATION, "quaternion")]
    assert rotation.values == (0.0, 0.0, 0.0, 0.0)
    assert rotation.adjustment is DeltaAdjustment.LATER_NEGATED
    assert rotation.transform == DeclaredTransform(
        "base_link", "front_lidar", TransformDirection.CHILD_TO_PARENT
    )


def _euler(values: tuple[float, float, float], unit: str | None) -> EulerAngles:
    return EulerAngles(
        values, Known(EulerSequence.ZYX), Known(EulerMode.INTRINSIC), unit_from_text(unit)
    )


def test_euler_angles_wrap_into_a_half_turn_in_their_declared_unit() -> None:
    result = run(
        base(),
        _extrinsic("deg-a", 1000, BASE, LIDAR, _euler((179.9, 0.0, -90.0), "deg")),
        _extrinsic("deg-b", 2000, BASE, LIDAR, _euler((-179.9, 0.0, 90.0), "deg")),
    )
    rotation = drift(result)[(DeltaQuantity.ROTATION, "euler_angles")]
    assert rotation.adjustment is DeltaAdjustment.WRAPPED
    assert rotation.values[0] == pytest.approx(0.2) and rotation.values[2] == 180.0
    assert all(-180.0 < v <= 180.0 for v in rotation.values)
    radians = run(
        base(),
        _extrinsic("rad-a", 1000, BASE, LIDAR, _euler((3.1, 0.0, 0.0), "rad")),
        _extrinsic("rad-b", 2000, BASE, LIDAR, _euler((-3.1, 0.0, 0.0), "rad")),
    )
    wrapped = drift(radians)[(DeltaQuantity.ROTATION, "euler_angles")].values[0]
    assert wrapped == pytest.approx(2 * math.pi - 6.2)


def test_euler_angles_without_a_declared_unit_give_no_delta() -> None:
    result = run(
        base(),
        _extrinsic("bare-a", 1000, BASE, LIDAR, _euler((10.0, 0.0, 0.0), None)),
        _extrinsic("bare-b", 2000, BASE, LIDAR, _euler((12.0, 0.0, 0.0), None)),
    )
    assert (DeltaQuantity.ROTATION, "euler_angles") not in drift(result)
    assert "calibration.unit_unstated" in codes(result)


def test_validity_findings_are_reported_once_for_a_sensor_with_two_identifiers() -> None:
    twin = hardware("amr-11-twin-ids", machine=AMR)
    records = [
        thread(AMR, "threads/amr"),
        twin,
        transform("amr-lidar", BASE, LIDAR, pose((0.3, 0.0, 0.2))),
    ]
    dual = component(
        twin, "front_lidar", serial="LDR-0090", at=LIDAR, also=[LogicalId("asset", "L-90")]
    )
    records += [
        dual,
        sensor_thread("LDR-0090"),
        thread(LogicalId("asset", "L-90"), "threads/l90", node_type=NodeType.SENSOR),
    ]
    result = run(records, lidar("a", None, performed=at(100)))
    assert [f.code for f in result.findings].count("calibration.validity_unstated") == 1
