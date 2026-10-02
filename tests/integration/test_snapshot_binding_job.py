"""MVL-38 end to end: every run is bound to the snapshots evidence relates it to (ADR 0064).

A real job, with the real sandbox and grouping, reads four sites across embodiments:

- an AMR fleet: one session directory, two robots, each with its own recording, navigation
  parameters and git build revision, and a fleet configuration above both, which is neither's own;
- a manipulator cell whose recording's own MCAP metadata names its policy checkpoint by hash
  (stated), and its gripper firmware version and controller configuration by relative path
  (inferred: a version names a release, and a path assumes the robot's working directory);
- a quadruped trot with parameters and no software identity at all;
- an arm whose session holds two different ``params.yaml``, equally near its recording.

Stated bindings are canonical records; inferred ones are a derived table; every gap, conflict and
shared file is a finding naming the run. Nothing is mocked, and a second job writes the same
package.
"""

import hashlib
import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.registry import AdapterRegistry
from neptune.derived.bindings import (
    CONFLICTING_SNAPSHOTS,
    NO_SOFTWARE_IDENTITY,
    SHARED_SNAPSHOT,
    SNAPSHOT_UNRESOLVED,
    InferredSnapshotBinding,
)
from neptune.derived.sessions import read_derived
from neptune.model.alignment import SnapshotBinding, SnapshotKind
from neptune.model.configuration import ConfigurationSnapshot
from neptune.model.finding import FindingCategory, IngestFinding
from neptune.model.knowledge import AssertionKind
from neptune.model.machine import SoftwareConfiguration
from neptune.model.run import Run
from neptune.runtime import IngestJob, JobOptions, JobState
from neptune.store.package import read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

TESTS: Final = Path(__file__).parents[1]
SOFTWARE: Final = TESTS / "fixtures" / "software"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FORMATS: Final = _load("binding_formats", TESTS / "fixtures" / "model" / "formats.py")
POLICY: Final = (SOFTWARE / "checkpoints" / "policy.safetensors").read_bytes()
FIRMWARE: Final = SOFTWARE / "firmware" / "esp_app.bin"  # ESP-IDF image, version v2.3.1-robot


def recording(
    topic: str, metadata: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
) -> bytes:
    schema = FORMATS.McapSchema(1, "std_msgs/msg/String", "ros2msg", b"string data")
    channel = FORMATS.McapChannel(1, 1, topic, "cdr")
    payload = FORMATS.Cdr().string(topic).bytes()
    message = FORMATS.McapMessage(1, 0, 1_000_000_000, 1_000_000_000, payload)
    data, _ = FORMATS.mcap("ros2", (schema,), (channel,), (message,), metadata=metadata)
    return bytes(data)


def build(root: Path) -> None:
    files: dict[str, bytes] = {
        # AMR fleet: per-robot parameters and builds in one session directory.
        "fleet/run_001/amr_01/drive.mcap": recording("/amr_01/odom"),
        "fleet/run_001/amr_01/nav_params.yaml": b"amr_01:\n  max_speed: 1.2\n",
        "fleet/run_001/amr_01/REVISION": b"8f3c2a1d9e7b6c5a4f3e2d1c0b9a8f7e6d5c4b3a\n",
        "fleet/run_001/amr_02/drive.mcap": recording("/amr_02/odom"),
        "fleet/run_001/amr_02/nav_params.yaml": b"amr_02:\n  max_speed: 0.9\n",
        "fleet/run_001/amr_02/REVISION": b"0123456789abcdef0123456789abcdef01234567\n",
        "fleet/run_001/fleet.yaml": b"fleet:\n  robots: 2\n",
        # Manipulator cell: the recording names its firmware, checkpoint and configuration.
        "cell/run_007/arm.mcap": recording(
            "/arm/joint_states",
            (
                (
                    "robot_info",
                    (
                        ("gripper_firmware", "v2.3.1-robot"),
                        ("policy_sha256", hashlib.sha256(POLICY).hexdigest()),
                        ("controller_config", "config/controller.yaml"),
                    ),
                ),
            ),
        ),
        "cell/run_007/firmware/gripper.bin": FIRMWARE.read_bytes(),
        "cell/run_007/models/policy.safetensors": POLICY,
        "cell/run_007/config/controller.yaml": b"controller:\n  rate_hz: 500\n",
        # Quadruped: parameters, no software identity.
        "quad/run_003/trot.mcap": recording("/quad/gait"),
        "quad/run_003/gait.yaml": b"gait:\n  stride_m: 0.4\n",
        # Conflict: two different params.yaml, equally near the recording.
        "conflict/run_009/arm.mcap": recording("/conflict/joint_states"),
        "conflict/run_009/left/params.yaml": b"arm:\n  payload_kg: 2.0\n",
        "conflict/run_009/right/params.yaml": b"arm:\n  payload_kg: 5.0\n",
    }
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def ingest(root: Path, tmp_path: Path, name: str) -> Any:
    registry = AdapterRegistry(builtin_adapters())
    job = IngestJob(root, tmp_path / name, Workspace(tmp_path / "home"), registry, JobOptions())
    result = job.run()
    assert result.state is JobState.COMMITTED, result
    return read_package(tmp_path / name)


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Any:
    tmp_path = tmp_path_factory.mktemp("binding")
    build(tmp_path / "root")
    return ingest(tmp_path / "root", tmp_path, "package")


