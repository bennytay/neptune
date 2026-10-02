"""MVL-201 acceptance, end to end: workbooks become cited tables, hostile ones become findings.

One folder holds work orders of a mobile-robot fleet, a manipulator cell's change log, a 1904-epoch
inspection log, a formula workbook, a macro-enabled workbook with external links, and the broken
and hostile ones (truncated, a sheet cut mid-row, a zip bomb, a part over the ratio, entities).
The real job reads them through the real sandbox: the job commits, every workbook is read by the
tabular adapter, and each cell of the package read back is found at the place it cites.
"""

import io
import shutil
import zipfile
from pathlib import Path
from typing import Any, Final
from xml.etree import ElementTree

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.registry import AdapterRegistry
from neptune.identity.hashing import content_id
from neptune.model.knowledge import Known, NotCovered
from neptune.model.provenance import AdapterLocator, ByteRange
from neptune.model.world import StructuredRecord, StructuredTable
from neptune.runtime import IngestJob, JobOptions, JobState
from neptune.store.package import read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "tabular"
NAMES: Final = (
    "workorders_amr_fleet.xlsx",
    "changelog_manipulator_cell.xlsx",
    "epoch1904_quadruped.xlsx",
    "formulas_humanoid_energy.xlsx",
    "macro_external_links.xlsm",
    "truncated_workorders.xlsx",
    "damaged_sheet_xml.xlsx",
    "bomb_zeros_sheet.xlsx",
    "bomb_part_ratio.xlsx",
    "hostile_entities_sheet.xlsx",
    "blowup_shared_strings.xlsx",
)


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Any:
    base = tmp_path_factory.mktemp("xlsx")
    root = base / "site"
    root.mkdir()
    for name in NAMES:
        shutil.copy(FIXTURES / name, root / name)
    # the same workbook under a name that says nothing, with a byte after the zip so it is a
    # source of its own (a source is its content)
    data = (FIXTURES / "epoch1904_quadruped.xlsx").read_bytes()
    (root / "renamed_no_extension").write_bytes(data + b"\n")
    job = IngestJob(
        root,
        base / "package",
        Workspace(base / "home"),
        AdapterRegistry(builtin_adapters()),
        JobOptions(),
    )
    assert job.run().state is JobState.COMMITTED  # damage is findings, never a failed job
    return read_package(base / "package"), root


def names_by_source(root: Path) -> dict[str, str]:
    return {str(content_id(p.read_bytes())): p.name for p in root.iterdir()}


def tables_of(read: Any, root: Path, name: str) -> list[StructuredTable]:
    by_path = names_by_source(root)
    return [
        r
        for r in read.records
        if isinstance(r, StructuredTable) and by_path[str(r.provenance.evidence.source)] == name
    ]


def sheet_table(read: Any, root: Path, name: str, sheet: str) -> StructuredTable:
    (found,) = [
        t
        for t in tables_of(read, root, name)
        if isinstance(t.name, Known) and t.name.value == sheet
    ]
    return found


def rows_of(read: Any, table: StructuredTable) -> list[StructuredRecord]:
    found = [r for r in read.records if isinstance(r, StructuredRecord) and r.table == table.id]
    return sorted(found, key=lambda record: record.row)


def test_every_workbook_is_read_by_the_tabular_adapter_through_the_sandbox(package: Any) -> None:
    read, _ = package
    adapters = {t.id: t.adapter_id for t in read.receipt.transforms}
    by_source = {
        str(s.location.to_json()["path"]): sorted({adapters[t] for t in s.read_by})
        for s in read.receipt.sources
    }
    for name in (*NAMES, "renamed_no_extension"):
        assert "tabular" in by_source[name], name


def test_damage_and_hostility_are_findings_and_the_other_workbooks_lose_nothing(
    package: Any,
) -> None:
    read, root = package
    by_path = names_by_source(root)
    found: dict[str, set[str]] = {}
    for finding in (r for r in read.records if r.kind == "ingest_finding"):
        source = getattr(finding.subject, "source", None)
        if str(source) in by_path and finding.code.startswith("tabular."):
            found.setdefault(by_path[str(source)], set()).add(finding.code)
    assert found["truncated_workorders.xlsx"] == {"tabular.xlsx_corrupt"}
    assert found["damaged_sheet_xml.xlsx"] == {"tabular.xlsx_corrupt"}
    assert found["bomb_zeros_sheet.xlsx"] == {"tabular.xlsx_limit"}
    assert found["bomb_part_ratio.xlsx"] == {"tabular.xlsx_limit"}
    assert found["hostile_entities_sheet.xlsx"] == {"tabular.xlsx_part_refused"}
    assert found["macro_external_links.xlsm"] == {
        "tabular.xlsx_external_link",
        "tabular.xlsx_macros",
    }
    assert found["workorders_amr_fleet.xlsx"] == {"tabular.xlsx_number_text"}
    assert "changelog_manipulator_cell.xlsx" not in found  # nothing to say about a clean one
    # the rows before the cut survived (the header is a row: nobody declared one)
    damaged = sheet_table(read, root, "damaged_sheet_xml.xlsx", "Sites")
    assert len(rows_of(read, damaged)) == 3
    # the bomb's table is there, not covered
    (bomb,) = tables_of(read, root, "bomb_zeros_sheet.xlsx")
    assert isinstance(bomb.name, NotCovered)
    orders = sheet_table(read, root, "workorders_amr_fleet.xlsx", "Work Orders")
    assert len(rows_of(read, orders)) == 5


def test_a_cell_traces_to_the_xml_of_the_workbook_it_cites(package: Any) -> None:
    read, root = package
    for name in ("workorders_amr_fleet.xlsx", "formulas_humanoid_energy.xlsx"):
        data = (root / name).read_bytes()
        checked = 0
        for table in tables_of(read, root, name):
            if not (isinstance(table.name, Known) and table.provenance.evidence.locator):
                continue
            for record in rows_of(read, table):
                for column, cell in enumerate(record.cells):
                    span, inside, step = record.cell_evidence(table, column).locator
                    assert isinstance(span, ByteRange) and isinstance(inside, ByteRange)
                    assert isinstance(step, AdapterLocator)
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        (info,) = [i for i in archive.infolist() if i.header_offset == span.offset]
                        part = archive.read(info)
                    xml = part[inside.offset : inside.offset + inside.length]
                    fields = dict(step.fields)
                    assert info.filename == fields["part"]
                    element = ElementTree.fromstring(xml)
                    if fields["content"] != "missing":
                        assert element.get("r") == fields["ref"]
                        if isinstance(cell, Known) and isinstance(cell.value, bool):
                            assert element.findtext("v") == str(int(cell.value))
                    checked += 1
        assert checked > 20, name


def test_the_same_workbook_under_another_name_reads_the_same(package: Any) -> None:
    read, root = package
    named = rows_of(read, sheet_table(read, root, "epoch1904_quadruped.xlsx", "Inspections"))
    renamed = rows_of(read, sheet_table(read, root, "renamed_no_extension", "Inspections"))
    assert len(named) == len(renamed) == 4
    assert [[c.value for c in r.cells if isinstance(c, Known)] for r in named] == [
        [c.value for c in r.cells if isinstance(c, Known)] for r in renamed
    ]
