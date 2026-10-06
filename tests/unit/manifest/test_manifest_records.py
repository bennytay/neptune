"""Manifest declarations as stated records (ADR 0072), over the golden packages' own records.

Each case reads the records and layout of one golden package (``tests/golden/manifest``: real
ingests, nothing mocked), pairs them with a manifest, and checks what ``declared_records`` makes:
the goldens' own tables again from their own manifests, in any input order; and, from edited
manifests, every finding of ADR 0072 §5.
"""

import importlib.util
import random
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.discovery.layout import Layout, LayoutFile, LayoutLink, layout_of
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.manifest import LoadedManifest, parse_bytes
from neptune.manifest.records import (
    BINDING_STEP,
    MACHINE_CONTRADICTS_RUN,
    PIN_NOT_A_SNAPSHOT,
    PIN_UNRESOLVED,
    RUN_DECLARED_TWICE,
    RUN_STEP,
    RUN_UNRECORDED,
    ManifestRecords,
    declared_records,
)
from neptune.model.alignment import SnapshotBinding, SnapshotKind
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import LogicalId
from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import AssertionKind, Known, NotCovered, Unknown
from neptune.model.machine import Machine
from neptune.model.provenance import AdapterLocator, JsonPointer, Provenance
from neptune.model.run import Run, RunDeclaration
from neptune.model.source import LocalPath, SourceRevision
from neptune.model.world import Site

GOLDEN: Final = Path(__file__).parents[2] / "golden" / "manifest"
KINDS: Final = ("machine", "site", "run_declaration", "snapshot_binding")


