"""The two deployment archetypes, through the compiler's packages and the Deploy mapper (ADR 0004).

``fixtures/archetypes/make_archetypes.py`` writes the sources (an AMR fleet and a manipulator cell),
runs ``neptune ingest`` over them as a subprocess into the committed base packages, and keeps the
mapper's output as golden files. These tests never run ingestion (a member may not, root
``test_merge_freshness``): they check the generator and the sources, that each base package is the
ingest of exactly those sources, that the mapper over a base package gives the golden lifecycle
package, that the corrupt bag and the stale config are findings and not failures, and that every
lifecycle value is ``stated`` and cites the export, form or span it came from.
"""

import importlib.util
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from xml.etree import ElementTree  # our own generated fixtures, not hostile input

import pytest

from neptune.identity.hashing import content_id
from neptune.model.ids import ContentId
from neptune.model.knowledge import AssertionKind, Known
from neptune.model.lifecycle import LIFECYCLE_KINDS
from neptune.model.provenance import EvidenceRef, Provenance
from neptune.store.package import IngestPackage, read_package
from neptune_deploy.lifecycle import MAPPER_ID

ARCHETYPES: Final = Path(__file__).parent / "fixtures" / "archetypes"
MAX_BYTES: Final = 512 * 1024


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_archetypes", ARCHETYPES / "make_archetypes.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


A: Final = _generator()
BUILT: Final = A.build()  # the generator's output, once; one test builds it a second time


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


# --- The generator and the sources (fast) ---------------------------------------------------------


def test_the_committed_sources_are_what_the_generator_writes() -> None:
    # On failure, run make_archetypes.py and explain the diff in the PR.
    assert _tree(A.SOURCES) == BUILT


def test_generation_is_deterministic() -> None:
    assert A.build() == BUILT


def test_every_committed_fixture_file_is_under_512_kib() -> None:
    sizes = {path: len(data) for path, data in _tree(ARCHETYPES).items()}
    assert max(sizes.values()) < MAX_BYTES, max(sizes.items(), key=lambda item: item[1])


def _names(deployment: str) -> set[str]:
    return {path.removeprefix(f"{deployment}/") for path in BUILT if path.startswith(deployment)}


def test_the_fleet_has_what_the_issue_lists() -> None:
    names = _names(A.FLEET)
    configs = {n for n in names if n.startswith("config/")}
    assert {n.split("/")[1] for n in configs} == {f"AMR-{n:02d}" for n in range(5, 11)}
    assert {n.split("/")[1] for n in names if n.startswith("runs/")} == {"S-007", "S-012"}
    assert len([n for n in names if n.endswith(".mcap") and "runs/" in n]) == 7  # six runs, one bag
    assert {n for n in names if n.endswith(".urdf")} == {"urdf/tug_200.urdf", "urdf/lift_150.urdf"}
    assert {n for n in names if n.endswith(".geojson")} == {
        "maps/S-007_zones.geojson",
        "maps/S-012_zones.geojson",
    }
    assert {n for n in names if n.endswith(".pdf")} == {
        "incidents/INC-0007.pdf",
        "incidents/INC-0013.pdf",
    }
    for needed in (
        "authorisation/zone_register.csv",
        "cmms/work_orders.csv",
        "changes/servicenow_changes.csv",
        "requalification/requalification_tests.csv",
    ):
        assert needed in names


def test_the_cell_has_what_the_issue_lists() -> None:
    names = _names(A.CELL)
    assert len([n for n in names if n.startswith("calibration/")]) == 4  # commissioning + 3
    assert any(n.startswith("bags/") and n.endswith("metadata.yaml") for n in names)
    pdfs = {n for n in names if n.endswith(".pdf")}
    assert {p.split("/")[-1].split("_")[0] for p in pdfs} == {"risk", "commissioning", "sop"}
    for needed in (
        "urdf/arm6.urdf",
        "cmms/work_orders.csv",
        "changes/servicenow_changes.csv",
        "requalification/requalification_tests.csv",
        "tickets/near_miss_export.json",
    ):
        assert needed in names


def _joints(path: str) -> dict[str, str]:
    """A built URDF's joints and their types."""
    root = ElementTree.fromstring(BUILT[path])
    return {joint.attrib["name"]: joint.attrib["type"] for joint in root.iter("joint")}


