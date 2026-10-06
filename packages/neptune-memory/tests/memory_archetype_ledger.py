"""The archetype Ledger: one multi-embodiment, multi-transaction Ledger over which all eight
registered deterministic consolidators emit claims (Memory ADR 0003 §4, ADR 0007 §5).

``ARCHETYPE`` maps a Ledger transaction (``tx_seq``) to the packages registered at it; a package id
appears once in the whole Ledger. ``packages_at(snapshot)`` is the cumulative Ledger a build at
that snapshot sees. Every record is built with the compiler's own types (or the Ledger stand-ins
of the ``memory_*_records`` helpers) and holds in canonical JSON, so the Ledger can be exported to
a file and read back with identical consolidation output. Scenarios are adapted from the
consolidator suites; nothing here is random or reads the clock.

Embodiments: aerial (PX4 drone), legged (quadruped), manipulator (arm cells), mobile (warehouse
AMRs, a rover), humanoid (an operator-confirmed identity), marine (a survey boat).

Scenarios by registration transaction:

tx 1
- identity: a drone's asset tag co-declared with its PX4 ``sys_uuid`` (aerial); an arm cell's
  configuration lineage v1-v2-v3; an AMR bag and register row (candidates only); a humanoid
  fleet-register id confirmed ``same_identity`` with a vendor id by an operator assertion
- time: the PX4 flight's boot and GPS clocks with a stated sync (aerial); the quadruped's first
  boot-to-dock clock sync ``sync-v1``
- runs: a warehouse of six AMRs on two sites, boot clocks, a manifest per day; a rosbag2 recording
  whose first part ``bag_0`` is uploaded (``bag-upload-1``)
- configuration: the warehouse AMR chain r3-r4-r5 and its authorisation envelope
- calibration: an arm cell's wrist camera recalibrated twice (``wrist-1``, ``wrist-2``)
- episodes: AMR-07's job missions with manifest tasks (no tickets yet) and a manipulator pick cycle
- coverage: an AMR bag with a lidar at half its declared rate and a late odometry stream

tx 2
- identity: an operator joins a quadruped's bag namespace to its serial
- time: ``sync-v2``, the quadruped's re-sync from boot tick 1000; two warehouse sites' NTP clocks
  related through GPS time
- runs: the rosbag2 recording's second part ``bag_1`` (the run ``continues`` the first part)
- configuration: the arm-cell chain (repair, tool change, three recalibrations) and three shifts of
  AMR-07 bound to configurations
- episodes: the tickets (a mid-mission intervention, a bumper stop, an untimed intervention)
- events: a CMMS incident for AMR-07 and an arm cell's incident report with its timeline
- coverage: a truncated AGV bag; a quadruped's streams on two clocks

tx 3
- identity: the operator RETRACTS the humanoid confirmation
- calibration: ``wrist-3``, the third recalibration, closes ``wrist-2``'s interval; with its
  requalification
- events: an operator intervention and a controller fault log on two clocks related by a mapping
- coverage: a manipulator cell's wrist-camera video; a survey boat's sonar (marine)

A later transaction changes what an earlier build emitted in four places, so a claim built at
tx 1 is absent from the build at tx 3: ``sync-v2`` replaces ``sync-v1``'s mapping (time);
``bag_1`` widens the bag's span (runs); the tickets cut AMR-07's missions short (episodes); the
retraction withdraws the humanoid ``same_as`` (identity); and ``wrist-3`` closes ``wrist-2``'s
validity (calibration).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

import memory_calibration_records as cal
import memory_configuration_records as conf
import memory_coverage_records as cov
import memory_episode_records as epi
import memory_event_records as evt
import memory_run_records as runs
import memory_time_records as tim
from memory_configuration_records import worked_example
from memory_identity_records import (
    assertion,
    at,
    civil_domain,
    lineage,
    link,
    thread,
)
from neptune.model.alignment import MemberRole
from neptune.model.assertion import AssertionType
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import Known
from neptune.model.time import Timestamp
from neptune_memory.schema.nodes import NodeType

Record = dict[str, object]
REC, DESC = MemberRole.RECORDING, MemberRole.DESCRIPTION
SECOND: Final = 10**9
T0: Final = 1_790_000_000 * SECOND  # an instant in 2026, POSIX nanoseconds


def _pkgs(prefix: str, packages: dict[str, list[Record]]) -> dict[str, list[Record]]:
    """``packages`` with every package id prefixed, so ids stay unique across scenarios."""
    return {f"{prefix}/{name}": records for name, records in packages.items()}


# --- identity -------------------------------------------------------------------------------------

DRONE_TAG = LogicalId("asset-tag", "UAV-0042")
DRONE_LOG = tim.DRONE  # the PX4 ``sys_uuid`` the flight's run is recorded by
SPOT_BAG = LogicalId("ros2.namespace", "/spot1")
SPOT_SERIAL = LogicalId("serial", "SPOT-1234")
CELL_CONFIGS = [LogicalId("cell.config", f"left-arm/v{n}") for n in (1, 2, 3)]
AMR_BAG = LogicalId("ros1.hostname", "amr-12")
AMR_ROW = LogicalId("site.register_row", "W3/AMR-12")
HUMANOID_A = LogicalId("fleet-register", "apollo-03")
HUMANOID_B = LogicalId("vendor-log", "unit-7f2c")
CONFIRMATION = LogicalId("ops-console", "ASR-2026-0301")


def _identity_1() -> dict[str, list[Record]]:
    config = NodeType.CONFIGURATION
    return {
        "drone": [thread(DRONE_LOG, "flight.ulg")],
        "fleet": [thread(DRONE_TAG, "fleet.csv"), link("fleet.csv row 1", DRONE_TAG, DRONE_LOG)],
        "cell": [
            *(
                thread(c, "session.mcap", f"hand_eye {c.value}", node_type=config)
                for c in CELL_CONFIGS
            ),
            lineage("recommissioned v2", CELL_CONFIGS[0], CELL_CONFIGS[1]),
            lineage("recommissioned v3", CELL_CONFIGS[1], CELL_CONFIGS[2]),
        ],
        "amr": [thread(AMR_BAG, "drive.bag", "sites.csv"), thread(AMR_ROW, "sites.csv")],
        "humanoid-register": [thread(HUMANOID_A, "register.csv")],
        "humanoid-vendor": [thread(HUMANOID_B, "vendor.log")],
        "humanoid-ops": [
            assertion(
                "apollo-03 is unit 7f2c",
                AssertionType.SAME_IDENTITY,
                (HUMANOID_A, HUMANOID_B),
                identifier=CONFIRMATION,
                authored_at=at(8),
            )
        ],
    }


def _identity_2() -> dict[str, list[Record]]:
    return {
        "quadruped": [thread(SPOT_BAG, "walk_0.mcap"), thread(SPOT_SERIAL, "asset register")],
        "ops": [
            assertion(
                "ASR-1", AssertionType.SAME_IDENTITY, (SPOT_BAG, SPOT_SERIAL), authored_at=at(7)
            )
        ],
    }


def _identity_3() -> dict[str, list[Record]]:
    return {
        "humanoid-ops-retraction": [
            assertion("withdrawn", AssertionType.RETRACT, (), retracts=CONFIRMATION)
        ]
    }


# --- time -----------------------------------------------------------------------------------------


def _quadruped_sync(version: int) -> list[Record]:
    """The quadruped's clocks and first sync (1), or its re-sync from boot tick 1000 (2)."""
    if version == 2:
        return [tim.mapping("sync-v2", "spot boot", "dock", anchor=(1_000, 50_030), start=1_000)]
    return [
        tim.domain("spot boot", tim.MICRO),
        tim.domain("dock", tim.MICRO),
        tim.domain("site gps", tim.MICRO),
        tim.mapping("sync-v1", "spot boot", "dock", anchor=(0, 50_000), start=0, end=tim.OPEN_SIDE),
        tim.mapping("dock-gps", "dock", "site gps", anchor=(0, 7), start=0, end=tim.OPEN_SIDE),
    ]


