"""MVL-205 end to end: a manifest's declarations as stated records, and what it cannot apply.

A real job (default sandbox, grouping, binding) reads one hostile folder: a legged robot's two
patrols and an arm's recording, under a manifest that declares them and also gets things wrong.
Each wrong thing is a finding of the manifest transform citing the manifest, and the rest still
applies: the package commits, every recording names its declared machine through a stated
``run_declaration``, and the pin that does resolve is a stated binding. What the manifest reader
refuses whole (ADR 0047 §1 to §2) is a configuration error before any work. A second job writes the
same package.
"""

import hashlib
import importlib.util
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.manifest.records import (
    MACHINE_CONTRADICTS_RUN,
    PIN_NOT_A_SNAPSHOT,
    PIN_UNRESOLVED,
    RUN_DECLARED_TWICE,
    RUN_UNRECORDED,
)
from neptune.model.alignment import SnapshotBinding
from neptune.model.finding import IngestFinding
from neptune.model.knowledge import AssertionKind, Known
from neptune.model.run import Run, RunDeclaration
from neptune.sdk import ConfigurationError, Neptune, read_package

pytestmark = pytest.mark.integration

TESTS: Final = Path(__file__).parents[1]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FORMATS: Final = _load("declarations_job_formats", TESTS / "fixtures" / "model" / "formats.py")
GAIT: Final = b"gait:\n  stride_m: 0.4\n  height_m: 0.3\n"


def recording(topic: str) -> bytes:
    schema = FORMATS.McapSchema(1, "std_msgs/msg/String", "ros2msg", b"string data")
    channel = FORMATS.McapChannel(1, 1, topic, "cdr")
    payload = FORMATS.Cdr().string(topic).bytes()
    message = FORMATS.McapMessage(1, 0, 3_000_000_000, 3_000_000_000, payload)
    data, _ = FORMATS.mcap("ros2", (schema,), (channel,), (message,))
    return bytes(data)


MANIFEST: Final = b"""\
neptune: 1
machines:
  - {id: LEG-01, embodiment: legged}
  - {id: ARM-2, embodiment: manipulator}
sites:
  - {id: PLANT-2}
runs:
  - name: patrol-0912
    paths: [legged/patrol_0912.mcap]
    machine: LEG-01
    site: PLANT-2
    snapshots:
      - {path: legged/config/gait.yaml}
      - {path: legged/config/missing.yaml}
      - {path: legged/config}
      - {path: legged/config/outside.yaml}
      - {content: "sha256:%s"}
      - {path: legged/notes.csv}
  - {name: patrol-0914, paths: [legged/patrol_0914.mcap], machine: LEG-01, site: PLANT-2}
  - {name: patrol-0914-again, paths: [legged], machine: LEG-01}
  - {name: arm-pick, paths: [arm], machine: ARM-2, site: PLANT-2}
  - {name: notes-only, paths: [legged/notes.csv], machine: LEG-01}
""" % (hashlib.sha256(b"never written").hexdigest().encode())


def build(root: Path, outside: Path) -> Path:
    files = {
        "neptune.yaml": MANIFEST,
        "legged/patrol_0912.mcap": recording("/leg01/gait"),
        "legged/patrol_0914.mcap": recording("/leg01/odom"),
        "legged/config/gait.yaml": GAIT,
        "legged/notes.csv": b"time,note\n1,ok\n",
        "arm/pick.mcap": recording("/arm/joint_states"),
    }
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    outside.write_bytes(b"secret: 1\n")
    (root / "legged" / "config" / "outside.yaml").symlink_to(outside)
    return root


@pytest.fixture(scope="module")
def ingested(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Any, Any]:
    tmp = tmp_path_factory.mktemp("declarations")
    root = build(tmp / "root", tmp / "outside.yaml")
    first = Neptune(tmp / "ws1").ingest(root, tmp / "first")
    second = Neptune(tmp / "ws2").ingest(root, tmp / "second")
    assert first.committed and second.committed
    return root, first, second


