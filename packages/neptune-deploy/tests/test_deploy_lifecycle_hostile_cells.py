"""Hostile and lossy cells, large tables and the output path (review of PR #80, ADR 0002 §4, §6).

Variants of the compiler-written fixture packages are built by replacing cell values in their
``StructuredRecord``s (ids derive from evidence, not values, so lineage stays valid) or by cloning
rows at new positions with the ids the compiler's rule gives them.
"""

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from neptune.identity.provenance import evidence_record_id
from neptune.model.knowledge import Known, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, Row
from neptune.store.package import (
    IngestPackage,
    PackageError,
    package_files,
    read_files,
    read_package,
)
from neptune_deploy.lifecycle import map_files, map_package, preset
from neptune_deploy.lifecycle import mapper as mapper_module
from neptune_deploy.lifecycle.mapper import _number, _text

PACKAGES = Path(__file__).parent / "fixtures" / "lifecycle" / "packages"


def _base(name: str) -> IngestPackage:
    return read_package(PACKAGES / name)


def _of(package: IngestPackage, kind: str) -> list[Any]:
    return [r for r in package.records if r.kind == kind]


def _codes(package: IngestPackage, name: str) -> list[Any]:
    return [f for f in _of(package, "ingest_finding") if f.code.endswith(f".{name}")]


def _header(package: IngestPackage, column: str) -> tuple[Any, int]:
    """The table whose header has ``column``, and the column's position."""
    for table in _of(package, "structured_table"):
        if isinstance(table.header, Known) and column in table.header.value:
            return table, table.header.value.index(column)
    raise AssertionError(column)


def _with_cells(package: IngestPackage, column: str, values: dict[int, Any]) -> IngestPackage:
    """``package`` with ``column``'s cell in each given row replaced by ``values[row]``."""
    table, index = _header(package, column)
    records = []
    for record in package.records:
        if record.kind == "structured_record" and record.table == table.id and record.row in values:
            cells = list(record.cells)
            value = values[record.row]
            cells[index] = Unknown() if value is None else Known(value)
            record = replace(record, cells=tuple(cells))
        records.append(record)
    return read_files(package_files(records))


def _mapped(package: IngestPackage, *names: str) -> IngestPackage:
    return read_files(map_files(package, [preset(name) for name in names]))


# --- Numbers are read only when a double holds them exactly ---------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.8", 0.8),
        ("600", 600.0),
        ("-1.5e3", -1500.0),
        (600, 600.0),
        (2**53, float(2**53)),
        ("9007199254740993", None),
        (2**53 + 1, None),
        ("1e-400", None),
        ("1e400", None),
        (10**400, None),
        ("0x10", None),
        (True, None),
        ("", None),
    ],
)
def test_numbers_round_trip_or_are_not_read(value: Any, expected: float | None) -> None:
    assert _number(value) == expected


def test_a_lossy_number_is_unknown_with_a_finding_citing_its_cell() -> None:
    base = _with_cells(_base("warehouse_amr"), "Speed Limit", {1: "9007199254740993", 2: "0.8"})
    base = _with_cells(base, "Payload Max", {1: "1e-400"})
    package = _mapped(base, "register_zone")
    envelopes = {
        e.zones.value[0].zone.value.value: e for e in _of(package, "authorisation_envelope")
    }
    lossy = envelopes["DOCK-1"]
    assert isinstance(lossy.zones.value[0].speed_limit.value, Unknown)
    assert isinstance(lossy.payload_max.value, Unknown)
    assert envelopes["AISLE-14"].zones.value[0].speed_limit.value == Known(
        0.8, envelopes["AISLE-14"].zones.value[0].speed_limit.value.provenance
    )
    unreadable = _codes(package, "value_unreadable")
    fields = sorted(f.details["field"] for f in unreadable)
    assert fields == ["/payload_max/value", "/zones/0/speed_limit/value"]
    assert all(f.records == (lossy.id,) for f in unreadable)
    columns = sorted(f.subject.locator[-1].column_name for f in unreadable)
    assert columns == ["Payload Max", "Speed Limit"]


# --- Typed cells read as text are their canonical JSON text ---------------------------------------


@pytest.mark.parametrize(
    ("value", "text"),
    [(3, "3"), (True, "true"), (False, "false"), (2.5, "2.5"), (1e20, "1e+20"), ("S2", "S2")],
)
def test_typed_cells_are_their_canonical_json_text(value: Any, text: str) -> None:
    assert _text(value) == text


def test_json_scalars_map_uniformly_into_text_fields() -> None:
    base = _base("warehouse_amr")
    records = []
    for record in base.records:
        if record.kind == "structured_record":
            cells = []
            for cell in record.cells:
                grounds = cell.provenance
                pointer = (
                    getattr(grounds.evidence.locator[-1], "pointer", None)
                    if isinstance(grounds, Provenance)
                    else None
                )
                if pointer == "/fields/priority/name" and isinstance(cell, Known):
                    cell = Known(4, cell.provenance)
                cells.append(cell)
            record = replace(record, cells=tuple(cells))
        records.append(record)
    package = _mapped(read_files(package_files(records)), "jira_json")
    assert {i.severity.value for i in _of(package, "incident_record")} == {"4"}


# --- Blank list cells: an Unknown list per cell, citing it ---------------------------------