def _time_1() -> dict[str, list[Record]]:
    return {"px4-flight-17": tim.drone_flight(), "quadruped-sync-1": _quadruped_sync(1)}


def _time_2() -> dict[str, list[Record]]:
    return {
        "quadruped-sync-2": _quadruped_sync(2),
        **_pkgs("sites", tim.two_sites()),
    }


# --- runs -----------------------------------------------------------------------------------------

NORTH, SOUTH = LogicalId("site", "WH-NORTH"), LogicalId("site", "WH-SOUTH")
EPOCH_NS: Final = 1_790_000_000 * 10**9


def _amr(k: int) -> LogicalId:
    return LogicalId("asset-tag", f"AMR-0{k}")


def _warehouse_runs() -> dict[str, list[Record]]:
    """Six AMRs at two sites, each log on its boot clock; AMR-01's is mapped to civil time."""
    civil_record, civil_id = runs.domain("site ntp", civil=True)
    packages: dict[str, list[Record]] = {
        "registers": [
            runs.site("site register north", NORTH),
            runs.site("site register south", SOUTH),
        ],
        "ntp": [civil_record],
    }
    boot_1: RecordId | None = None
    for k in range(1, 7):
        day = "day-1-north" if k <= 3 else "day-1-south"
        boot_record, boot = runs.domain(f"amr-0{k} boot", civil=False)
        log = f"amr-0{k}/log.mcap"
        record, run_id = runs.run(
            log, first=at(1_000, boot), last=at(60_000, boot), machine=_amr(k)
        )
        held, _ = runs.assembly(log, run_id, [(log, REC)], rule="recording")
        packages.setdefault(day, []).extend([boot_record, runs.revision(log)[0], record, held])
        packages.setdefault(f"{day}-manifest", []).append(
            runs.declaration(
                f"amr-0{k}",
                runs.by_record(run_id),
                site=NORTH if k <= 3 else SOUTH,
                task=LogicalId("task", "pick-wave-1"),
            )
        )
        if k == 1:
            boot_1 = boot
    assert boot_1 is not None
    packages["day-1-north"].append(
        runs.mapping("amr-01 sync", boot_1, civil_id, anchor=(0, EPOCH_NS))
    )
    return packages


