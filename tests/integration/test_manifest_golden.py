"""The manifest's golden packages: three embodiments ingested under a manifest (ADR 0072).

A manipulator cell, an aerial survey and an AMR fleet, each a real folder ingested by the real job
(sandbox, grouping, binding). Their records are compatibility-sensitive output (ADR 0003): any
change is an explained golden diff. Every run names its declared machine through a stated record
citing the manifest, every pin is a stated binding, and each package is schema version 9.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest
from jsonschema import Draft202012Validator

from neptune.identity import canonical_json
from neptune.manifest import parse_bytes
from neptune.model.alignment import SnapshotBinding
from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import AssertionKind, Known
from neptune.model.machine import Machine
from neptune.model.provenance import JsonPointer
from neptune.model.record import SCHEMA_VERSION
from neptune.model.run import Run, RunDeclaration
from neptune.model.schema import canonical_schema
from neptune.model.world import Site
from neptune.store.package import MANIFEST

pytestmark = pytest.mark.integration

GOLDEN: Final = Path(__file__).parents[1] / "golden" / "manifest"


def _load() -> ModuleType:
    path = GOLDEN / "make_manifest_golden.py"
    spec = importlib.util.spec_from_file_location("make_manifest_golden", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_manifest_golden"] = module
    spec.loader.exec_module(module)
    return module


MAKER: Final = _load()


def committed() -> dict[str, bytes]:
    return {
        path.relative_to(GOLDEN).as_posix(): path.read_bytes()
        for path in sorted(GOLDEN.rglob("*"))
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }


def records(name: str) -> list[Any]:
    """Every record of one golden package, through the compiler's strict readers."""
    manifest = canonical_json.loads((GOLDEN / name / MANIFEST).read_bytes())
    assert isinstance(manifest, dict)
    assert manifest["schema_version"] == SCHEMA_VERSION == 9
    found: list[Any] = []
    for path in sorted((GOLDEN / name / "records").glob("*.jsonl")):
        _, read = RECORD_KINDS[path.stem]
        found += [read(canonical_json.loads(line)) for line in path.read_bytes().splitlines()]
    return found


def test_the_golden_packages_are_what_ingesting_the_folders_gives() -> None:
    # On failure: run `make examples`, bump the manifest transform's version if its output
    # changed, and explain the diff in the PR.
    assert committed() == MAKER.build()


@pytest.mark.parametrize("name", MAKER.NAMES)
def test_every_run_names_its_declared_machine_through_a_stated_record(name: str) -> None:
    text = MAKER.FOLDERS[name]["neptune.yaml"]
    manifest = parse_bytes(text, "neptune.yaml")
    found = records(name)
    validator = Draft202012Validator(canonical_schema())
    for record in found:
        line = canonical_json.loads(canonical_json.dumps(record.to_json()))
        assert list(validator.iter_errors(line)) == []
    runs = [r for r in found if isinstance(r, Run)]
    declarations = {r.run: r for r in found if isinstance(r, RunDeclaration)}
    assert runs and set(declarations) == {run.id for run in runs}
    declared_machine = {decl.name: decl.machine for decl in manifest.runs}
    paths = {r.content_id: r.location.path for r in found if r.kind == "source_revision"}
    for run in runs:
        declaration = declarations[run.id]
        assert declaration.provenance.assertion_kind is AssertionKind.STATED
        assert paths[declaration.provenance.evidence.source] == "neptune.yaml"
        assert isinstance(declaration.logical_id, Known) and isinstance(declaration.machine, Known)
        name_of_run = declaration.logical_id.value.value
        assert declaration.machine.value.value == declared_machine[name_of_run]
        (pointer, _) = declaration.provenance.evidence.locator
        assert isinstance(pointer, JsonPointer)
    machines = [r for r in found if isinstance(r, Machine)]
    sites = [r for r in found if isinstance(r, Site)]
    assert len(machines) == len(manifest.section("machines"))
    assert len(sites) == len(manifest.section("sites"))
    pins = sum(len(decl.snapshots) for decl in manifest.runs)
    stated = [
        b
        for b in found
        if isinstance(b, SnapshotBinding) and paths[b.provenance.evidence.source] == "neptune.yaml"
    ]
    assert len(stated) == pins  # one snapshot per pinned file, one run per entry here
    unresolved = {
        (f.details["run"], f.details["snapshot_kind"])
        for f in found
        if f.kind == "ingest_finding" and f.code == "neptune.bindings.snapshot_unresolved"
    }
    for binding in stated:  # a pinned kind is bound: never also unresolved
        assert (binding.run, str(binding.snapshot_kind)) not in unresolved
