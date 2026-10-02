"""MVL-23 acceptance, end to end: two robot runs are bound to the exact configuration snapshots
they used and compared field by field later, from the package alone.

A real ingest job, with the real parser sandbox (ADR 0030), reads a field folder: two runs' ROS 2
parameters and the tool configuration they shared, a vehicle's PX4 parameters, a renamed backup
of run A's parameters, notes, and broken, truncated and hostile files. Nothing is mocked.
"""

import shutil
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.registry import AdapterRegistry
from neptune.model.configuration import (
    ChangeKind,
    ConfigFormat,
    ConfigurationSnapshot,
    ConfigurationValue,
    compare_configurations,
)
from neptune.model.finding import IngestFinding
from neptune.model.knowledge import Known
from neptune.model.provenance import EvidenceRef
from neptune.model.source import LocalPath
from neptune.runtime import IngestJob, JobOptions, JobState
from neptune.store.package import read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
LAYOUT: Final = {
    "run_a/params.yaml": "config/run_a_params.yaml",
    "run_a/tool.toml": "config/gripper_tool.toml",
    "run_b/params.yaml": "config/run_b_params.yaml",
    "run_b/tool.toml": "config/gripper_tool.toml",  # the same tool: the same bytes
    "vehicle/px4_params.json": "config/px4_params.json",
    "vehicle/params_backup": "config/robot_config",  # run A's values, BOM and CR LF, no extension
    "notes.txt": "text/notes.txt",
    "broken.toml": "config/corrupted_tool.toml",
    "truncated.json": "config/truncated_px4.json",
    "bomb.yaml": "config/billion_laughs.yaml",
}


@pytest.fixture
def field(tmp_path: Path) -> Path:
    root = tmp_path / "field"
    for target, source in LAYOUT.items():
        (root / target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / source, root / target)
    return root


def ingest(root: Path, tmp_path: Path, name: str, home: str = "home") -> tuple[Any, Any]:
    registry = AdapterRegistry(builtin_adapters())
    job = IngestJob(root, tmp_path / name, Workspace(tmp_path / home), registry, JobOptions())
    outcome = job.run()
    assert outcome.state is JobState.COMMITTED
    return outcome, read_package(tmp_path / name)


class Bound:
    """What the package binds each location to: its bytes, the adapter that read them, and the
    snapshot and values those bytes declare."""

    def __init__(self, package: Any) -> None:
        self.package = package
        transforms = {t.id: t.adapter_id for t in package.receipt.transforms}
        self.sources = {
            source.location.path: (source.content_id, [transforms[t] for t in source.read_by])
            for source in package.receipt.sources
            if isinstance(source.location, LocalPath)
        }
        self.snapshots = [r for r in package.records if isinstance(r, ConfigurationSnapshot)]
        self.values = [r for r in package.records if isinstance(r, ConfigurationValue)]

    def snapshot(self, location: str) -> ConfigurationSnapshot:
        content, _ = self.sources[location]
        (found,) = [s for s in self.snapshots if s.provenance.evidence.source == content]
        return found

    def values_of(self, location: str) -> list[ConfigurationValue]:
        snapshot = self.snapshot(location)
        return [v for v in self.values if v.snapshot == snapshot.id]


def test_two_runs_bind_to_exact_snapshots_and_compare_field_by_field(
    field: Path, tmp_path: Path
) -> None:
    _, package = ingest(field, tmp_path, "package")
    bound = Bound(package)

    # Each source was read by the adapter its bytes call for, never by its name.
    assert {path: readers for path, (_, readers) in bound.sources.items()} == {
        "bomb.yaml": ["config"],
        "broken.toml": ["text", "neptune.probe"],  # not valid TOML: read as text, and said so
        "notes.txt": ["text"],
        "run_a/params.yaml": ["config"],
        "run_a/tool.toml": ["config"],
        "run_b/params.yaml": ["config"],
        "run_b/tool.toml": ["config"],
        "truncated.json": ["config"],
        "vehicle/params_backup": ["config"],
        "vehicle/px4_params.json": ["config"],
    }
    assert package.manifest.version == 2  # it holds configuration records (ADR 0037)

    # Exact snapshots: a run binds to its file's bytes and the snapshot they declare.
    run_a, run_b = bound.snapshot("run_a/params.yaml"), bound.snapshot("run_b/params.yaml")
    assert run_a.id != run_b.id and run_a.digest != run_b.digest
    assert bound.snapshot("run_a/tool.toml") == bound.snapshot("run_b/tool.toml")  # one artifact
    backup = bound.snapshot("vehicle/params_backup")
    assert backup.id != run_a.id and backup.digest == run_a.digest  # other bytes, same values
    assert (backup.byte_order_mark, run_a.byte_order_mark) == (True, False)
    assert bound.snapshot("vehicle/px4_params.json").format is ConfigFormat.JSON
    for snapshot in (run_a, run_b, backup):
        assert snapshot.values == len([v for v in bound.values if v.snapshot == snapshot.id]) == 15

    # Field by field, from the package alone.
    planner = ("controller_server", "ros__parameters", "FollowPath")
    changes = compare_configurations(
        bound.values_of("run_a/params.yaml"), bound.values_of("run_b/params.yaml")
    )
    assert [(c.path, c.change) for c in changes] == [
        ((*planner, "critics", 1), ChangeKind.CHANGED),
        ((*planner, "critics", 2), ChangeKind.CHANGED),
        ((*planner, "debug_trajectory_details"), ChangeKind.REMOVED),
        ((*planner, "max_vel_x"), ChangeKind.CHANGED),
        ((*planner, "sim_time"), ChangeKind.CHANGED),
        ((*planner, "xy_goal_tolerance"), ChangeKind.ADDED),
    ]
    by_id = {v.id: v for v in bound.values}
    (speed,) = (c for c in changes if c.path[-1] == "max_vel_x")
    assert [by_id[i].text for i in (*speed.left, *speed.right)] == [Known("0.26"), Known("0.31")]
    assert (
        compare_configurations(
            bound.values_of("run_a/params.yaml"), bound.values_of("vehicle/params_backup")
        )
        == ()
    )

    # Broken and hostile input cost findings about their own sources, nothing else.
    findings = [r for r in package.records if isinstance(r, IngestFinding)]
    codes = sorted(f.code for f in findings if f.code.startswith("config."))
    assert codes == ["config.syntax_error"]
    (syntax,) = (f for f in findings if f.code == "config.syntax_error")
    assert isinstance(syntax.subject, EvidenceRef)
    assert syntax.subject.source == bound.sources["truncated.json"][0]
    (mismatch,) = (f for f in findings if f.code == "neptune.probe.name_mismatch")
    assert isinstance(mismatch.subject, EvidenceRef)
    assert mismatch.subject.source == bound.sources["broken.toml"][0]
    assert len(bound.values_of("bomb.yaml")) == 91


def test_a_second_job_writes_the_same_package_and_reuses_every_chunk(
    field: Path, tmp_path: Path
) -> None:
    first, _ = ingest(field, tmp_path, "first")
    again, _ = ingest(field, tmp_path, "again")
    fresh, _ = ingest(field, tmp_path, "fresh", home="other")
    assert first.package == again.package == fresh.package
    assert again.cache.calls.ingest == 0 and again.cache.calls.plan == 0