def _bag_upload(part: str) -> list[Record]:
    """One upload of a rosbag2 recording: its metadata and part ``bag_0`` or ``bag_1``."""
    clock, clock_id = runs.domain(f"{part} log_time", civil=True)
    meta, meta_id = runs.run("amr-03/bag/metadata.yaml")
    index = int(part[-1])
    record, _ = runs.run(
        f"amr-03/bag/{part}.mcap",
        first=at(EPOCH_NS + 100 * index, clock_id),
        last=at(EPOCH_NS + 100 * index + 99, clock_id),
        machine=_amr(3),
    )
    held, _ = runs.assembly(
        "amr-03/bag/metadata.yaml#relative_file_paths",
        meta_id,
        [("amr-03/bag/metadata.yaml", DESC), (f"amr-03/bag/{part}.mcap", REC)],
    )
    files = [
        runs.revision("amr-03/bag/metadata.yaml")[0],
        runs.revision(f"amr-03/bag/{part}.mcap")[0],
    ]
    return [clock, meta, record, held, *files]


# --- configuration --------------------------------------------------------------------------------

SITE_CLOCK, SITE_CLOCK_ID = civil_domain("site forms")
AMR07 = LogicalId("fleet", "AMR-07")
S007 = LogicalId("siteops.site", "S-007")
R3, R4, R5 = (LogicalId("siteops.configuration", f"CFG-AMR07-r{n}") for n in (3, 4, 5))
ARM = LogicalId("robot.serial", "20415")
CELL = LogicalId("plant.cell", "CELL-3")
CELL_CFG_IDS = ("CELL3-CFG-A", "CELL3-CFG-A.1", "CELL3-CFG-B", "CELL3-CFG-B.1", "CELL3-CFG-B.2")


def _posix(text: str) -> int:
    """A stated instant with its declared UTC offset; never the host's time zone."""
    instant = datetime.fromisoformat(text)
    if instant.tzinfo is None:
        raise ValueError(f"a stated instant needs its offset: {text!r}")
    return int(instant.timestamp())


def _stated(text: str) -> Timestamp:
    """An instant as a site form states it, on the forms' own (civil) clock."""
    return Timestamp(_posix(text), SITE_CLOCK_ID)