def test_the_embodiments_are_ground_vehicles_and_an_arm() -> None:
    """Every robot here is a wheeled AMR or a six-axis arm (root AGENTS.md: every robot)."""
    tug = _joints(f"{A.FLEET}/urdf/tug_200.urdf")
    lift = _joints(f"{A.FLEET}/urdf/lift_150.urdf")
    arm = _joints(f"{A.CELL}/urdf/arm6.urdf")
    wheels = sorted(joint for joint, kind in tug.items() if kind == "continuous")
    assert wheels == ["left_wheel_joint", "right_wheel_joint"]
    assert {joint for joint, kind in lift.items() if kind == "prismatic"} == {"fork_lift_joint"}
    assert list(arm.values()).count("revolute") == 6
    for joints in (tug, lift, arm):
        assert not any(word in name for name in joints for word in ("rotor", "prop", "motor"))
    configured = {path.split("/")[2] for path in BUILT if path.startswith(f"{A.FLEET}/config/")}
    assert configured == {f"AMR-{n:02d}" for n in range(5, 11)}


# --- The committed base packages and the mapper over them -----------------------------------------


@pytest.fixture(scope="module")
def packages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path]]:
    work = tmp_path_factory.mktemp("archetypes")
    out = {}
    for name in A.PIPELINES:
        A.lifecycle(A.PACKAGES / name, A.PIPELINES[name], work / name)
        out[name] = (A.PACKAGES / name, work / name)
    return out


def _of(package: IngestPackage, kind: str) -> list[Any]:
    return [r for r in package.records if r.kind == kind]


def _paths(package: IngestPackage) -> dict[Any, str]:
    return {r.content_id: r.location.path for r in _of(package, "source_revision")}


def _sources(value: Any) -> set[Any]:
    """Every source id a record's provenance and findings cite, found in its JSON."""
    found: set[Any] = set()
    stack = [value.to_json()]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if isinstance(item.get("source"), str):
                found.add(ContentId(item["source"]))
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _codes(package: IngestPackage) -> Counter[str]:
    return Counter(f.code for f in _of(package, "ingest_finding"))


def _found(package: IngestPackage, code: str) -> list[str]:
    """The source paths the findings with ``code`` are about."""
    paths = _paths(package)
    out = []
    for finding in _of(package, "ingest_finding"):
        if finding.code == code:
            assert isinstance(finding.subject, EvidenceRef)
            out.append(paths[finding.subject.source])
    return sorted(out)


def test_both_archetypes_are_golden(packages: dict[str, tuple[Path, Path]]) -> None:
    # On failure, run make_archetypes.py and explain the diff in the PR. A mapper, template or
    # mapping change moves these; a compiler adapter change moves the base packages only when they
    # are re-ingested (the Platform harness sees that drift, MVL-181).
    built = {
        f"{name}/lifecycle/{path}": data
        for name, (_, mapped) in packages.items()
        for path, data in A.package_files(mapped, empty=False).items()
    }
    assert built == _tree(A.GOLDEN)


def test_each_base_package_is_the_ingest_of_exactly_these_sources(
    packages: dict[str, tuple[Path, Path]],
) -> None:
    for name, (base, _) in packages.items():
        revisions = _of(read_package(base), "source_revision")  # read_package verifies the hashes
        raw = _tree(A.SOURCES / name)
        assert {r.location.path for r in revisions} == set(raw), name
        for revision in revisions:
            assert revision.content_id == content_id(raw[revision.location.path]), revision


def test_the_corrupt_bag_is_a_finding_not_a_failure(
    packages: dict[str, tuple[Path, Path]],
) -> None:
    base = read_package(packages[A.FLEET][0])
    bag = "runs/S-012/amr-08_2026-05-19/amr-08_2026-05-19_0.mcap"
    assert _found(base, "mcap.truncated") == [bag]
    assert _found(base, "mcap.chunk_truncated") == [bag]
    (error,) = (f for f in _of(base, "ingest_finding") if f.code == "mcap.truncated")
    assert error.severity.value == "error"
    # The whole records before the cut are still read, and the job published the package.
    assert [r.location.path for r in _of(base, "source_revision")].count(bag) == 1
    other_runs = [p for p in _paths(base).values() if p.endswith(".mcap") and p != bag]
    assert len(other_runs) == 6
    assert not any(
        f.severity.value == "error" for f in _of(base, "ingest_finding") if f.code != error.code
    )


