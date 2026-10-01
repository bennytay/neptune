"""MVL-30 acceptance, end to end: structured values are queryable and trace to source cells.

One folder holds tables from different embodiments (a mobile base's telemetry CSV, a legged
platform's TSV register, a marine vehicle's JSON Lines log, a manipulator's JSON joint states, a
humanoid's Parquet log) beside a damaged one. The real job reads them through the real sandbox, and
the package read back is checked against independent readers: for each cell, the cited place in
the raw bytes holds the cell's value.
"""

import csv
import io
import json
import shutil
from pathlib import Path
from typing import Any, Final

import pyarrow.parquet as pq
import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.registry import AdapterRegistry
from neptune.identity.hashing import content_id
from neptune.model.knowledge import Known
from neptune.model.provenance import ByteRange, JsonPointer, Row, RowCell
from neptune.model.world import StructuredRecord, StructuredTable
from neptune.runtime import IngestJob, JobOptions, JobState
from neptune.store.package import read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "tabular"
NAMES: Final = (
    "telemetry_amr.csv",
    "inspection_quadruped.tsv",
    "events_auv.jsonl",
    "joint_states_arm.json",
    "humanoid_joints.parquet",
    "damaged.jsonl",
    "truncated.parquet",
)


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Any:
    base = tmp_path_factory.mktemp("tabular")
    root = base / "site"
    root.mkdir()
    for name in NAMES:
        shutil.copy(FIXTURES / name, root / name)
    # the same rows under a name that says nothing, plus a blank line so its bytes differ
    (root / "renamed_no_extension").write_bytes(
        (FIXTURES / "events_auv.jsonl").read_bytes() + b"\n"
    )
    job = IngestJob(
        root,
        base / "package",
        Workspace(base / "home"),
        AdapterRegistry(builtin_adapters()),
        JobOptions(),
    )
    assert job.run().state is JobState.COMMITTED
    return read_package(base / "package"), root


def paths(root: Path) -> dict[str, str]:
    """Each source's path by its content id: what the cited evidence names."""
    return {str(content_id(p.read_bytes())): p.name for p in root.iterdir()}


def tables_of(read: Any, root: Path, name: str) -> list[StructuredTable]:
    by_path = paths(root)
    return [
        r
        for r in read.records
        if isinstance(r, StructuredTable) and by_path[str(r.provenance.evidence.source)] == name
    ]


def rows_of(read: Any, table: StructuredTable) -> list[StructuredRecord]:
    found = [r for r in read.records if isinstance(r, StructuredRecord) and r.table == table.id]
    return sorted(found, key=lambda record: record.row)


def data_table(read: Any, root: Path, name: str) -> StructuredTable:
    (found,) = [t for t in tables_of(read, root, name) if len(t.provenance.evidence.locator) == 1]
    return found


def test_every_table_lands_through_the_sandbox_with_its_adapter(package: Any) -> None:
    read, _ = package
    adapters = {t.id: t.adapter_id for t in read.receipt.transforms}
    by_source = {
        str(s.location.to_json()["path"]): sorted({adapters[t] for t in s.read_by})
        for s in read.receipt.sources
    }
    for name in NAMES + ("renamed_no_extension",):
        assert by_source[name] == ["tabular"], name
    assert read.receipt.records  # record counts are in the receipt
    counts = dict(read.receipt.records)
    assert counts["structured_table"] == 10 and counts["structured_record"] > 70


def test_findings_name_the_damage_and_nothing_else_is_lost(package: Any) -> None:
    read, root = package
    codes = sorted(f.code for f in read.receipt.findings)
    assert "tabular.parquet_footer" in codes  # truncated.parquet
    assert {"tabular.json_syntax", "tabular.json_duplicate_key", "tabular.invalid_utf8"} <= set(
        codes
    )
    assert codes.count("tabular.csv_dialect") == 2  # the CSV and the TSV say how they were read
    # the damaged sources did not cost the others a row
    assert len(rows_of(read, data_table(read, root, "humanoid_joints.parquet"))) == 10
    assert len(rows_of(read, data_table(read, root, "events_auv.jsonl"))) == 4


def test_a_csv_cell_traces_to_the_row_and_column_of_the_raw_file(package: Any) -> None:
    read, root = package
    table = data_table(read, root, "telemetry_amr.csv")
    raw = (root / "telemetry_amr.csv").read_text(encoding="utf-8")
    oracle = list(csv.reader(io.StringIO(raw, newline="")))
    for record in rows_of(read, table):
        for column, cell in enumerate(record.cells):
            step = record.cell_evidence(table, column).locator[-1]
            assert isinstance(step, RowCell) and (step.row, step.column) == (record.row, column)
            wanted = oracle[step.row][step.column]
            assert (cell.value if isinstance(cell, Known) else "") == wanted.strip() or wanted == ""


def test_a_json_cell_traces_to_the_bytes_and_pointer_it_cites(package: Any) -> None:
    read, root = package
    for name in ("events_auv.jsonl", "joint_states_arm.json", "renamed_no_extension"):
        raw = (root / name).read_bytes()
        table = data_table(read, root, name)
        records = rows_of(read, table)
        assert records, name
        for record in records:
            span = record.provenance.evidence.locator[0]
            assert isinstance(span, ByteRange)
            document = json.loads(raw[span.offset : span.offset + span.length])
            for cell in record.cells:
                pointer = cell.provenance.evidence.locator[-1]  # type: ignore[union-attr]
                assert isinstance(pointer, JsonPointer)
                found = document
                for token in pointer.pointer.split("/")[1:]:
                    token = token.replace("~1", "/").replace("~0", "~")
                    found = found[int(token)] if isinstance(found, list) else found[token]
                if (
                    isinstance(cell, Known)
                    and isinstance(cell.value, str)
                    and isinstance(found, int)
                ):
                    assert int(cell.value) == found
                elif isinstance(cell, Known):
                    assert cell.value == found


def test_a_parquet_cell_traces_to_its_row_and_column(package: Any) -> None:
    read, root = package
    table = data_table(read, root, "humanoid_joints.parquet")
    oracle = pq.read_table(root / "humanoid_joints.parquet")
    assert isinstance(table.header, Known)
    joint = table.header.value.index("joint")
    for record in rows_of(read, table):
        assert record.provenance.evidence.locator == (Row(record.row),)
        step = record.cell_evidence(table, joint).locator[-1]
        assert step == RowCell(record.row, joint, "joint")
        assert record.cells[joint].value == oracle.column("joint")[record.row].as_py()  # type: ignore[union-attr]


def test_the_same_folder_packages_to_the_same_bytes(package: Any, tmp_path: Path) -> None:
    read, root = package
    job = IngestJob(
        root,
        tmp_path / "again",
        Workspace(tmp_path / "home"),
        AdapterRegistry(builtin_adapters()),
        JobOptions(),
    )
    assert job.run().state is JobState.COMMITTED
    assert read_package(tmp_path / "again").id == read.id