@pytest.fixture(scope="module")
def package(ingested: tuple[Path, Any, Any]) -> Any:
    return read_package(ingested[1].destination)


def _paths(package: Any) -> dict[Any, str]:
    return {r.content_id: r.location.path for r in package.records if r.kind == "source_revision"}


def _findings(package: Any, code: str) -> list[IngestFinding]:
    return [r for r in package.records if isinstance(r, IngestFinding) and r.code == code]


def test_the_same_folder_gives_the_same_package(ingested: tuple[Path, Any, Any]) -> None:
    _, first, second = ingested
    assert first.package == second.package
    assert (first.destination / "manifest.json").read_bytes() == (
        second.destination / "manifest.json"
    ).read_bytes()


def test_every_recording_names_its_declared_machine_through_a_stated_record(
    package: Any,
) -> None:
    paths = _paths(package)
    machines: dict[str, set[str]] = {}
    for record in package.records:
        if isinstance(record, RunDeclaration):
            assert record.provenance.assertion_kind is AssertionKind.STATED
            assert paths[record.provenance.evidence.source] == "neptune.yaml"
            run = next(r for r in package.records if isinstance(r, Run) and r.id == record.run)
            assert isinstance(record.machine, Known)
            where = paths[run.provenance.evidence.source]
            machines.setdefault(where, set()).add(record.machine.value.value)
    assert machines == {
        "legged/patrol_0912.mcap": {"LEG-01"},
        "legged/patrol_0914.mcap": {"LEG-01"},
        "arm/pick.mcap": {"ARM-2"},
    }


def test_each_mistake_is_a_finding_and_the_rest_applies(package: Any) -> None:
    by_code = Counter(
        r.code
        for r in package.records
        if isinstance(r, IngestFinding) and r.code.startswith("neptune.manifest.")
    )
    assert by_code == {
        PIN_UNRESOLVED: 4,  # missing, a directory, a symlink out of the root, unknown content
        PIN_NOT_A_SNAPSHOT: 1,  # the notes table holds no configuration
        RUN_DECLARED_TWICE: 2,  # patrol-0912 and patrol-0914 are both in "legged"
        RUN_UNRECORDED: 1,  # notes-only holds no recording
    }
    unresolved = {
        f.details["manifest_pointer"]: f.message for f in _findings(package, PIN_UNRESOLVED)
    }
    assert "a symlink" in unresolved["/runs/0/snapshots/3"]
    assert "a directory" in unresolved["/runs/0/snapshots/2"]
    paths = _paths(package)
    (binding,) = [
        b
        for b in package.records
        if isinstance(b, SnapshotBinding) and paths[b.provenance.evidence.source] == "neptune.yaml"
    ]
    assert binding.provenance.assertion_kind is AssertionKind.STATED
    assert (
        MACHINE_CONTRADICTS_RUN not in by_code
    )  # recordings that state no machine contradict none


def test_a_pin_through_a_symlink_reads_nothing_outside_the_root(package: Any) -> None:
    outside = "sha256:" + hashlib.sha256(b"secret: 1\n").hexdigest()
    assert outside not in _paths(package)
    assert "legged/config/outside.yaml" not in _paths(package).values()


@pytest.mark.parametrize(
    ("edit", "says"),
    [
        (b"machine: ARM-2, site", b"machine: ARM-9, site"),
        (b"name: patrol-0914-again", b"name: patrol-0914"),
        (b"{path: legged/config/missing.yaml}", b"{path: ../outside.yaml}"),
    ],
)
def test_what_the_reader_refuses_is_refused_before_any_work(
    tmp_path: Path, edit: bytes, says: bytes
) -> None:
    root = build(tmp_path / "root", tmp_path / "outside.yaml")
    manifest = root / "neptune.yaml"
    text = manifest.read_bytes()
    assert edit in text
    manifest.write_bytes(text.replace(edit, says, 1))
    with pytest.raises(ConfigurationError):
        Neptune(tmp_path / "ws").ingest(root, tmp_path / "out")
    assert not (tmp_path / "out").exists()