def _amr_chain() -> list[Record]:
    """The warehouse worked example, its threads, and a firmware change with requalification."""
    return [
        *worked_example("warehouse_amr"),
        *conf.threads(NodeType.MACHINE, AMR07),
        *conf.threads(NodeType.SITE, S007),
        *(
            conf.configuration_thread(c, f"threads/{c.value}", f"{c.value}.yaml")
            for c in (R3, R4, R5)
        ),
        SITE_CLOCK,
        conf.change(
            "CHG-0040 firmware V01.04.00", [AMR07], R5, _stated("2026-10-01T06:00:00+10:00")
        ),
        conf.requalification("RQ-0040", [AMR07], R5, _stated("2026-10-01T15:00:00+10:00")),
    ]


def _amr_shifts() -> list[Record]:
    """Three shifts of AMR-07: before the incident change (r3), after it (r4), and one unbound."""
    controller, controller_id = civil_domain("AMR-07 controller clock")

    def shift(name: str, first: str, last: str) -> Record:
        return conf.run(
            f"{name}.bag",
            LogicalId("fleet.run", name),
            Timestamp(_posix(first), controller_id),
            Timestamp(_posix(last), controller_id),
            AMR07,
        )

    def bind(name: str, shift_: Record, revision: str) -> Record:
        snapshot = conf.hardware(f"CFG-AMR07-{revision}.yaml", AMR07)
        return conf.binding(name, shift_, snapshot, start="open", end="open", clock=controller_id)

    before = shift("AMR-07/2026-09-23", "2026-09-23T10:00:00+10:00", "2026-09-23T10:59:59+10:00")
    after = shift("AMR-07/2026-09-28", "2026-09-28T10:00:00+10:00", "2026-09-28T10:59:59+10:00")
    unbound = shift("AMR-07/2026-09-29", "2026-09-29T10:00:00+10:00", "2026-09-29T10:59:59+10:00")
    return [
        controller,
        *(
            conf.run_thread(LogicalId("fleet.run", r), f"threads/{r}")
            for r in ("AMR-07/2026-09-23", "AMR-07/2026-09-28", "AMR-07/2026-09-29")
        ),
        before,
        after,
        unbound,
        conf.hardware("CFG-AMR07-r3.yaml", AMR07),
        conf.hardware("CFG-AMR07-r4.yaml", AMR07),
        bind("09-23", before, "r3"),
        bind("09-28", after, "r4"),
    ]


def _cell_chain() -> list[Record]:
    """The manipulator-cell worked example, a gripper change and three recalibrations."""
    config = {
        name: LogicalId("plant.configuration", name) for name in (*CELL_CFG_IDS, "CELL3-CFG-B.3")
    }
    return [
        *worked_example("manipulator_cell"),
        *conf.threads(NodeType.MACHINE, ARM),
        *conf.threads(NodeType.SITE, CELL),
        *(conf.configuration_thread(c) for c in config.values()),
        SITE_CLOCK,
        conf.change(
            "CHG-T1 gripper 2F-140 to EPick",
            [ARM],
            config["CELL3-CFG-B"],
            _stated("2026-10-02T08:00:00+10:00"),
        ),
        conf.maintenance(
            "WO-R1 TCP recalibration",
            [ARM],
            config["CELL3-CFG-B.1"],
            _stated("2026-10-03T08:00:00+10:00"),
        ),
        conf.maintenance(
            "WO-R2 TCP recalibration",
            [ARM],
            config["CELL3-CFG-B.2"],
            _stated("2026-10-04T08:00:00+10:00"),
        ),
        conf.maintenance(
            "WO-R3 TCP recalibration",
            [ARM],
            config["CELL3-CFG-B.3"],
            _stated("2026-10-05T08:00:00+10:00"),
        ),
    ]


# --- calibration ----------------------------------------------------------------------------------

WRIST_ARM = LogicalId("asset-tag", "ARM-06")
TOOL0, WRIST = cal.frame("tool0", "urdf"), cal.frame("wrist_camera", "urdf")
URDF = cal.hardware("ur5e-cell")  # a URDF: it names no machine; the chain places it
CAL_REVISIONS = {n: LogicalId("cfg", f"ARM-06-r{n}") for n in (1, 2, 3)}
HAND_EYE = {
    1: ((0.05, 0.0, 0.1), (0.0, 0.0, 0.0, 1.0), 4.0),
    2: ((0.051, 0.0, 0.1), (0.0, 0.0, 0.01, 0.99995), 4.02),
    3: ((0.052, 0.001, 0.099), (0.0, 0.0, 0.02, 0.9998), 4.01),
}