def _maker() -> ModuleType:
    path = GOLDEN / "make_manifest_golden.py"
    spec = importlib.util.spec_from_file_location("manifest_records_maker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["manifest_records_maker"] = module
    spec.loader.exec_module(module)
    return module


MAKER: Final = _maker()


def golden(name: str) -> list[Any]:
    found: list[Any] = []
    for path in sorted((GOLDEN / name / "records").glob("*.jsonl")):
        _, read = RECORD_KINDS[path.stem]
        found += [read(canonical_json.loads(line)) for line in path.read_bytes().splitlines()]
    return found


def layout(records: list[Any]) -> Layout:
    return layout_of(
        LayoutFile(r.id, r.location, r.content_id)
        for r in records
        if isinstance(r, SourceRevision) and isinstance(r.location, LocalPath)
    )


def loaded(text: bytes) -> LoadedManifest:
    return LoadedManifest(
        parse_bytes(text, "neptune.yaml"), LocalPath("neptune.yaml"), content_id(text), len(text)
    )


def made(name: str, text: bytes | None = None, extra: Layout | None = None) -> ManifestRecords:
    records = golden(name)
    manifest = loaded(text if text is not None else MAKER.FOLDERS[name]["neptune.yaml"])
    return declared_records(manifest, records, extra if extra is not None else layout(records))


def codes(found: ManifestRecords) -> list[str]:
    return sorted(f.code for f in found.findings)


@pytest.mark.parametrize("name", MAKER.NAMES)
def test_the_goldens_tables_are_what_their_manifests_declare_in_any_order(name: str) -> None:
    records = golden(name)
    manifest = loaded(MAKER.FOLDERS[name]["neptune.yaml"])
    expected = [
        r for r in records if r.kind in KINDS and r.provenance.transform == manifest.transform.id
    ]
    first = declared_records(manifest, records, layout(records))
    assert sorted(first.records, key=lambda r: r.id) == sorted(expected, key=lambda r: r.id)
    assert first.findings == ()
    shuffled = list(records)
    random.Random(7).shuffle(shuffled)
    assert declared_records(manifest, shuffled, layout(records)) == first
    assert declared_records(manifest, reversed(records), layout(records)) == first


def test_every_record_is_stated_and_cites_the_manifest() -> None:
    found = made("amr_fleet")
    manifest = loaded(MAKER.FOLDERS["amr_fleet"]["neptune.yaml"])
    for record in found.records:
        assert record.provenance.assertion_kind is AssertionKind.STATED
        assert record.provenance.evidence.source == manifest.content_id
        assert record.provenance.transform == manifest.transform.id
    machines = [r for r in found.records if isinstance(r, Machine)]
    identifiers = set()
    for machine in machines:
        (ident,) = machine.identifiers
        assert isinstance(ident, Known) and ident.provenance != machine.provenance
        identifiers.add(ident.value)
    assert identifiers == {LogicalId("manifest", "AMR-01"), LogicalId("manifest", "AMR-02")}
    for machine in machines:
        assert machine.manufacturer == NotCovered() and machine.model == NotCovered()
    (site,) = [r for r in found.records if isinstance(r, Site)]
    assert isinstance(site.name, Known) and site.name.value == "Northgate distribution centre"
    assert site.parent == NotCovered() and site.location == NotCovered()


def test_one_declaration_per_run_and_one_binding_per_run_and_pin() -> None:
    found = made("amr_fleet")
    runs = {r.id for r in golden("amr_fleet") if isinstance(r, Run)}
    declarations = [r for r in found.records if isinstance(r, RunDeclaration)]
    assert {d.run for d in declarations} == runs and len(declarations) == 2
    for declaration in declarations:
        pointer, step = declaration.provenance.evidence.locator
        assert isinstance(pointer, JsonPointer) and pointer.pointer.startswith("/runs/")
        assert step == AdapterLocator(RUN_STEP, (("run", declaration.run),))
        assert isinstance(declaration.machine, Known)
        machine = declaration.machine.value
        assert machine.namespace == "manifest" and machine.value in {"AMR-01", "AMR-02"}
        assert isinstance(declaration.task, Unknown)
    bindings = [r for r in found.records if isinstance(r, SnapshotBinding)]
    assert {b.run for b in bindings} == runs
    assert {b.snapshot_kind for b in bindings} == {SnapshotKind.CONFIGURATION_SNAPSHOT}
    assert all(b.validity == Unknown() for b in bindings)
    for binding in bindings:
        _, step = binding.provenance.evidence.locator
        assert isinstance(step, AdapterLocator) and step.kind == BINDING_STEP


def test_a_declared_alias_that_is_not_the_runs_own_machine_is_a_contradiction() -> None:
    text = MAKER.FOLDERS["aerial_survey"]["neptune.yaml"].replace(
        MAKER.SYS_UUID.encode(), b"0000000000000000000000000000ffff"
    )
    found = made("aerial_survey", text)
    (finding,) = found.findings
    assert finding.code == MACHINE_CONTRADICTS_RUN
    assert finding.category is FindingCategory.INCONSISTENT and finding.severity is Severity.WARNING
    assert finding.details["stated"] == {
        "namespace": "px4.sys_uuid",
        "value": MAKER.SYS_UUID,
    }
    # Both stand: the declaration is still made.
    assert any(isinstance(r, RunDeclaration) for r in found.records)


def test_an_alias_in_another_namespace_contradicts_nothing() -> None:
    text = MAKER.FOLDERS["aerial_survey"]["neptune.yaml"].replace(b"px4.sys_uuid", b"serial")
    assert made("aerial_survey", text).findings == ()


FLIGHT: Final = b"""\
neptune: 1
machines:
  - {id: quad}
runs:
%s
"""


def flight(*entries: bytes) -> bytes:
    return FLIGHT % b"\n".join(entries)


def test_two_entries_covering_one_run_both_stand() -> None:
    found = made(
        "aerial_survey",
        flight(
            b"  - {name: a, paths: [flights], machine: quad}",
            b"  - {name: b, paths: [flights/flight.ulg]}",
        ),
    )
    assert codes(found) == [RUN_DECLARED_TWICE]
    (finding,) = found.findings
    assert finding.category is FindingCategory.AMBIGUOUS
    assert finding.details["manifest_pointers"] == ["/runs/0", "/runs/1"]
    assert len([r for r in found.records if isinstance(r, RunDeclaration)]) == 2


def test_an_entry_holding_no_recording_is_said_of_no_run() -> None:
    found = made("aerial_survey", flight(b"  - {name: a, paths: [params], machine: quad}"))
    assert codes(found) == [RUN_UNRECORDED]
    assert not any(isinstance(r, RunDeclaration) for r in found.records)
    # An entry holding nothing at all is grouping's ``declaration_unmatched``, not this.
    assert made("aerial_survey", flight(b"  - {name: a, paths: [nothing]}")).findings == ()


@pytest.mark.parametrize(
    ("pin", "code", "says"),
    [
        (b"{path: params/missing.yaml}", PIN_UNRESOLVED, "not a file the job read"),
        (b"{path: params}", PIN_UNRESOLVED, "a directory"),
        (b"{path: params/link.yaml}", PIN_UNRESOLVED, "a symlink"),
        (b"{content: 'sha256:" + b"0" * 64 + b"'}", PIN_UNRESOLVED, "no file the job read"),
        (b"{path: flights/flight.ulg}", PIN_NOT_A_SNAPSHOT, "no configuration"),
    ],
)
def test_a_pin_that_binds_nothing_is_a_finding(pin: bytes, code: str, says: str) -> None:
    records = golden("aerial_survey")
    base = layout(records)
    link = LayoutLink(LocalPath("params/link.yaml"), b"../../outside/params.yaml")
    text = flight(b"  - {name: a, paths: [flights], snapshots: [%s]}" % pin)
    found = declared_records(loaded(text), records, Layout(base.files, (link,)))
    (finding,) = found.findings
    assert finding.code == code and says in finding.message
    assert finding.category is FindingCategory.MISSING
    assert finding.details["manifest_pointer"] == "/runs/0/snapshots/0"
    assert not any(isinstance(r, SnapshotBinding) for r in found.records)
    assert any(isinstance(r, RunDeclaration) for r in found.records)  # the rest still applies


def test_findings_are_the_manifest_transforms_and_cite_its_bytes() -> None:
    text = flight(b"  - {name: a, paths: [flights], snapshots: [{path: gone.yaml}]}")
    manifest = loaded(text)
    records = golden("aerial_survey")
    (finding,) = declared_records(manifest, records, layout(records)).findings
    assert isinstance(finding, IngestFinding)
    assert finding.transform == manifest.transform.id
    assert finding.subject == manifest.cite("/runs/0/snapshots/0")


def test_a_machines_aliases_are_its_identifiers_each_citing_where_it_is_written() -> None:
    (machine,) = [r for r in made("manipulator_cell").records if isinstance(r, Machine)]
    cited = {
        ident.value: ident.provenance.evidence.locator
        for ident in machine.identifiers
        if isinstance(ident, Known) and isinstance(ident.provenance, Provenance)
    }
    assert cited == {
        LogicalId("manifest", "ur5e-cell-3"): (JsonPointer("/machines/0/id"),),
        LogicalId("serial", "20235400123"): (JsonPointer("/machines/0/aliases/serial"),),
    }
    assert machine.provenance.evidence.locator == (JsonPointer("/machines/0"),)