def _cloned_rows(package: IngestPackage, column: str, count: int) -> IngestPackage:
    """``count`` more rows like the table's first data row, with ``column`` blank in every row."""
    table, index = _header(package, column)
    rows = sorted(
        (r for r in _of(package, "structured_record") if r.table == table.id), key=lambda r: r.row
    )
    transforms = {t.id: t for t in _of(package, "transform_record")}
    template = rows[0]
    added = []
    for n in range(count):
        row = rows[-1].row + 1 + n
        evidence = EvidenceRef(template.provenance.evidence.source, (Row(row),))
        transform = transforms[template.provenance.transform]
        added.append(
            replace(
                template,
                id=evidence_record_id("structured_record", evidence, transform),
                provenance=replace(template.provenance, evidence=evidence),
                row=row,
            )
        )
    records = [*package.records, *added]
    blanked = []
    for record in records:
        if record.kind == "structured_record" and record.table == table.id:
            cells = list(record.cells)
            cells[index] = Unknown()
            record = replace(record, cells=tuple(cells))
        blanked.append(record)
    return read_files(package_files(blanked))


def test_every_blank_list_cell_is_an_unknown_list_citing_its_cell() -> None:
    base = _cloned_rows(_base("warehouse_amr"), "Robots", 12)
    package = _mapped(base, "register_zone")
    envelopes = _of(package, "authorisation_envelope")
    assert len(envelopes) == 14
    # A blank list is the state Unknown, not () and not a finding (ADR 0012 §1).
    assert not _codes(package, "list_cell_blank")
    blank = [e for e in envelopes if isinstance(e.machines, Unknown)]
    assert len(blank) == 14  # the column is blank in every row
    for envelope in blank:
        cell = envelope.machines.provenance.evidence.locator[-1]
        assert cell.column_name == "Robots"
        assert isinstance(envelope.machines.provenance, Provenance)


def test_empty_split_parts_and_repeated_ids_are_findings() -> None:
    base = _with_cells(_base("warehouse_amr"), "Related", {2: "INC-0007;;INC-0007; ;CHG-1"})
    package = _mapped(base, "cmms_generic")
    events = [
        e
        for e in _of(package, "maintenance_event")
        if any(i.value.value == "WO-26-0312" for i in e.identifiers.value)
    ]
    (event,) = events
    assert [r.value.value for r in event.related.value] == ["CHG-1", "INC-0007"]
    empty = _codes(package, "list_part_empty")
    repeated = _codes(package, "list_id_repeated")
    assert len(empty) == 1 and len(repeated) == 1  # one finding per cell
    assert empty[0].details["count"] == 2
    assert all(f.records == (event.id,) for f in [*empty, *repeated])
    assert repeated[0].details["field"] == "/related"


# --- Large tables: column lookups never scan the rows ---------------------------------------------


class _CountingList(list[Any]):
    iterations = 0

    def __iter__(self) -> Any:
        _CountingList.iterations += 1
        return super().__iter__()


def test_mapping_a_json_table_never_rescans_its_rows() -> None:
    base = _base("warehouse_amr")
    tables, _ = mapper_module.tables_of(base.records)
    (jira,) = [t for t in tables if t.header is None]
    assert not jira.has("/fields/no_such_field")
    jira.pointers = _CountingList(jira.pointers)
    _CountingList.iterations = 0
    mapper = mapper_module._Mapper(preset("jira_json"), base.id, tables)
    mapper.run()
    assert _CountingList.iterations == 0


def test_a_large_json_table_maps_in_linear_time() -> None:
    base = _base("warehouse_amr")
    transforms = {t.id: t for t in _of(base, "transform_record")}
    jira = [
        r
        for r in _of(base, "structured_record")
        if isinstance(r.cells[0].provenance, Provenance)
        and r.cells[0].provenance.evidence.locator[-1].kind == "json_pointer"
    ]
    template = min(jira, key=lambda r: r.row)
    transform = transforms[template.provenance.transform]
    source = template.provenance.evidence.source
    rows = []
    for n in range(20_000):
        evidence = EvidenceRef(source, (ByteRange(n * 10, 10),))
        cells = tuple(
            replace(
                cell,
                provenance=replace(
                    cell.provenance,
                    evidence=EvidenceRef(
                        source, (evidence.locator[0], cell.provenance.evidence.locator[-1])
                    ),
                ),
            )
            for cell in template.cells
            if not cell.provenance.evidence.locator[-1].pointer.startswith("/fields/environment")
        )
        rows.append(
            replace(
                template,
                id=evidence_record_id("structured_record", evidence, transform),
                provenance=replace(template.provenance, evidence=evidence),
                row=n,
                cells=cells,
            )
        )
    keep = [r for r in base.records if r.kind != "structured_record" or r not in jira]
    large = IngestPackage(
        id=base.id,
        manifest=base.manifest,
        receipt_document=base.receipt_document,
        records=(*keep, *rows),
        series={},
        blobs={},
    )
    tables, _ = mapper_module.tables_of(large.records)
    (table,) = [t for t in tables if t.header is None]
    table.pointers = _CountingList(table.pointers)
    _CountingList.iterations = 0
    records = mapper_module._Mapper(preset("jira_json"), large.id, tables).run()
    assert sum(r.kind == "incident_record" for r in records) == 20_000
    assert _CountingList.iterations == 0


# --- The output never lands inside the base -------------------------------------------------------


def test_map_package_refuses_an_output_inside_the_base(tmp_path: Path) -> None:
    base = tmp_path / "base"
    shutil.copytree(PACKAGES / "inspection_quadruped", base)
    with pytest.raises(PackageError, match="inside the base package"):
        map_package(base, [preset("cmms_maximo")], base / "mapped")
    with pytest.raises(PackageError, match="inside the base package"):
        map_package(base, [preset("cmms_maximo")], base)
    assert not (base / "mapped").exists()
    read_package(base)