def _recalibration(n: int, start: int) -> list[Record]:
    """Recalibration ``n`` of the wrist camera, its configuration revision and its thread."""
    xyz, quaternion, focal = HAND_EYE[n]
    own = cal.transform(
        f"hand-eye-{n}",
        cal.frame("tool0", f"hand-eye-file-{n}"),
        cal.frame("wrist_camera", f"hand-eye-file-{n}"),
        cal.pose(xyz, quaternion),
    )
    record = cal.calibration(
        f"wrist-{n}",
        machine=WRIST_ARM,
        subject="wrist_camera",
        valid_from=at(start),
        parameters={"distortion_model": "radtan", "focal_length": ((focal,), "mm")},
        extrinsics=(own["id"],),  # type: ignore[arg-type]
    )
    return [
        own,
        record,
        cal.binding(f"hand-eye-{n}", TOOL0, WRIST, own, record),
        thread(
            CAL_REVISIONS[n], "hw/ur5e-cell", f"cal/wrist-{n}", node_type=NodeType.CONFIGURATION
        ),
    ]


def _calibration_1() -> list[Record]:
    """The cell, its commissioning and a camera remount, and the first two recalibrations."""
    description_edge = cal.transform("urdf-wrist", TOOL0, WRIST, cal.pose((0.05, 0.0, 0.1)))
    return [
        thread(WRIST_ARM, "threads/arm"),
        URDF,
        cal.component(URDF, "wrist_camera", serial="CAM-7731", at=WRIST),
        description_edge,
        cal.sensor_thread("CAM-7731"),
        conf.commissioning("commissioning", [WRIST_ARM], CAL_REVISIONS[1], at(100)),
        conf.maintenance("camera remount", [WRIST_ARM], CAL_REVISIONS[2], at(390)),
        *_recalibration(1, 200),
        *_recalibration(2, 400),
    ]


def _calibration_3() -> list[Record]:
    """The requalification and the third recalibration, which closes the second's interval."""
    return [
        conf.requalification("requalification", [WRIST_ARM], CAL_REVISIONS[3], at(590)),
        *_recalibration(3, 600),
    ]


# --- episodes -------------------------------------------------------------------------------------

AMR_A, AMR_B = LogicalId("asset-tag", "AMR-07"), LogicalId("asset-tag", "AMR-08")
TOTE, PALLET = LogicalId("task", "tote-transport"), LogicalId("task", "empty-pallet-return")
UR10: Final = LogicalId("asset-tag", "UR10-CELL3")
BIN_PICK: Final = LogicalId("task", "bin-pick-7")
CYCLE: Final = LogicalId("cell-3.cycle", "0412")


def _job(value: str) -> LogicalId:
    return LogicalId("wms.job", value)


def _missions() -> tuple[dict[str, list[Record]], dict[str, list[Record]]]:
    """AMR-07's job missions with their manifest, and the tickets that cut some short."""
    boot_record, boot = runs.domain("amr-07 boot", civil=False)
    boot8_record, boot8 = runs.domain("amr-08 boot", civil=False)
    missions: list[Record] = [boot_record, boot8_record]
    manifest: list[Record] = []
    for value, first, last, task in (
        ("J-1042", 1_000, 9_000, TOTE),
        ("J-1043", 10_000, 19_000, PALLET),
        ("J-1044", 20_000, 29_000, TOTE),
        ("J-1045", 30_000, 39_000, None),  # the mission log names no task for it
    ):
        record, _ = runs.run(
            f"missions/{value}.mcap",
            first=at(first, boot),
            last=at(last, boot),
            machine=AMR_A,
            logical_id=_job(value),
        )
        missions.append(record)
        if task is not None:
            manifest.append(runs.declaration(value, _job(value), task=task))
    other, _ = runs.run(
        "missions/J-2001.mcap",
        first=at(1_000, boot8),
        last=at(9_000, boot8),
        machine=AMR_B,
        logical_id=_job("J-2001"),
    )
    missions.append(other)
    manifest.append(runs.declaration("J-2001", _job("J-2001"), task=TOTE))
    assist, _ = epi.intervention(
        "INT-1187",
        machines=[AMR_A],
        start=at(3_000, boot),
        end=at(3_500, boot),
        outcome="mission completed",
    )
    bumper, _ = epi.incident(
        "INC-0007", machines=[AMR_A], occurred=at(14_000, boot), description="bumper stop"
    )
    named, _ = epi.intervention("INT-1190", related=[_job("J-1044")])
    return (
        {"missions": missions, "manifest": manifest},
        {"tickets": [assist, bumper, named]},
    )


