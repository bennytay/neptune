"""MVL-27 end to end: a robot's software tree through the real job and its sandbox (ADR 0040).

The tree is what a robot workspace holds: a ``.git`` directory (symbolic ``HEAD``, a loose ref,
``packed-refs``, an index, a loose object and a large pack), a ROS package, a Python project and
its lockfile, firmware images, policy checkpoints and an SBOM. Every identity-declaring file
becomes a ``SoftwareConfiguration`` exactly as the adapter alone makes it; git's object store
is left to the probe engine, read no further than its head; a second job writes the same package.
"""

import importlib.util
import struct
import sys
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.registry import AdapterRegistry
from neptune.identity import canonical_json
from neptune.model.finding import IngestFinding
from neptune.model.machine import SoftwareConfiguration
from neptune.model.provenance import EvidenceRef, TransformRecord
from neptune.model.source import LocalPath, SourceRevision
from neptune.runtime import IngestJob, JobOptions, JobOutcome, JobState
from neptune.store.package import read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

TESTS: Final = Path(__file__).parents[1]
MIB: Final = 1024 * 1024


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


ORACLE: Final = _load(TESTS / "fixtures" / "software" / "software_oracle.py")
GOLDEN: Final = _load(TESTS / "golden" / "software" / "make_software_golden.py")

# Where each fixture sits in the robot's tree.
TREE: Final = {
    ".git/HEAD": "git/HEAD",
    ".git/ORIG_HEAD": "git/ORIG_HEAD",
    ".git/refs/heads/main": "git/HEAD_detached",
    ".git/packed-refs": "git/packed-refs",
    "src/arm_controller/package.xml": "manifests/package.xml",
    "src/arm_controller/CMakeLists.txt": "manifests/CMakeLists.txt",
    "planner/pyproject.toml": "manifests/pyproject.toml",
    "planner/uv.lock": "lockfiles/uv.lock",
    "firmware/nav-node": "firmware/app.elf",
    "firmware/gripper.bin": "firmware/esp_app.bin",
    "firmware/fmu.px4": "firmware/firmware.px4",
    "models/policy.safetensors": "checkpoints/policy.safetensors",
    "models/policy.onnx": "checkpoints/policy.onnx",
    "models/cut.pt": "checkpoints/policy_truncated.pt",
    "sbom.cdx.json": "sbom/robot.cdx.json",
    "dist/release.sha256": "git/release.sha256",
}
# Read by another adapter (text), or by none.
NOT_SOFTWARE: Final = {".git/HEAD", "dist/release.sha256"}
PACK: Final = ".git/objects/pack/pack-0123456789abcdef0123456789abcdef01234567.pack"