def test_the_stale_config_is_a_finding_and_a_stated_revision(
    packages: dict[str, tuple[Path, Path]],
) -> None:
    base = read_package(packages[A.FLEET][0])
    assert _found(base, "config.duplicate_key") == ["config/AMR-09/nav2_params.yaml"]
    paths = _paths(base)
    revisions = {
        paths[v.provenance.evidence.source].split("/")[1]: v.text.value
        for v in _of(base, "configuration_value")
        if v.path == ("config_revision",) and isinstance(v.provenance, Provenance)
    }
    assert revisions == {f"AMR-{n:02d}": "12" for n in range(5, 11)} | {"AMR-09": "11"}
    # The change record states AMR-09 went to 4.3.1: Deploy keeps both facts and decides nothing.
    mapped = read_package(packages[A.FLEET][1])
    paths_of_mapped = _paths(mapped)
    (change,) = (
        r
        for r in _of(mapped, "change_record")
        if any(isinstance(m, Known) and m.value.value == "AMR-09" for m in r.machines.value)
    )
    assert change.changes.value[0].after.value == "4.3.1"
    # Deploy states no comparison: nothing in the mapped package cites a config, and no lifecycle
    # record cites a log (a finding may name the bag's metadata table it could not map).
    cited = {paths_of_mapped[source] for r in mapped.records for source in _sources(r)}
    assert not {path for path in cited if path.startswith("config/")}
    lifecycle = {
        paths_of_mapped[source]
        for r in mapped.records
        if isinstance(r, LIFECYCLE_KINDS)
        for source in _sources(r)
    }
    assert not {path for path in lifecycle if path.startswith("runs/")}


def test_the_fleet_lifecycle_package(packages: dict[str, tuple[Path, Path]]) -> None:
    mapped = read_package(packages[A.FLEET][1])
    kinds = Counter(r.kind for r in mapped.records if isinstance(r, LIFECYCLE_KINDS))
    assert kinds == {
        "authorisation_envelope": 4,
        "change_record": 6,
        "incident_record": 2,
        "maintenance_event": 12,
        "requalification_record": 2,
    }
    machines = {
        m.value.value
        for r in mapped.records
        if r.kind in ("maintenance_event", "change_record", "requalification_record")
        for m in r.machines.value
        if isinstance(m, Known)
    }
    assert machines == {f"AMR-{n:02d}" for n in range(5, 11)}
    sites = {
        r.site.value.value
        for r in mapped.records
        if r.kind in ("authorisation_envelope", "incident_record") and isinstance(r.site, Known)
    }
    assert sites == {"S-007", "S-012"}


def test_the_cell_lifecycle_package(packages: dict[str, tuple[Path, Path]]) -> None:
    mapped = read_package(packages[A.CELL][1])
    kinds = Counter(r.kind for r in mapped.records if isinstance(r, LIFECYCLE_KINDS))
    assert kinds == {
        "change_record": 2,
        "commissioning_baseline": 1,
        "incident_record": 1,
        "maintenance_event": 5,  # four work orders and the tool-change SOP
        "requalification_record": 3,
        "risk_assessment": 1,
    }
    base = read_package(packages[A.CELL][0])
    assert len(_of(base, "run")) == 2  # the bag's metadata and its storage file
    snapshots = [p for p in _paths(base).values() if p.startswith("calibration/")]
    assert len(snapshots) == 4


def test_lifecycle_values_are_stated_and_cite_their_source(
    packages: dict[str, tuple[Path, Path]],
) -> None:
    for name, (_, mapped_root) in packages.items():
        mapped = read_package(mapped_root)
        ledger = set(_paths(mapped))
        checked = 0
        for record in mapped.records:
            if not isinstance(record, LIFECYCLE_KINDS):
                continue
            assert record.provenance.assertion_kind is AssertionKind.STATED, name
            assert record.provenance.evidence.source in ledger
            checked += 1
            for value in vars(record).values():
                states = value if isinstance(value, tuple) else (value,)
                for state in states:
                    if isinstance(state, Known) and isinstance(state.provenance, Provenance):
                        assert state.provenance.assertion_kind is AssertionKind.STATED
                        assert state.provenance.evidence.source in ledger
        assert checked >= 10
        for finding in _of(mapped, "ingest_finding"):
            assert finding.code.startswith(("deploy_lifecycle_map.", "deploy_document_map."))
    assert MAPPER_ID == "deploy_lifecycle_map"


def test_the_mapper_neither_changes_the_base_nor_varies(
    packages: dict[str, tuple[Path, Path]], tmp_path: Path
) -> None:
    for name, (base, mapped) in packages.items():
        before = _tree(base)
        again = tmp_path / name
        A.lifecycle(base, A.PIPELINES[name], again)
        assert _tree(base) == before
        assert _tree(again) == _tree(mapped)