def _pick_cycle() -> list[Record]:
    """A pick cycle on the arm's boot clock, mapped to the console's civil clock by a stated PTP
    mapping, and a human intervention mid-task stated on the console clock."""
    boot_record, boot = runs.domain("cell-3 arm boot", civil=False)
    console_record, console = runs.domain("cell console", civil=True)
    cycle, _ = runs.run(
        "cell-3/cycle-0412.bag",
        first=at(5 * SECOND, boot),
        last=at(65 * SECOND, boot),
        machine=UR10,
        logical_id=CYCLE,
    )
    stop, _ = epi.intervention(
        "OP-77",
        machines=[UR10],
        start=Timestamp(EPOCH_NS + 25 * SECOND, console),
        end=Timestamp(EPOCH_NS + 31 * SECOND, console),
    )
    return [
        boot_record,
        console_record,
        cycle,
        runs.mapping("cell-3 ptp", boot, console, anchor=(0, EPOCH_NS), bound=2 * SECOND),
        runs.declaration("cycle 0412", CYCLE, task=BIN_PICK),
        stop,
    ]


# --- events ---------------------------------------------------------------------------------------

AMR07_CMMS = LogicalId("cmms.asset", "AMR-07")
WH_NORTH, AISLE_14 = LogicalId("site", "WH-NORTH"), LogicalId("zone", "WH-NORTH/AISLE-14")
H0: Final = 52_358 * SECOND  # 14:32:38 on the HMI's time-of-day clock
ARM3A: Final = LogicalId("cmms.asset", "ARM-3A")


def _cmms_incident() -> list[Record]:
    clock, cmms = runs.domain("cmms export", civil=True)
    report, _ = evt.incident(
        "cmms INC-0007",
        occurred=Timestamp(T0 + 2 * SECOND, cmms),
        severity="S2",
        description="AMR-07 struck a pallet in aisle 14; e-stop pressed by the picker",
        machines=[AMR07_CMMS],
        site=WH_NORTH,
        zone=AISLE_14,
        identifier=LogicalId("cmms.incident", "INC-0007"),
    )
    return [clock, report]


def _arm_cell_report() -> list[Record]:
    """The arm cell's incident report (INC-C3-0011), its timeline on the HMI clock."""
    hmi_clock, hmi = runs.domain("cell3 hmi", civil=False)
    report, _ = evt.incident(
        "INC-C3-0011.pdf",
        occurred=Timestamp(H0, hmi),
        severity="S2",
        description="Part dropped onto the floor guard after a protective stop; E-stop at OP-2",
        machines=[ARM3A],
        site=LogicalId("site", "PLANT-2"),
        zone=LogicalId("zone", "PLANT-2/CELL-3"),
        timeline=[
            (Timestamp(H0, hmi), "Protective stop: joint 5 torque over its limit"),
            (Timestamp(H0 + 3 * SECOND, hmi), "Operator presses the E-stop at OP-2"),
        ],
    )
    return [hmi_clock, report]


def _two_clock_events() -> list[Record]:
    """An intervention on a robot's boot clock and a fault log on a controller's, related by a
    stated mapping."""
    a_clock, a = runs.domain("robot boot", civil=False)
    b_clock, b = runs.domain("controller", civil=False)
    assist, _ = evt.intervention(
        "formant intervention 41",
        start=Timestamp(10 * SECOND, a),
        end=Timestamp(70 * SECOND, a),
        mode="remote assist",
        reason="robot stuck at a door",
        machines=[LogicalId("formant.device", "dev-9")],
    )
    report, _ = evt.incident("controller fault log", occurred=Timestamp(6 * SECOND, b))
    return [
        a_clock,
        b_clock,
        assist,
        report,
        runs.mapping("sync", a, b, anchor=(10 * SECOND, 5 * SECOND), bound=0),
    ]


