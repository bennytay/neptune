"""The acceptance corpus's manifest, as stated records (root ADR 0072, MVL-205).

The corpus's ``neptune.yaml`` declares five machines, two sites and nine recorded runs (and five
hand-eye sessions that record no run). Ingested by the compiler, every recording a declared run
holds names its declared machine and site through a stated ``run_declaration`` citing the
manifest, and each declared machine and site is a stated record. The run sheet's pins bind their
runs to the calibrations and configurations they ran with, stated, with no inference: what
Memory's run and configuration consolidators read. The stale cell configuration (gold T2) is never
pinned, and the AMR runs before the firmware rollout are deliberately unpinned.
"""

from pathlib import Path
from typing import Any, Final

import pytest
from harness import acceptance
from harness.acceptance import generate

from neptune.model.alignment import SnapshotBinding, SnapshotKind
from neptune.model.knowledge import AssertionKind, Known
from neptune.model.machine import Machine
from neptune.model.run import Run, RunDeclaration
from neptune.model.world import Site
from neptune.sdk import Neptune, read_package

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# What the corpus's manifest declares, by run name: (machine, site).
DECLARED: Final = {
    "cell3-2026-08-20": ("ARM-3A", "PLANT-2"),
    "cell3-2026-09-09": ("ARM-3A", "PLANT-2"),
    "cell3-2026-09-14": ("ARM-3A", "PLANT-2"),
    "leg01-2026-09-12": ("LEG-01", "PLANT-2"),
    "leg01-2026-09-14": ("LEG-01", "PLANT-2"),
    "amr05-2026-03-03": ("AMR-05", "S-007"),
    "amr06-2026-03-03": ("AMR-06", "S-007"),
    "amr07-2026-04-02": ("AMR-07", "S-007"),
    "amr07-2026-04-15": ("AMR-07", "S-007"),
}
# What the run sheet pins, by run name: the snapshot files (generate.RUNS). The AMR runs before the
# 4.3.1 rollout are deliberately unpinned: their navigation configuration is not in the hand-over.
PINS: Final = {run.name: set(run.snapshots) for run in generate.RUNS if run.snapshots}
UNPINNED: Final = {"amr05-2026-03-03", "amr06-2026-03-03", "amr07-2026-04-02"}


def _ingest(root: Path, out: Path, workspace: Path) -> Any:
    result = Neptune(workspace).ingest(root, out)
    assert result.committed
    return read_package(out)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return acceptance.materialise(tmp_path_factory.mktemp("corpus") / "acceptance")


@pytest.fixture(scope="module")
def package(corpus: Path, tmp_path_factory: pytest.TempPathFactory) -> Any:
    tmp = tmp_path_factory.mktemp("declared")
    return _ingest(corpus, tmp / "package", tmp / "workspace")


def _paths(package: Any) -> dict[Any, list[str]]:
    paths: dict[Any, list[str]] = {}
    for r in package.records:
        if r.kind == "source_revision":
            paths.setdefault(r.content_id, []).append(r.location.path)
    return paths


def test_every_declared_run_names_its_declared_machine_through_a_stated_record(
    package: Any,
) -> None:
    paths = _paths(package)
    runs = {r.id: r for r in package.records if isinstance(r, Run)}
    declarations = [r for r in package.records if isinstance(r, RunDeclaration)]
    named: dict[str, set[tuple[str, str]]] = {}
    for declaration in declarations:
        assert declaration.provenance.assertion_kind is AssertionKind.STATED
        assert paths[declaration.provenance.evidence.source] == ["neptune.yaml"]
        assert declaration.run in runs
        assert isinstance(declaration.logical_id, Known)
        assert isinstance(declaration.machine, Known) and isinstance(declaration.site, Known)
        named.setdefault(declaration.logical_id.value.value, set()).add(
            (declaration.machine.value.value, declaration.site.value.value)
        )
    assert named == {name: {declared} for name, declared in DECLARED.items()}
    # Every run record in the corpus is a declared run's: none is left with no machine.
    assert {d.run for d in declarations} == set(runs)


def test_declared_machines_and_sites_are_stated_records(package: Any) -> None:
    machines = sorted(
        ident.value.value
        for r in package.records
        if isinstance(r, Machine) and r.provenance.assertion_kind is AssertionKind.STATED
        for ident in r.identifiers
        if isinstance(ident, Known) and ident.value.namespace == "manifest"
    )
    assert machines == ["AMR-05", "AMR-06", "AMR-07", "ARM-3A", "LEG-01"]
    sites = {
        (ident.value.value, r.name.value if isinstance(r.name, Known) else None)
        for r in package.records
        if isinstance(r, Site)
        for ident in r.identifiers
        if isinstance(ident, Known) and ident.value.namespace == "manifest"
    }
    assert sites == {("S-007", "Northgate distribution centre"), ("PLANT-2", "Riverside plant 2")}


def test_the_run_sheet_binds_each_pinned_run_stated_and_leaves_the_gaps_unpinned(
    package: Any,
) -> None:
    paths = _paths(package)
    snapshots = {
        r.id: paths[r.provenance.evidence.source]
        for r in package.records
        if r.kind in ("configuration_snapshot", "calibration")
    }
    names = {
        d.run: d.logical_id.value.value
        for d in package.records
        if isinstance(d, RunDeclaration) and isinstance(d.logical_id, Known)
    }
    bound: dict[str, set[str]] = {}
    for binding in package.records:
        if not isinstance(binding, SnapshotBinding):
            continue
        if paths[binding.provenance.evidence.source] != ["neptune.yaml"]:
            continue
        assert binding.provenance.assertion_kind is AssertionKind.STATED
        assert binding.snapshot_kind in (
            SnapshotKind.CONFIGURATION_SNAPSHOT,
            SnapshotKind.CALIBRATION,
        )
        bound.setdefault(names[binding.run], set()).update(snapshots[binding.snapshot])
    assert bound == PINS
    assert not UNPINNED & set(bound)
    # The stale managed export is never pinned (gold trap T2).
    assert all("cell_config.yaml" not in path for pins in bound.values() for path in pins)
    unresolved = {
        f.details["run"]
        for f in package.records
        if f.kind == "ingest_finding"
        and f.code == "neptune.bindings.snapshot_unresolved"
        and f.details["snapshot_kind"] == "configuration_snapshot"
    }
    pinned_runs = {run for run, name in names.items() if name in PINS}
    assert not unresolved & pinned_runs
