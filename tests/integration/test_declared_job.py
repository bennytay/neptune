"""MVL-33 acceptance, end to end: site manifests, registers, SOPs, briefs, requirements and work
orders across embodiments become typed records through a real job, each adapter call sandboxed.

Later reasoning asks which source stated a requirement or an asset's identity from the package
alone: every answer cites the exact text or cell, and a second job writes the same package.
"""

import shutil
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.builtin import default_registry
from neptune.derived.sessions import read_derived
from neptune.model.ids import LogicalId
from neptune.model.knowledge import Known
from neptune.model.provenance import JsonPointer, RowCell, Span, TransformRecord
from neptune.model.source import LocalPath, SourceRevision
from neptune.model.task import Requirement, SOPSection, TaskBrief, WorkOrder
from neptune.model.world import Asset, DocumentRecord, Site
from neptune.runtime import IngestJob, Isolation, JobOptions, JobState
from neptune.store.package import read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "declared"
CONFIG: Final = {"tabular": {"csv_header": "first_row"}}  # the register's header is declared


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "fleet"
    root.mkdir()
    for path in FIXTURES.iterdir():
        if path.is_file() and path.suffix != ".py":
            shutil.copy(path, root / path.name)
    return root


def ingest(root: Path, tmp_path: Path, name: str) -> tuple[Any, Any]:
    job = IngestJob(
        root,
        tmp_path / name,
        Workspace(tmp_path / f"{name}-home"),
        default_registry(),
        JobOptions(config=CONFIG),
    )
    outcome = job.run()
    assert outcome.state is JobState.COMMITTED
    return outcome, read_package(tmp_path / name)


def by_kind(package: Any, kind: type) -> list[Any]:
    return [record for record in package.records if isinstance(record, kind)]


def ids(records: list[Any]) -> set[str]:
    return {record.identifiers[0].value.value for record in records}


def test_declared_records_answer_who_stated_what_from_the_package_alone(
    corpus: Path, tmp_path: Path
) -> None:
    outcome, package = ingest(corpus, tmp_path, "package")
    paths = {
        r.content_id: r.location.path
        for r in package.records
        if isinstance(r, SourceRevision) and isinstance(r.location, LocalPath)
    }
    texts = {cid: (corpus / path).read_bytes().decode() for cid, path in paths.items()}

    assert ids(by_kind(package, Site)) == {"WH-3", "FIELD-7"}
    assert ids(by_kind(package, Asset)) == {"DOCK-2", "AISLE-14", "AMR-07", "AMR-09", "RACK-A1"}
    assert ids(by_kind(package, TaskBrief)) == {
        "PICK-NIGHT",
        "TB-2026-117",
        "HULL-SURVEY-9",
        "ORCH-5",
    }
    assert ids(by_kind(package, WorkOrder)) == {"WO-5531", "WO-1"}
    assert len(by_kind(package, SOPSection)) == 2
    requirements = {r.identifiers[0].value.value: r for r in by_kind(package, Requirement)}
    assert set(requirements) == {
        "PICK-R1",
        "CELL-SAF-3",
        "INS-1",
        "INS-2",
        "ROV-R1",
        "ROV-R2",
        "ROV-R3",
        "AG-1",
    }

    # "Which source stated requirement INS-1?" - its record cites the exact span of its text.
    ins = requirements["INS-1"]
    source = ins.provenance.evidence.source
    assert paths[source] == "inspection_drone_brief.md"
    (span,) = ins.text.provenance.evidence.locator
    assert isinstance(span, Span)
    assert texts[source][span.start : span.end] == "The aircraft shall hold 25 m above the panels."
    assert ins.task.value == LogicalId("task", "TB-2026-117")
    documents = {d.id: d for d in by_kind(package, DocumentRecord)}
    assert documents[ins.declared_in].provenance.evidence.source == source

    # A table row's requirement cites its cell; a manifest's nested one inherits its task.
    rov = requirements["ROV-R1"]
    assert rov.task.value == LogicalId("task", "HULL-SURVEY-9")
    pick = requirements["PICK-R1"]
    assert pick.task.value == LogicalId("task", "PICK-NIGHT")
    assert isinstance(pick.provenance.evidence.locator[-1], JsonPointer)

    # "Which source stated AMR-07's identity?" - the register's row, cell by cell.
    (tugger,) = [a for a in by_kind(package, Asset) if a.identifiers[0].value.value == "AMR-07"]
    assert paths[tugger.provenance.evidence.source] == "warehouse_amr_assets.csv"
    serial = next(i for i in tugger.identifiers if i.value.namespace == "serial")
    (cell,) = serial.provenance.evidence.locator
    assert cell == RowCell(1, 6, "serial_number")
    # A site manifest's nested asset is at that site; the site keeps its stated position.
    (dock,) = [a for a in by_kind(package, Asset) if a.identifiers[0].value.value == "DOCK-2"]
    assert dock.site.value == LogicalId("site", "WH-3")
    (warehouse,) = [s for s in by_kind(package, Site) if s.identifiers[0].value.value == "WH-3"]
    assert isinstance(warehouse.location, Known)
    assert warehouse.location.value.latitude == -33.8121

    # The work order names its task and is no brief; the procedure's steps name the procedure.
    (order,) = [w for w in by_kind(package, WorkOrder) if w.identifiers[0].value.value == "WO-5531"]
    assert order.task.value == LogicalId("task", "PICK-NIGHT")
    assert all(
        s.procedure.value == LogicalId("procedure", "SOP-CELL-04")
        for s in by_kind(package, SOPSection)
    )

    # Candidates are derived and inferred; malformed declarations are findings, not failures.
    candidates = [r for r in read_derived(package.derived) if r.kind == "declared_candidate"]
    assert {(c.proposes, c.rule) for c in candidates} == {
        ("sop_section", "numbered_heading_in_procedure"),
        ("requirement", "modal_sentence_without_label"),
    }
    codes = sorted(
        r.code for r in package.records if getattr(r, "code", "").startswith("declared.")
    )
    assert codes == [
        "declared.coordinate_not_decimal",
        "declared.label_repeated",
        "declared.requirement_without_text",
        "declared.section_not_entries",
        "declared.unnamed_declaration",
        "declared.unnamed_declaration",
    ]
    # Neptune's own manifest shape is ADR 0047's: no site is read from it.
    assert "SHOULD-NOT-BE-READ" not in ids(by_kind(package, Site))
    transforms = [
        t for t in by_kind(package, TransformRecord) if t.adapter_id == "neptune.declared"
    ]
    assert {len(t.upstream) for t in transforms} == {1}
    assert outcome.package is not None