# --- coverage -------------------------------------------------------------------------------------

LIDAR, CAMERA = LogicalId("serial", "VLP-16-0042"), LogicalId("serial", "CAM-7731")
WRIST_CAM, FORCE = LogicalId("serial", "WRIST-CAM-1"), LogicalId("serial", "FT-300-9")


def _amr_bag() -> list[Record]:
    """A lidar declared at 100 Hz that the series holds at 50 Hz, an odometry stream that starts a
    second late, and a camera configured with no stream naming it."""
    clock_record, clock = runs.domain("amr-07 bag boot", civil=False)
    run_record, run_id = runs.run(
        "amr-07.bag",
        first=at(T0, clock),
        last=at(T0 + 10 * SECOND, clock),
        machine=LogicalId("asset-tag", "AMR-07"),
    )
    scan, scan_id = cov.stream(
        "/scan",
        run_id,
        (clock,),
        count=1001,
        first=at(T0, clock),
        last=at(T0 + 10 * SECOND, clock),
        recording="amr-07.bag",
    )
    odom, odom_id = cov.stream(
        "/odom",
        run_id,
        (clock,),
        count=501,
        first=at(T0, clock),
        last=at(T0 + 5 * SECOND, clock),
        recording="amr-07.bag",
    )
    config, config_id = cov.configuration("amr-07.urdf", LogicalId("asset-tag", "AMR-07"))
    lidar, _ = cov.component("amr-07.urdf", config_id, "front_lidar", LIDAR)
    camera, _ = cov.component("amr-07.urdf", config_id, "nav_camera", CAMERA)
    return [
        clock_record,
        run_record,
        scan,
        odom,
        cov.series(scan_id, clock, T0, T0 + 10 * SECOND, 501),
        cov.series(odom_id, clock, T0 + SECOND, T0 + 5 * SECOND, 401),
        config,
        lidar,
        camera,
        cov.binding("amr-07 launch", run_id, config_id),
    ]


def _truncated_agv() -> list[Record]:
    """A truncated bag: the series stops before the extent its index declares."""
    clock_record, clock = runs.domain("agv boot", civil=False)
    run_record, run_id = runs.run("agv.bag", first=at(T0, clock), last=at(T0 + 10 * SECOND, clock))
    imu, imu_id = cov.stream(
        "/imu",
        run_id,
        (clock,),
        count=1001,
        first=at(T0, clock),
        last=at(T0 + 10 * SECOND, clock),
        recording="agv.bag",
    )
    return [
        clock_record,
        run_record,
        imu,
        cov.finding("rosbag1.truncated", "agv.bag"),
        cov.finding(
            "rosbag1.chunk_truncated", "agv.bag", severity=Severity.WARNING, records=(imu_id,)
        ),
        cov.finding(
            "rosbag1.topic_mismatch",
            "other.bag",
            category=FindingCategory.INCONSISTENT,
            severity=Severity.INFO,
        ),
        cov.series(imu_id, clock, T0, T0 + 6 * SECOND, 601),
    ]


def _quadruped_streams() -> list[Record]:
    """A quadruped's streams on two clocks, one of them with untimed samples."""
    log_record, log_time = runs.domain("quadruped log_time", civil=False)
    stamp_record, header = runs.domain("quadruped header.stamp", civil=False)
    run_record, run_id = runs.run(
        "spot-12.mcap",
        first=at(T0, log_time),
        last=at(T0 + 2 * SECOND, log_time),
        machine=LogicalId("asset-tag", "Q-12"),
    )
    joints, joints_id = cov.stream(
        "/joint_states",
        run_id,
        (log_time, header),
        count=201,
        first=at(T0, log_time),
        last=at(T0 + 2 * SECOND, log_time),
        recording="spot-12.mcap",
    )
    return [
        log_record,
        stamp_record,
        run_record,
        joints,
        cov.series(joints_id, log_time, T0 + cov.HZ_100, T0 + 2 * SECOND, 200),
        # Header stamps: ten samples carry none, so the header clock's gaps are not decided.
        cov.series(joints_id, header, 5_000, 5_000 + 189 * cov.HZ_100, 190, rows_unknown=10),
    ]


