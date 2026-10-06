"""MVL-24 end to end: robot descriptions through the real job, the real sandbox and a package.

A folder of URDF and Xacro descriptions, corrupt and hostile files beside them, and one
description copied byte for byte to a second robot's folder. The job commits; every description
lands as configuration, components, frames and transforms; every bad file is a finding about
itself; nothing merges the two robots; and a second job in a fresh workspace writes the same
package.
"""

import shutil
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.builtin import default_registry
from neptune.model.knowledge import Known, NotCovered
from neptune.model.machine import (
    DescriptionExpansion,
    HardwareComponent,
    HardwareConfiguration,
    Machine,
)
from neptune.model.reference import FrameTransform
from neptune.model.source import LocalPath, SourceRevision
from neptune.runtime import IngestJob, JobOptions, JobState
from neptune.store.package import IngestPackage, read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "urdf"
FILES: Final = (
    "robots/arm6.urdf",
    "robots/diff_drive.urdf",
    "robots/quadrotor.urdf",
    "renamed/robot_description",
    "xacro/quadrotor.urdf.xacro",
    "xacro/diff_drive.urdf.xacro",
    "corrupt/truncated.urdf",
    "corrupt/bad_values.urdf",
    "corrupt/latin1.urdf",
    "hostile/billion_laughs.urdf",
    "hostile/external_entity.urdf",
    "hostile/deep_nesting.urdf",
    "hostile/macro_bomb.urdf.xacro",
    "hostile/recursive_macro.urdf.xacro",
    "hostile/expression_attacks.urdf.xacro",
)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "descriptions"
    for relative in FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / relative, target)
    second = root / "fleet" / "arm_b"
    second.mkdir(parents=True)
    shutil.copy(FIXTURES / "robots" / "arm6.urdf", second / "arm6.urdf")
    return root


def ingest(root: Path, tmp_path: Path, name: str) -> IngestPackage:
    job = IngestJob(
        root,
        tmp_path / name,
        Workspace(tmp_path / f"{name}-home"),
        default_registry(),
        JobOptions(),
    )
    outcome = job.run()
    assert outcome.state is JobState.COMMITTED
    return read_package(tmp_path / name)


def of(package: IngestPackage, kind: type) -> list[Any]:
    return [record for record in package.records if isinstance(record, kind)]


def test_descriptions_land_through_the_sandbox_and_bad_files_are_findings(
    corpus: Path, tmp_path: Path
) -> None:
    package = ingest(corpus, tmp_path, "package")
    names = sorted(
        c.name.value for c in of(package, HardwareConfiguration) if isinstance(c.name, Known)
    )
    assert names == [
        "arm6",
        "attacks",
        "bad_values",
        "diffbot",
        "diffbot",
        "quadrotor",
        "quadrotor",
    ]
    assert len(of(package, DescriptionExpansion)) == 3  # the two Xacro robots and the attacks
    codes = sorted({finding.code for finding in package.receipt.findings})
    assert "urdf.doctype_refused" in codes and "urdf.limit_exceeded" in codes
    assert "urdf.xml_malformed" in codes and "urdf.encoding_unsupported" in codes
    assert "urdf.xacro_not_covered" in codes and "urdf.xacro_include_not_followed" in codes
    assert not [code for code in codes if code.startswith("neptune.runtime")]
    readers = {(t.adapter_id, t.adapter_version) for t in package.receipt.transforms}
    assert ("urdf", "0.1.0") in readers
    transforms = of(package, FrameTransform)
    wheels = [t for t in transforms if t.child.frame_id == "left_wheel_link"]
    assert len(wheels) == 2  # the URDF's and the Xacro's, in two frame graphs
    assert wheels[0].parent.frame_graph_id != wheels[1].parent.frame_graph_id


def test_two_robots_sharing_a_description_are_never_merged(corpus: Path, tmp_path: Path) -> None:
    package = ingest(corpus, tmp_path, "package")
    revisions = [
        r
        for r in of(package, SourceRevision)
        if isinstance(r.location, LocalPath) and r.location.parts[-1] == "arm6.urdf"
    ]
    assert len(revisions) == 2 and revisions[0].content_id == revisions[1].content_id
    arms = [c for c in of(package, HardwareConfiguration) if c.name == Known("arm6")]
    assert len(arms) == 1  # one artifact, read once; it names a model, never a machine
    assert isinstance(arms[0].machine, NotCovered)
    assert not of(package, Machine)
    joints = [c for c in of(package, HardwareComponent) if c.configuration == arms[0].id]
    assert len(joints) == 28


def test_a_second_job_in_a_fresh_workspace_writes_the_same_package(
    corpus: Path, tmp_path: Path
) -> None:
    first, second = ingest(corpus, tmp_path, "first"), ingest(corpus, tmp_path, "second")
    assert first.id == second.id
    assert first.files() == second.files()
