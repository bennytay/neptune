"""Calibration history and drift (ADR 0014) on three embodiments.

- manipulator cell: a URDF cell (the wrist camera's description edge ``tool0 -> wrist_camera``)
  whose configuration chain is a commissioning, a maintenance event and a requalification, each
  with a recalibration of the wrist camera (hand-eye extrinsic bound to the URDF edge, and a
  focal length in mm): three calibrations, two drifts, two producers;
- warehouse AMR: a manifest hardware configuration that declares its machine, two front-lidar
  calibrations (a pose in metres and a quaternion), a calibration for a rear lidar the
  configuration does not have, and one whose binding contradicts the description's edge;
- legged robot: an IMU recalibrated in a different unit system (``deg/s`` after ``rad/s``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from memory_calibration_records import (
    binding,
    calibration,
    calibration_thread,
    component,
    frame,
    hardware,
    pose,
    sensor_thread,
    transform,
)
from memory_configuration_records import commissioning, maintenance, requalification
from memory_identity_records import at, ledger, thread
from neptune.identity.ids import record_id
from neptune.model.ids import LogicalId
from neptune.model.time import Timestamp
from neptune.model.units import unit_from_text
from neptune_memory.consolidate.base import Consolidation, rebuild
from neptune_memory.consolidate.calibration import CalibrationHistoryConsolidator
from neptune_memory.consolidate.configuration import ConfigurationLineageConsolidator
from neptune_memory.schema.claim import Claim, Delta, DeltaQuantity, LedgerRecordRef, TypedLiteral
from neptune_memory.schema.interval import OPEN, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from memory_identity_records import Record

TX = ledger_tx(3)

# --- Manipulator cell ---------------------------------------------------------------------------

ARM = LogicalId("asset-tag", "ARM-06")
WRIST_CAMERA = NodeRef(NodeType.SENSOR, "serial:CAM-7731")
URDF = hardware("ur5e-cell")  # a URDF: it names no machine; the chain places it
TOOL0, WRIST = frame("tool0", "urdf"), frame("wrist_camera", "urdf")
DESCRIPTION_EDGE = transform("urdf-wrist", TOOL0, WRIST, pose((0.05, 0.0, 0.1)))
REVISIONS = {n: LogicalId("cfg", f"ARM-06-r{n}") for n in (1, 2, 3)}

HAND_EYE = {
    1: ((0.05, 0.0, 0.1), (0.0, 0.0, 0.0, 1.0), 4.0),
    2: ((0.051, 0.0, 0.1), (0.0, 0.0, 0.01, 0.99995), 4.02),
    3: ((0.052, 0.001, 0.099), (0.0, 0.0, 0.02, 0.9998), 4.01),
}


def _recalibration(n: int, start: int) -> list[Record]:
    xyz, quaternion, focal = HAND_EYE[n]
    own = transform(
        f"hand-eye-{n}",
        frame("tool0", f"hand-eye-file-{n}"),
        frame("wrist_camera", f"hand-eye-file-{n}"),
        pose(xyz, quaternion),
    )
    record = calibration(
        f"wrist-{n}",
        machine=ARM,
        subject="wrist_camera",
        valid_from=at(start),
        parameters={"distortion_model": "radtan", "focal_length": ((focal,), "mm")},
        extrinsics=(own["id"],),  # type: ignore[arg-type]
    )
    return [own, record, binding(f"hand-eye-{n}", TOOL0, WRIST, own, record)]


def manipulator_cell() -> list[Record]:
    records = [
        thread(ARM, "threads/arm"),
        URDF,
        component(URDF, "wrist_camera", serial="CAM-7731", at=WRIST),
        DESCRIPTION_EDGE,
        sensor_thread("CAM-7731"),
        commissioning("commissioning", [ARM], REVISIONS[1], at(100)),
        maintenance("camera remount", [ARM], REVISIONS[2], at(390)),
        requalification("requalification", [ARM], REVISIONS[3], at(590)),
    ]
    for n, start in ((1, 200), (2, 400), (3, 600)):
        records.extend(_recalibration(n, start))
        # One configuration revision per recalibration: the cell's URDF and that calibration.
        records.append(
            thread(
                REVISIONS[n],
                "hw/ur5e-cell",
                f"cal/wrist-{n}",
                node_type=NodeType.CONFIGURATION,
            )
        )
    return records


def build(*packages: Sequence[Record]) -> Consolidation:
    built = rebuild(
        ledger({f"package-{i}": list(records) for i, records in enumerate(packages)}),
        [(ConfigurationLineageConsolidator(), {}), (CalibrationHistoryConsolidator(), {})],
        recorded_at=TX,
    )
    result = built[1]
    assert not [f for f in result.findings if f.code.startswith("consolidate.")]
    return result


def of(result: Consolidation, predicate: str, subject: NodeRef | None = None) -> list[Claim]:
    found = [c for c in result.claims if c.predicate == predicate]
    found = [c for c in found if subject is None or c.subject == subject]
    return sorted(found, key=lambda c: (c.valid_from.ticks, c.id))


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def deltas(result: Consolidation, subject: NodeRef) -> list[tuple[int, int, Delta]]:
    out = []
    for claim in of(result, "drift", subject):
        assert isinstance(claim.object, TypedLiteral)
        assert isinstance(claim.object.value, Delta)
        end = claim.valid_to
        assert end is not OPEN
        out.append((claim.valid_from.ticks, end.ticks, claim.object.value))  # type: ignore[union-attr]
    return out


def test_three_recalibrations_are_one_ordered_history_on_the_wrist_camera() -> None:
    result = build(manipulator_cell())
    history = of(result, "calibrated_with", WRIST_CAMERA)
    revisions = [NodeRef(NodeType.CONFIGURATION, f"cfg:ARM-06-r{n}") for n in (1, 2, 3)]
    # Each from its stated valid_from until the next recalibration; the last is still current.
    assert [(c.object, c.valid_from, c.valid_to) for c in history] == [
        (revisions[0], at(200), at(400)),
        (revisions[1], at(400), at(600)),
        (revisions[2], at(600), OPEN),
    ]
    assert {c.assertion_kind for c in history} == {"observed"}
    assert not of(result, "calibration_candidate")
    # Every claim cites the calibration, the URDF's sensor and the chain that placed the URDF.
    first = history[0]
    assert URDF["id"] in first.provenance.records
    assert len(first.provenance.evidence) > 1


def test_drift_between_consecutive_recalibrations_is_exact_and_in_declared_units() -> None:
    result = build(manipulator_cell())
    found = deltas(result, WRIST_CAMERA)
    spans = sorted({(start, end) for start, end, _ in found})
    assert spans == [(200, 400), (400, 600)]
    by = {(start, d.quantity, d.representation): d for start, _, d in found}
    focal = by[(200, DeltaQuantity.PARAMETER, "values")]
    assert focal.name == "focal_length" and focal.values == (4.02 - 4.0,)
    translation = by[(400, DeltaQuantity.TRANSLATION, "translation")]
    assert translation.values == (0.052 - 0.051, 0.001 - 0.0, 0.099 - 0.1)
    assert translation.edge == (TOOL0, WRIST)
    rotation = by[(200, DeltaQuantity.ROTATION, "quaternion")]
    assert rotation.values == (0.0, 0.0, 0.01, 0.99995 - 1.0)
    # Units are the declared ones: mm for the focal length, m for the translation; none for a
    # quaternion. Nothing is converted to SI.
    units = {
        (c.object.value.representation, c.object.value.name): c.object.unit  # type: ignore[union-attr]
        for c in of(result, "drift", WRIST_CAMERA)
    }
    assert units[("values", "focal_length")] == unit_from_text("mm")
    assert units[("translation", None)] == unit_from_text("m")
    assert type(units[("quaternion", None)]).__name__ == "NotApplicable"
    # Both calibration records are evidence of every drift claim; no threshold is applied.
    for claim in of(result, "drift"):
        assert claim.assertion_kind == "observed"
        assert isinstance(claim.object, TypedLiteral)
        delta = claim.object.value
        assert isinstance(delta, Delta)
        assert {delta.earlier, delta.later} <= set(claim.provenance.records)


def test_recalibrations_are_calibrated_by_their_maintenance_and_requalification() -> None:
    records = manipulator_cell()
    result = build(records)
    produced = {(c.subject.node_id, c.object, c.valid_from) for c in of(result, "calibrated_by")}
    maintenance_id = next(r["id"] for r in records if r["kind"] == "maintenance_event")
    requalification_id = next(r["id"] for r in records if r["kind"] == "requalification_record")
    assert produced == {
        ("cfg:ARM-06-r2", LedgerRecordRef(maintenance_id), at(390)),  # type: ignore[arg-type]
        ("cfg:ARM-06-r3", LedgerRecordRef(requalification_id), at(590)),  # type: ignore[arg-type]
    }
    assert {c.assertion_kind for c in of(result, "calibrated_by")} == {"stated"}


# --- Warehouse AMR ------------------------------------------------------------------------------

AMR = LogicalId("fleet", "AMR-11")
FRONT_LIDAR = NodeRef(NodeType.SENSOR, "serial:LDR-0090")
MANIFEST = hardware("amr-11-manifest", machine=AMR)
BASE, LIDAR, MAST = (frame(n, "amr-urdf") for n in ("base_link", "front_lidar", "mast_link"))
LIDAR_EDGE = transform("amr-lidar", BASE, LIDAR, pose((0.3, 0.0, 0.2)))


def _lidar_calibration(
    name: str, start: int, xyz: tuple[float, float, float], subject: str = "front_lidar"
) -> list[Record]:
    own = transform(
        name, frame("base_link", f"{name}-file"), frame("front_lidar", f"{name}-file"), pose(xyz)
    )
    record = calibration(
        name,
        machine=AMR,
        subject=subject,
        valid_from=at(start),
        parameters={"range_offset": ((0.012,), "m")},
        extrinsics=(own["id"],),  # type: ignore[arg-type]
    )
    return [own, record, calibration_thread(name)]


def warehouse_amr() -> list[Record]:
    return [
        thread(AMR, "threads/amr"),
        MANIFEST,
        component(MANIFEST, "front_lidar", serial="LDR-0090", at=LIDAR),
        LIDAR_EDGE,
        sensor_thread("LDR-0090"),
    ]


def test_an_amr_lidar_found_through_its_manifest_drifts_by_its_stated_poses() -> None:
    first = _lidar_calibration("lidar-a", 1000, (0.3, 0.0, 0.2))
    second = _lidar_calibration("lidar-b", 5000, (0.302, -0.001, 0.2))
    bind_a = binding("lidar-a", BASE, LIDAR, first[0], first[1])
    bind_b = binding("lidar-b", BASE, LIDAR, second[0], second[1])
    result = build(warehouse_amr(), [*first, bind_a], [*second, bind_b])
    history = of(result, "calibrated_with", FRONT_LIDAR)
    assert [(c.object.node_id, c.valid_from, c.valid_to) for c in history] == [  # type: ignore[union-attr]
        ("cal:lidar-a", at(1000), at(5000)),
        ("cal:lidar-b", at(5000), OPEN),
    ]
    found = {d.quantity: d.values for _, _, d in deltas(result, FRONT_LIDAR)}
    assert found[DeltaQuantity.TRANSLATION] == (0.302 - 0.3, -0.001, 0.0)
    assert found[DeltaQuantity.ROTATION] == (0.0, 0.0, 0.0, 0.0)  # unchanged is a stated 0
    assert found[DeltaQuantity.PARAMETER] == (0.0,)


def test_a_calibration_for_a_sensor_not_in_the_configuration_is_a_finding_not_a_claim() -> None:
    rear = _lidar_calibration("rear", 1000, (-0.3, 0.0, 0.2), subject="rear_lidar")
    result = build(warehouse_amr(), rear)
    assert not result.claims
    (finding,) = [f for f in result.findings if f.code == "calibration.sensor_not_in_configuration"]
    assert finding.records == (rear[1]["id"],)
    assert finding.details["subject"] == ["rear_lidar"]


def test_a_binding_that_contradicts_the_description_is_a_candidate_citing_both() -> None:
    cal = _lidar_calibration("mast", 1000, (0.1, 0.0, 0.9))
    # The calibration says the lidar hangs off the mast; the description says off the base.
    wrong = binding("mast", MAST, LIDAR, cal[0], cal[1])
    result = build(warehouse_amr(), [*cal, wrong])
    assert not of(result, "calibrated_with")
    (candidate,) = of(result, "calibration_candidate", FRONT_LIDAR)
    assert candidate.object == NodeRef(NodeType.CONFIGURATION, "cal:mast")
    assert {wrong["id"], LIDAR_EDGE["id"]} <= set(candidate.provenance.records)
    assert "calibration.frame_disagreement" in codes(result)
    assert not of(result, "drift")


# --- Legged robot: a recalibration in another unit system ---------------------------------------

QUADRUPED = LogicalId("serial", "QX-0042")
IMU = NodeRef(NodeType.SENSOR, "serial:IMU-5521")
BODY = hardware("qx-0042-body", machine=QUADRUPED)


def _imu(name: str, start: int, gyro: tuple[float, str], accel: float) -> Record:
    return calibration(
        name,
        machine=QUADRUPED,
        subject="imu0",
        valid_from=at(start),
        parameters={
            "accelerometer_noise_density": ((accel,), "m/s^2"),
            "gyroscope_noise_density": ((gyro[0],), gyro[1]),
        },
    )


def test_a_recalibration_in_another_unit_gives_no_delta_and_is_never_converted() -> None:
    records = [
        thread(QUADRUPED, "threads/qx"),
        BODY,
        component(BODY, "imu0", serial="IMU-5521", at=frame("imu_link", "qx-urdf")),
        sensor_thread("IMU-5521"),
        _imu("imu-2025", 100, (0.00017, "rad/s"), 0.002),
        calibration_thread("imu-2025"),
        _imu("imu-2026", 900, (0.0097, "deg/s"), 0.0021),
        calibration_thread("imu-2026"),
    ]
    result = build(records)
    assert [c.valid_to for c in of(result, "calibrated_with", IMU)] == [at(900), OPEN]
    found = {d.name: d.values for _, _, d in deltas(result, IMU)}
    # The accelerometer is in m/s^2 both times: a delta. The gyroscope moved from rad/s to deg/s:
    # no delta, and the deg/s value is not turned into rad/s.
    assert found == {"accelerometer_noise_density": (0.0021 - 0.002,)}
    (mismatch,) = [f for f in result.findings if f.code == "calibration.unit_mismatch"]
    compared: Any = mismatch.details["compared"]
    assert [c["of"] for c in compared] == ["gyroscope_noise_density"]


def test_after_a_sensor_swap_the_chain_not_the_old_manifest_says_which_sensor() -> None:
    """Both manifests declare the AMR; the configuration chain says the retrofit was in force."""
    retrofit = hardware("amr-11-retrofit", machine=AMR)
    old, new = LogicalId("cfg", "AMR-11-r1"), LogicalId("cfg", "AMR-11-r2")
    records = [
        *warehouse_amr(),
        retrofit,
        component(retrofit, "front_lidar", serial="LDR-0200", at=LIDAR),
        sensor_thread("LDR-0200"),
        thread(old, "hw/amr-11-manifest", node_type=NodeType.CONFIGURATION),
        thread(new, "hw/amr-11-retrofit", node_type=NodeType.CONFIGURATION),
        commissioning("commissioning", [AMR], old, at(100)),
        maintenance("lidar swap", [AMR], new, at(500)),
    ]
    first = _lidar_calibration("before-swap", 200, (0.3, 0.0, 0.2))
    second = _lidar_calibration("after-swap", 800, (0.31, 0.0, 0.2))
    result = build(records, first, second)
    placed = [(c.subject.node_id, c.object.node_id) for c in of(result, "calibrated_with")]  # type: ignore[union-attr]
    assert placed == [("serial:LDR-0090", "cal:before-swap"), ("serial:LDR-0200", "cal:after-swap")]
    assert not of(result, "calibration_candidate")
    assert not of(result, "drift")  # two sensors: nothing is consecutive across a swap


def test_a_chain_on_another_clock_leaves_the_machine_declaring_configurations_as_readings() -> None:
    """The chain places the URDF (sensor LDR-0090) on the forms' clock; the calibration is on
    its tool's clock, so which configuration was in force then is not known: the retrofit that
    declares the AMR (sensor LDR-0200) is only one reading, never a definite placement."""
    urdf = hardware("amr-11-urdf")
    retrofit = hardware("amr-11-retrofit", machine=AMR)
    old, new = LogicalId("cfg", "AMR-11-u1"), LogicalId("cfg", "AMR-11-u2")
    records = [
        thread(AMR, "threads/amr"),
        urdf,
        component(urdf, "front_lidar", serial="LDR-0090", at=LIDAR),
        retrofit,
        component(retrofit, "front_lidar", serial="LDR-0200", at=LIDAR),
        sensor_thread("LDR-0090"),
        sensor_thread("LDR-0200"),
        thread(old, "hw/amr-11-urdf", node_type=NodeType.CONFIGURATION),
        thread(new, "hw/amr-11-retrofit", node_type=NodeType.CONFIGURATION),
        commissioning("commissioning", [AMR], old, at(100)),
        maintenance("swap", [AMR], new, at(500)),
    ]
    tool_clock = record_id("test.clock", {"name": "calibration tool clock"})
    same = calibration("same", machine=AMR, subject="front_lidar", valid_from=at(300))
    other = calibration(
        "other",
        machine=AMR,
        subject="front_lidar",
        valid_from=Timestamp(300, tool_clock),
    )
    on_chain = build(records, [same, calibration_thread("same")])
    assert [c.subject.node_id for c in of(on_chain, "calibrated_with")] == ["serial:LDR-0090"]
    off_chain = build(records, [other, calibration_thread("other")])
    assert not of(off_chain, "calibrated_with")
    assert [c.subject.node_id for c in of(off_chain, "calibration_candidate")] == [
        "serial:LDR-0200"
    ]
    assert "calibration.chain_undecided" in codes(off_chain)