def test_a_second_job_writes_the_same_package(corpus: Path, tmp_path: Path) -> None:
    first, _ = ingest(corpus, tmp_path, "one")
    second, _ = ingest(corpus, tmp_path, "two")
    assert first.package == second.package
    assert (tmp_path / "one" / "records" / "requirement.jsonl").read_bytes() == (
        tmp_path / "two" / "records" / "requirement.jsonl"
    ).read_bytes()


def test_the_pass_loads_no_log_twice_and_some_never(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copy(FIXTURES.parent / "series" / "imu.mcap", corpus / "imu.mcap")
    shutil.copy(FIXTURES.parent / "rosbag1" / "robot_none.bag", corpus / "robot.bag")
    workspace = Workspace(tmp_path / "with-logs-home")
    job = IngestJob(
        corpus,
        tmp_path / "with-logs",
        workspace,
        default_registry(),
        JobOptions(config=CONFIG, isolation=Isolation.IN_PROCESS),
    )
    assert job.run().state is JobState.COMMITTED
    package = read_package(tmp_path / "with-logs")
    paths = {
        r.location.path: r.content_id
        for r in package.records
        if isinstance(r, SourceRevision) and isinstance(r.location, LocalPath)
    }
    chunks: dict[str, set[str]] = {}
    for content, transform in job._ingested:
        plan = workspace.load_plan(content, transform)
        assert plan is not None
        chunks.setdefault(content, set()).update(str(chunk["id"]) for chunk in plan.chunks)
    loads: list[str] = []
    load = Workspace.load

    def spy(self: Workspace, chunk: str) -> Any:
        loads.append(chunk)
        return load(self, chunk)

    monkeypatch.setattr(Workspace, "load", spy)
    job._declared_records()
    mcap, bag = chunks[paths["imu.mcap"]], chunks[paths["robot.bag"]]
    assert not bag & set(loads)  # a ROS 1 bag's adapter declares none of the pass's kinds
    assert sum(chunk in mcap for chunk in loads) == len(mcap)  # read once: no register rows
    register = chunks[paths["warehouse_amr_assets.csv"]]
    assert sum(chunk in register for chunk in loads) == 2 * len(register)  # tables, then rows