def _runs(package: Any) -> dict[str, Run]:
    """Each run by its recording's path."""
    paths = {r.content_id: r.location.path for r in package.records if r.kind == "source_revision"}
    return {paths[r.provenance.evidence.source]: r for r in package.records if isinstance(r, Run)}


def _snapshot_paths(package: Any) -> dict[str, str]:
    """Each snapshot record's id to its file's path."""
    paths = {r.content_id: r.location.path for r in package.records if r.kind == "source_revision"}
    kinds = (ConfigurationSnapshot, SoftwareConfiguration)
    return {
        r.id: paths[r.provenance.evidence.source] for r in package.records if isinstance(r, kinds)
    }


def _bound(package: Any, run: Run) -> dict[str, tuple[str, SnapshotKind]]:
    """What the run is bound to: the snapshot's path to (assertion, kind)."""
    where = _snapshot_paths(package)
    out: dict[str, tuple[str, SnapshotKind]] = {}
    for record in package.records:
        if isinstance(record, SnapshotBinding) and record.run == run.id:
            assert record.provenance.assertion_kind is AssertionKind.STATED
            out[where[record.snapshot]] = ("stated", record.snapshot_kind)
    for record in read_derived(package.derived):
        if isinstance(record, InferredSnapshotBinding) and record.run == run.id:
            out[where[record.snapshot]] = ("inferred", record.snapshot_kind)
    return out


def _codes(package: Any, run: Run) -> list[tuple[str, str]]:
    return sorted(
        (f.code, str(f.details.get("snapshot_kind", "")))
        for f in package.records
        if isinstance(f, IngestFinding)
        and f.code.startswith("neptune.bindings.")
        and run.id in f.records
    )


CONFIG: Final = SnapshotKind.CONFIGURATION_SNAPSHOT
SOFTWARE_KIND: Final = SnapshotKind.SOFTWARE_CONFIGURATION
UNRESOLVED_REST: Final = [
    (SNAPSHOT_UNRESOLVED, "calibration"),
    (SNAPSHOT_UNRESOLVED, "hardware_configuration"),
]


def test_each_fleet_robot_gets_its_own_parameters_and_build(package: Any) -> None:
    runs = _runs(package)
    for robot in ("amr_01", "amr_02"):
        run = runs[f"fleet/run_001/{robot}/drive.mcap"]
        assert _bound(package, run) == {
            f"fleet/run_001/{robot}/nav_params.yaml": ("inferred", CONFIG),
            f"fleet/run_001/{robot}/REVISION": ("inferred", SOFTWARE_KIND),
        }
        # fleet.yaml is as near to both robots: bound to neither, and said so once.
        assert _codes(package, run) == sorted([(SHARED_SNAPSHOT, ""), *UNRESOLVED_REST])
    (shared,) = [
        f for f in package.records if isinstance(f, IngestFinding) and f.code == SHARED_SNAPSHOT
    ]
    assert shared.details["directory"] == "fleet/run_001"
    assert shared.details["recordings"] == 2