def build_tree(root: Path) -> None:
    for location, fixture in TREE.items():
        path = root / location
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(ORACLE.fixture(fixture))
    git = root / ".git"
    (git / "index").write_bytes(b"DIRC" + struct.pack(">II", 2, 0) + b"\x00" * 20)
    loose = git / "objects" / "8f" / "3c2a1d9e7b6c5a4f3e2d1c0b9a8f7e6d5c4b3a"
    loose.parent.mkdir(parents=True)
    loose.write_bytes(zlib.compress(b"commit 25\x00tree 4b825dc642cb6eb9a060\n"))
    pack = root / PACK
    pack.parent.mkdir(parents=True)
    with pack.open("wb") as out:  # a large pack: its header, then 3 MiB of object data
        out.write(b"PACK" + struct.pack(">II", 2, 1_000_000))
        out.write(bytes(range(256)) * (3 * MIB // 256))


def run_job(root: Path, home: Path, destination: Path) -> tuple[JobOutcome, Any]:
    job = IngestJob(
        root,
        destination,
        Workspace(home),
        AdapterRegistry(builtin_adapters()),
        JobOptions(attempts=2),
    )
    outcome = job.run()
    assert outcome.state is JobState.COMMITTED
    return outcome, read_package(destination)


@pytest.fixture(scope="module")
def ingested(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, JobOutcome, Any]:
    base = tmp_path_factory.mktemp("software")
    root = base / "robot"
    build_tree(root)
    outcome, package = run_job(root, base / "home", base / "package")
    return root, outcome, package


def locations(package: Any) -> dict[str, str]:
    """Content id to location, for every source in the package."""
    found: dict[str, str] = {}
    for record in package.records:
        if isinstance(record, SourceRevision) and isinstance(record.location, LocalPath):
            found.setdefault(record.content_id, record.location.path)
    return found


def source_of(evidence: object) -> str:
    """The content id an adapter's citation or finding names."""
    assert isinstance(evidence, EvidenceRef) and isinstance(evidence.source, str)
    return evidence.source


def software(package: Any) -> dict[str, SoftwareConfiguration]:
    where = locations(package)
    return {
        where[source_of(record.provenance.evidence)]: record
        for record in package.records
        if isinstance(record, SoftwareConfiguration)
    }


def test_every_identity_file_becomes_a_software_configuration(ingested: Any) -> None:
    _, _, package = ingested
    found = software(package)
    # .git/refs/heads/main holds the same bytes as no other file, so it is read once.
    expected = {location for location in TREE if location not in NOT_SOFTWARE}
    assert set(found) == expected
    head = found[".git/refs/heads/main"]
    assert head.software[0].commit.value.sha == ORACLE.fixture("git/HEAD_detached").decode().strip()  # type: ignore[union-attr]


def test_the_job_records_what_the_adapter_alone_makes(ingested: Any) -> None:
    root, _, package = ingested
    by_location = software(package)
    for location, record in sorted(by_location.items()):
        alone = ORACLE.run((root / location).read_bytes())
        (expected,) = alone.records()
        assert canonical_json.dumps(record.to_json()) == canonical_json.dumps(expected.to_json())


def findings(package: Any) -> set[tuple[str, str]]:
    """Every finding in the package, as (code, location of its subject)."""
    where = locations(package)
    return {
        (record.code, where[source_of(record.subject)])
        for record in package.records
        if isinstance(record, IngestFinding)
    }


def test_findings_name_the_symbolic_head_and_the_cut_checkpoint(ingested: Any) -> None:
    _, _, package = ingested
    found = findings(package)
    assert ("software.git_symbolic_ref", ".git/HEAD") in found
    assert ("software.truncated", "models/cut.pt") in found
    assert ("software.software_identity_missing", "planner/uv.lock") in found
    assert not [code for code, _ in found if code.startswith("neptune.runtime.")]


def test_git_objects_are_left_to_the_probe_engine_and_read_no_further(ingested: Any) -> None:
    _, outcome, package = ingested
    where = locations(package)
    unread = {location for code, location in findings(package) if code.endswith(".unsupported")}
    assert {PACK, ".git/index"} <= unread
    pack_id = next(cid for cid, location in where.items() if location == PACK)
    transforms = {r.id: r for r in package.records if isinstance(r, TransformRecord)}
    readers = {
        transforms[r.provenance.transform].adapter_id
        for r in package.records
        if getattr(r, "provenance", None) is not None and r.provenance.evidence.source == pack_id
    }
    assert readers == set()  # no adapter read the pack: only its head was probed
    assert pack_id not in {source.source for source in outcome.cache.sources}  # nor planned it


def test_a_second_job_writes_the_same_package(ingested: Any, tmp_path: Path) -> None:
    root, outcome, _ = ingested
    again, _ = run_job(root, tmp_path / "home", tmp_path / "package")
    assert again.package == outcome.package


def test_the_golden_package_is_what_reading_the_fixture_gives() -> None:
    # On failure, run tests/golden/software/make_software_golden.py and explain the diff.
    built = GOLDEN.build()
    root = TESTS / "golden" / "software"
    committed = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }
    assert committed == built
