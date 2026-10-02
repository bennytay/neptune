"""The two deployment archetypes, end to end: compiler ingest, then the Deploy mapper (ADR 0004).

``fixtures/archetypes/make_archetypes.py`` writes the sources (an AMR fleet and a manipulator cell),
runs ``IngestJob``-equivalent ingestion through the SDK and the Deploy mapper over each base
package, and keeps the receipts as golden files. The fast tests check the generator and the
sources. The slow ones run the pipeline and check the golden files, that the corrupt bag and the
stale config are findings and not failures, and that every lifecycle value is ``stated`` and cites
the export, form or span it came from.
"""

import importlib.util
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

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


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


# --- The generator and the sources (fast) ---------------------------------------------------------


def test_the_committed_sources_are_what_the_generator_writes() -> None:
    # On failure, run make_archetypes.py and explain the diff in the PR.
    assert A.build() == _tree(A.SOURCES)


def test_generation_is_deterministic() -> None:
    assert A.build() == A.build()


def test_every_committed_fixture_file_is_under_512_kib() -> None:
    sizes = {path: len(data) for path, data in _tree(ARCHETYPES).items()}
    assert max(sizes.values()) < MAX_BYTES, max(sizes.items(), key=lambda item: item[1])


def _names(deployment: str) -> set[str]:
    return {
        path.removeprefix(f"{deployment}/") for path in A.build() if path.startswith(deployment)
    }


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


def test_the_archetypes_are_not_drones() -> None:
    """Every embodiment here is a ground vehicle or an arm (root AGENTS.md: every robot)."""
    suffixes = {Path(path).suffix for path in A.build()}
    assert not suffixes & {".ulg", ".px4", ".tlog", ".bin", ".bag"}


# --- The pipeline (slow: a real ingest job per deployment) ----------------------------------------


@pytest.fixture(scope="module")
def packages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path]]:
    work = tmp_path_factory.mktemp("archetypes")
    return {name: A.pipeline(name, A.SOURCES, work) for name in A.PIPELINES}


def _of(package: IngestPackage, kind: str) -> list[Any]:
    return [r for r in package.records if r.kind == kind]


def _paths(package: IngestPackage) -> dict[Any, str]:
    return {r.content_id: r.location.path for r in _of(package, "source_revision")}


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


@pytest.mark.slow
@pytest.mark.integration
def test_both_archetypes_are_golden(packages: dict[str, tuple[Path, Path]]) -> None:
    # On failure, run make_archetypes.py and explain the diff in the PR (a compiler adapter change
    # reaches the golden base receipts here, which is the point).
    built: dict[str, bytes] = {}
    for name, (base, mapped) in packages.items():
        built.update(A.golden_files(name, base, mapped))
    assert built == _tree(A.GOLDEN)


@pytest.mark.slow
@pytest.mark.integration
def test_every_source_lands_as_records_or_findings(
    packages: dict[str, tuple[Path, Path]],
) -> None:
    for name, (base, _) in packages.items():
        package = read_package(base)
        sources = set(_paths(package).values())
        assert sources == set(_tree(A.SOURCES / name)), name


@pytest.mark.slow
@pytest.mark.integration
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


@pytest.mark.slow
@pytest.mark.integration
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
    (change,) = (
        r
        for r in _of(mapped, "change_record")
        if any(isinstance(m, Known) and m.value.value == "AMR-09" for m in r.machines)
    )
    assert change.changes[0].after.value == "4.3.1"
    assert not any(isinstance(r, LIFECYCLE_KINDS) and "stale" in repr(r) for r in mapped.records)


@pytest.mark.slow
@pytest.mark.integration
def test_the_fleet_lifecycle_package(packages: dict[str, tuple[Path, Path]]) -> None:
    mapped = read_package(packages[A.FLEET][1])
    kinds = Counter(r.kind for r in mapped.records if isinstance(r, LIFECYCLE_KINDS))
    assert kinds == {
        "authorisation_envelope": 4,
        "change_record": 6,
        "incident_record": 2,
        "maintenance_event": 11,
        "requalification_record": 2,
    }
    machines = {
        m.value.value
        for r in mapped.records
        if r.kind in ("maintenance_event", "change_record", "requalification_record")
        for m in r.machines
        if isinstance(m, Known)
    }
    assert machines == {f"AMR-{n:02d}" for n in range(5, 11)}
    sites = {
        r.site.value.value
        for r in mapped.records
        if r.kind in ("authorisation_envelope", "incident_record") and isinstance(r.site, Known)
    }
    assert sites == {"S-007", "S-012"}


@pytest.mark.slow
@pytest.mark.integration
def test_the_cell_lifecycle_package(packages: dict[str, tuple[Path, Path]]) -> None:
    mapped = read_package(packages[A.CELL][1])
    kinds = Counter(r.kind for r in mapped.records if isinstance(r, LIFECYCLE_KINDS))
    assert kinds == {
        "change_record": 2,
        "commissioning_baseline": 1,
        "incident_record": 1,
        "maintenance_event": 4,
        "requalification_record": 2,
        "risk_assessment": 1,
    }
    base = read_package(packages[A.CELL][0])
    assert len(_of(base, "run")) == 2  # the bag's metadata and its storage file
    snapshots = [p for p in _paths(base).values() if p.startswith("calibration/")]
    assert len(snapshots) == 4


@pytest.mark.slow
@pytest.mark.integration
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


@pytest.mark.slow
@pytest.mark.integration
def test_the_mapper_neither_changes_the_base_nor_varies(
    packages: dict[str, tuple[Path, Path]], tmp_path: Path
) -> None:
    for name, (base, mapped) in packages.items():
        before = _tree(base)
        again = tmp_path / name
        A.lifecycle(base, A.PIPELINES[name], again)
        assert _tree(base) == before
        assert _tree(again) == _tree(mapped)