def _cell_video() -> list[Record]:
    """A manipulator cell recorded as a wrist-camera video: the camera recorded, the force-torque
    sensor configured beside it known absent."""
    clock_record, clock = runs.domain("cell civil", civil=True)
    run_record, run_id = runs.run(
        "cell-run.yaml",
        first=at(T0, clock),
        last=at(T0 + 30 * SECOND, clock),
        machine=LogicalId("asset-tag", "ARM-06"),
    )
    files = runs.assembly("cell-run.yaml", run_id, [("wrist.mp4", REC), ("cell-run.yaml", DESC)])[0]
    clip, _ = cov.video("wrist.mp4", clock, Known(WRIST_CAM))
    config, config_id = cov.configuration("arm-06.urdf", LogicalId("asset-tag", "ARM-06"))
    wrist, _ = cov.component("arm-06.urdf", config_id, "wrist_camera", WRIST_CAM)
    force, _ = cov.component("arm-06.urdf", config_id, "wrist_ft", FORCE)
    return [
        clock_record,
        run_record,
        files,
        runs.revision("wrist.mp4")[0],
        runs.revision("cell-run.yaml")[0],
        clip,
        config,
        wrist,
        force,
        cov.binding("cell launch", run_id, config_id),
    ]


def _survey_boat() -> list[Record]:
    """A survey boat's sonar stream, its run declaring its own id (marine)."""
    clock_record, clock = runs.domain("boat", civil=False)
    run_record, run_id = runs.run(
        "survey-3.mcap",
        first=at(0, clock),
        last=at(SECOND, clock),
        logical_id=LogicalId("manifest", "survey-3"),
    )
    sonar, sonar_id = cov.stream("/sonar", run_id, (clock,), recording="survey-3.mcap")
    return [clock_record, run_record, sonar, cov.series(sonar_id, clock, 0, SECOND, 11)]


# --- the Ledger -----------------------------------------------------------------------------------


def _tx_1() -> dict[str, list[Record]]:
    missions, _ = _missions()
    return {
        **_pkgs("identity", _identity_1()),
        **_pkgs("time", _time_1()),
        **_pkgs("runs", _warehouse_runs()),
        "runs/bag-upload-1": _bag_upload("bag_0"),
        "configuration/amr-07-chain": _amr_chain(),
        "calibration/wrist-cell-1": _calibration_1(),
        **_pkgs("episodes", missions),
        "episodes/pick-cycle": _pick_cycle(),
        "coverage/amr-bag": _amr_bag(),
    }


def _tx_2() -> dict[str, list[Record]]:
    _, tickets = _missions()
    return {
        **_pkgs("identity", _identity_2()),
        **_pkgs("time", _time_2()),
        "runs/bag-upload-2": _bag_upload("bag_1"),
        "configuration/cell-3-chain": _cell_chain(),
        "configuration/amr-07-shifts": _amr_shifts(),
        **_pkgs("episodes", tickets),
        "events/cmms-incident": _cmms_incident(),
        "events/arm-cell-report": _arm_cell_report(),
        "coverage/truncated-agv": _truncated_agv(),
        "coverage/quadruped": _quadruped_streams(),
    }


def _tx_3() -> dict[str, list[Record]]:
    return {
        **_pkgs("identity", _identity_3()),
        "calibration/wrist-cell-3": _calibration_3(),
        "events/two-clocks": _two_clock_events(),
        "coverage/cell-video": _cell_video(),
        "coverage/survey-boat": _survey_boat(),
    }


ARCHETYPE: Final[dict[int, dict[str, list[Record]]]] = {1: _tx_1(), 2: _tx_2(), 3: _tx_3()}


def packages_at(snapshot: int) -> dict[str, list[Record]]:
    """Every package registered at or before ``snapshot`` (cumulative)."""
    return {
        pid: records
        for tx, packages in sorted(ARCHETYPE.items())
        if tx <= snapshot
        for pid, records in packages.items()
    }