def test_the_cell_recording_names_its_firmware_checkpoint_and_configuration(
    package: Any,
) -> None:
    run = _runs(package)["cell/run_007/arm.mcap"]
    assert _bound(package, run) == {
        "cell/run_007/firmware/gripper.bin": ("inferred", SOFTWARE_KIND),
        "cell/run_007/models/policy.safetensors": ("stated", SOFTWARE_KIND),
        "cell/run_007/config/controller.yaml": ("inferred", CONFIG),
    }
    assert _codes(package, run) == UNRESOLVED_REST
    stated = [r for r in package.records if isinstance(r, SnapshotBinding) and r.run == run.id]
    for binding in stated:  # each cites the metadata row that names the snapshot
        assert binding.provenance.evidence.source == run.provenance.evidence.source
    assert package.manifest.version == 3  # a package holding alignment records


def test_a_quadruped_run_with_no_software_identity_says_so(package: Any) -> None:
    run = _runs(package)["quad/run_003/trot.mcap"]
    assert _bound(package, run) == {"quad/run_003/gait.yaml": ("inferred", CONFIG)}
    assert _codes(package, run) == sorted(
        [(NO_SOFTWARE_IDENTITY, "software_configuration"), *UNRESOLVED_REST]
    )


def test_two_equally_near_parameter_files_are_a_conflict_never_a_choice(package: Any) -> None:
    run = _runs(package)["conflict/run_009/arm.mcap"]
    assert _bound(package, run) == {}
    assert _codes(package, run) == sorted(
        [
            (CONFLICTING_SNAPSHOTS, "configuration_snapshot"),
            (SNAPSHOT_UNRESOLVED, "configuration_snapshot"),
            (NO_SOFTWARE_IDENTITY, "software_configuration"),
            *UNRESOLVED_REST,
        ]
    )
    (conflict,) = [
        f
        for f in package.records
        if isinstance(f, IngestFinding) and f.code == CONFLICTING_SNAPSHOTS
    ]
    assert conflict.category is FindingCategory.AMBIGUOUS
    candidates: Any = conflict.details["candidates"]
    paths = sorted(p for c in candidates for p in c["paths"])
    assert paths == ["conflict/run_009/left/params.yaml", "conflict/run_009/right/params.yaml"]
    assert len(conflict.records) == 3  # the run and both candidates
    assert len(conflict.related) == 2  # both candidates' declarations


def test_every_run_is_bound_or_explicitly_unresolved_for_every_kind(package: Any) -> None:
    for run in _runs(package).values():
        kinds = {kind for _, kind in _bound(package, run).values()}
        missing = {
            str(f.details["snapshot_kind"])
            for f in package.records
            if isinstance(f, IngestFinding)
            and f.code in (SNAPSHOT_UNRESOLVED, NO_SOFTWARE_IDENTITY)
            and run.id in f.records
        }
        assert {str(k) for k in kinds} | missing == {str(k) for k in SnapshotKind}
        assert not {str(k) for k in kinds} & missing


def test_a_second_job_writes_the_same_package(package: Any, tmp_path: Path) -> None:
    build(tmp_path / "root")
    again = ingest(tmp_path / "root", tmp_path, "again")
    assert again.id == package.id


def test_identical_parameters_at_two_paths_bind_one_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "root"
    build(root)
    shutil.copy(
        root / "conflict/run_009/left/params.yaml", root / "conflict/run_009/right/params.yaml"
    )
    package = ingest(root, tmp_path, "same")
    run = _runs(package)["conflict/run_009/arm.mcap"]
    bound = [r for r in read_derived(package.derived) if isinstance(r, InferredSnapshotBinding)]
    mine = [b for b in bound if b.run == run.id]
    assert len(mine) == 1 and mine[0].snapshot_kind is CONFIG
    assert CONFLICTING_SNAPSHOTS not in [c for c, _ in _codes(package, run)]
