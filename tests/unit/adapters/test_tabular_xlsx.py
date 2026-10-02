"""The tabular adapter on XLSX: probing, cells as declared, citations, damage, limits, hostility.

The oracles are independent readings: the zip and the cell's XML read back with ``zipfile`` and
``xml.etree`` from the byte ranges a citation names, the generator's own expected values (checked
against openpyxl by ``make_xlsx_fixtures.py --check``), and the same workbook cut into other blocks.
"""

import io
import random
import struct
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from xml.etree import ElementTree

import pytest

from neptune.adapters.contract import SIGNATURE, VERIFIED, ProbeHints
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.tabular import TabularAdapter, _xlsx
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.knowledge import (
    AssertionKind,
    Knowledge,
    Known,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import AdapterLocator, ByteRange
from neptune.model.scalars import NonFinite
from neptune.model.world import StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "tabular"
HEADED: Final = {"csv_header": "first_row"}


@pytest.fixture(scope="module")
def gen(xlsx_fixtures: ModuleType) -> ModuleType:
    return xlsx_fixtures


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes, **config: Any) -> SourceOutput:
    return ingest_source(TabularAdapter(), BytesReader(data), config)


def tables(output: SourceOutput) -> list[StructuredTable]:
    return [r for r in output.records() if isinstance(r, StructuredTable)]


def named(output: SourceOutput, name: str) -> StructuredTable:
    (found,) = [t for t in tables(output) if isinstance(t.name, Known) and t.name.value == name]
    return found


def workbook_table(output: SourceOutput) -> StructuredTable:
    (found,) = [t for t in tables(output) if _step(t.provenance.evidence.locator[-1]) == "workbook"]
    return found


def _step(step: object) -> str:
    assert isinstance(step, AdapterLocator)
    return step.kind.removeprefix("tabular:xlsx_")


def rows_of(output: SourceOutput, table: StructuredTable) -> list[StructuredRecord]:
    found = [r for r in output.records() if isinstance(r, StructuredRecord) and r.table == table.id]
    return sorted(found, key=lambda record: record.row)


def values(record: StructuredRecord) -> list[object]:
    """Each cell as its value, ``None`` for a cell that holds none."""
    return [cell.value if isinstance(cell, Known) else None for cell in record.cells]


def codes(output: SourceOutput) -> list[str]:
    return sorted(finding.code for finding in output.findings())


def probe(data: bytes, name: str = "x") -> float:
    return TabularAdapter().probe(data[:65536], ProbeHints(name, len(data))).confidence


def as_bytes(output: SourceOutput) -> bytes:
    return b"".join(canonical_json.dumps(r.to_json()) + b"\n" for r in output.package_records())


def state(cell: Knowledge[Any]) -> str:
    return type(cell).__name__


def cell_step(table_cell: Knowledge[Any]) -> AdapterLocator:
    assert not isinstance(table_cell, NotApplicable)
    step = table_cell.provenance.evidence.locator[-1]  # type: ignore[union-attr]
    assert isinstance(step, AdapterLocator)
    return step


def fields(step: AdapterLocator) -> dict[str, object]:
    return dict(step.fields)


def build(gen: ModuleType, sheets: list[tuple[str, bytes]], **kwargs: Any) -> bytes:
    return gen.zipped(gen.package(sheets, **kwargs))  # type: ignore[no-any-return]


def inflate(data: bytes, span: ByteRange, name: str | None = None) -> tuple[str, bytes]:
    """The part stored in ``span`` of the zip ``data`` and its inflated bytes."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        (info,) = [i for i in archive.infolist() if i.header_offset == span.offset]
        assert name is None or info.filename == name
        return info.filename, archive.read(info)


# --- Probe -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "workorders_amr_fleet.xlsx",
        "changelog_manipulator_cell.xlsx",
        "epoch1904_quadruped.xlsx",
        "formulas_humanoid_energy.xlsx",
        "macro_external_links.xlsm",
    ],
)
def test_a_whole_workbook_in_the_head_is_verified_by_its_parts_not_its_name(name: str) -> None:
    data = fixture(name)
    assert probe(data, "renamed") == VERIFIED
    assert probe(data, name) == VERIFIED


def test_a_larger_workbook_is_claimed_by_the_parts_the_head_shows() -> None:
    data = fixture("bomb_part_ratio.xlsx")
    assert len(data) > 65536
    assert probe(data, "no_name") == SIGNATURE
    assert probe(fixture("truncated_workorders.xlsx"), "no_name") == SIGNATURE


def test_a_zip_that_only_looks_like_ooxml_is_never_claimed_by_its_name(gen: ModuleType) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<doc/>")
    small = out.getvalue()  # a renamed word-processor file: its whole directory is in the head
    assert probe(small, "report.xlsx") == 0.0
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<doc/>" + "x" * 70000)
    assert probe(out.getvalue(), "report.xlsx") == 0.0  # larger than the head: still no name
    assert probe(out.getvalue(), "report.docx") == 0.0


def test_a_plain_zip_with_an_xl_folder_does_not_score(gen: ModuleType) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("xl/notes.txt", "hello")
        archive.writestr("data.csv", "a,b")
    assert probe(out.getvalue(), "xl.zip") == 0.0
    # a directory with a workbook part but no content types is no workbook either
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    assert probe(out.getvalue(), "x.xlsx") == 0.0


def test_other_zips_and_an_empty_zip_are_declined() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("a/b.txt", "hello")
    assert probe(out.getvalue(), "notes.xlsx") == 0.0
    assert probe(b"PK\x05\x06" + bytes(18), "x.xlsx") == 0.0
    assert probe(b"PK\x03\x04", "x.xlsx") == 0.0


# --- Cells as declared -------------------------------------------------------------------------


def test_a_work_order_export_is_one_table_per_sheet_with_cells_as_declared() -> None:
    output = run(fixture("workorders_amr_fleet.xlsx"), **HEADED)
    orders, assets = named(output, "Work Orders"), named(output, "Assets")
    assert isinstance(orders.header, Known)
    assert orders.header.value[:4] == ("WONUM", "ASSETNUM", "DESCRIPTION", "STATUS")
    assert isinstance(assets.header, Known) and assets.header.value == ("ASSETNUM", "MODEL", "SITE")
    first, second, third, fourth = rows_of(output, orders)
    assert [r.row for r in (first, second, third, fourth)] == [1, 2, 3, 4]  # sheet row r is r - 1
    assert values(first) == [
        "WO-1001", "AMR-017", "Replace drive wheel bearing", "APPR", "CM",
        pytest.approx(46030.416666666664), 3.5, True, "BRG-WEAR",
    ]  # fmt: skip
    # a short row keeps its length; the absent trailing cell is not invented
    assert len(first.cells) == 9 and len(second.cells) == 10
    # a rich-text string is its runs (the phonetic run is not text), an error a value as declared
    assert values(third)[2] == "Battery swap" and values(third)[6] == "#N/A"
    # a string keeps its whitespace; a number nothing holds exactly keeps its literal text
    assert values(fourth)[2] == " spare part, lead 14 d "
    assert values(fourth)[9] == "12345678901234567890123"
    assert [f.code for f in output.findings()] == ["tabular.xlsx_number_text"]


def test_blank_absent_and_empty_string_are_unknown_and_told_apart_by_their_citation() -> None:
    output = run(fixture("workorders_amr_fleet.xlsx"), **HEADED)
    (fourth,) = [r for r in rows_of(output, named(output, "Work Orders")) if r.row == 4]
    second = next(r for r in rows_of(output, named(output, "Work Orders")) if r.row == 2)
    styled_blank, empty_string = fourth.cells[7], fourth.cells[8]
    assert isinstance(styled_blank, Unknown) and isinstance(empty_string, Unknown)
    assert fields(cell_step(styled_blank))["content"] == "blank"  # <c s="1"/>: no value
    assert fields(cell_step(empty_string))["content"] == "empty_string"  # <is><t></t></is>
    absent = second.cells[8]  # I3: the sheet has no cell there
    assert isinstance(absent, Unknown) and fields(cell_step(absent))["content"] == "missing"
    assert fields(cell_step(absent))["ref"] == "I3"
    # none of the three is the text "" or any other value
    assert all(not isinstance(c, Known) for c in (styled_blank, empty_string, absent))


def test_dates_stay_serials_with_their_number_format_cited_and_the_epoch_recorded() -> None:
    output = run(fixture("workorders_amr_fleet.xlsx"), **HEADED)
    first = rows_of(output, named(output, "Work Orders"))[0]
    date = first.cells[5]
    assert isinstance(date, Known) and isinstance(date.value, float)
    step = fields(cell_step(date))
    assert step["numfmt"] == 164 and step["format"] == "yyyy\\-mm\\-dd\\ hh:mm"
    builtin = rows_of(output, named(output, "Work Orders"))[1].cells[5]
    assert isinstance(builtin, Known) and builtin.value == 46030  # an int serial stays an int
    assert fields(cell_step(builtin)) == {
        "content": "value", "numfmt": 14, "part": "xl/worksheets/sheet1.xml",
        "ref": "F3", "sheet": "Work Orders",
    }  # fmt: skip
    epoch = rows_of(output, workbook_table(output))[0]
    assert values(epoch) == ["date_epoch", None]  # the workbook states no date system


def test_a_1904_workbook_states_its_epoch_and_no_serial_is_converted(gen: ModuleType) -> None:
    output = run(fixture("epoch1904_quadruped.xlsx"), **HEADED)
    epoch = rows_of(output, workbook_table(output))[0]
    assert values(epoch) == ["date_epoch", 1904]
    value = epoch.cells[1]
    assert isinstance(value, Known) and value.provenance.assertion_kind is AssertionKind.STATED  # type: ignore[union-attr]
    # the declaration is the workbookPr tag, cited by bytes of the workbook part
    name, part = inflate(fixture("epoch1904_quadruped.xlsx"), epoch.provenance.evidence.locator[0])  # type: ignore[arg-type]
    assert name == "xl/workbook.xml"
    cited = epoch.provenance.evidence.locator[1]
    assert isinstance(cited, ByteRange)
    assert part[cited.offset : cited.offset + cited.length] == b'<workbookPr date1904="1"/>'
    rows = rows_of(output, named(output, "Inspections"))
    stamps = [r.cells[1] for r in rows]
    expected = [
        gen.serial(when, epoch1904=True)
        for when in (
            gen.datetime.datetime(2026, 3, 2, 9, 30),
            gen.datetime.datetime(2026, 3, 2, 14, 15),
            gen.datetime.datetime(2026, 3, 9, 8, 0),
        )
    ]
    assert [c.value for c in stamps if isinstance(c, Known)] == pytest.approx(expected)


def test_without_a_statement_the_epoch_is_unknown_not_the_formats_default(gen: ModuleType) -> None:
    output = run(fixture("workorders_amr_fleet.xlsx"))
    epoch = rows_of(output, workbook_table(output))[0]
    value = epoch.cells[1]
    assert isinstance(
        value, Unknown
    )  # ECMA-376's default is its specification's, not the workbook's
    # an explicit date1904="0" is a statement
    parts = gen.workorders_amr_fleet()
    parts["xl/workbook.xml"] = parts["xl/workbook.xml"].replace(
        b'<workbookPr defaultThemeVersion="124226"/>', b'<workbookPr date1904="0"/>'
    )
    stated = run(gen.zipped(parts))
    declared = rows_of(stated, workbook_table(stated))[0].cells[1]
    assert isinstance(declared, Known) and declared.value == 1900
    assert declared.provenance.assertion_kind is AssertionKind.STATED  # type: ignore[union-attr]


def test_the_workbook_table_lists_sheets_and_their_declared_state() -> None:
    output = run(fixture("workorders_amr_fleet.xlsx"))
    rows = [values(r) for r in rows_of(output, workbook_table(output))]
    assert rows == [
        ["date_epoch", None],
        ["sheet_count", 2],
        ["sheet", "Work Orders"],
        ["sheet", "Assets"],
        ["sheet_state", "hidden"],
    ]


def test_formulas_keep_their_cached_value_marked_and_their_text_in_a_table() -> None:
    output = run(fixture("formulas_humanoid_energy.xlsx"), **HEADED)
    budget = named(output, "Budget")
    rows = {r.row: r for r in rows_of(output, budget)}
    assert values(rows[1]) == ["hip", 1.5, 0.375]
    assert fields(cell_step(rows[1].cells[2]))["content"] == "formula"
    assert fields(cell_step(rows[1].cells[1]))["content"] == "value"
    assert values(rows[5]) == ["label", "hip-knee"]  # a string result
    assert values(rows[6]) == ["ratio", "#DIV/0!"]  # an error result, as declared
    no_value = rows[4].cells[2]  # a formula nobody calculated: Unknown, never evaluated
    assert isinstance(no_value, Unknown) and fields(cell_step(no_value))["content"] == "formula"
    assert [f.code for f in output.findings()] == ["tabular.xlsx_formula_no_value"]
    (formulas,) = [
        t for t in tables(output) if _step(t.provenance.evidence.locator[-1]) == "formulas"
    ]
    assert isinstance(formulas.header, Known)
    assert formulas.header.value == ("ref", "formula", "kind", "si", "range")
    by_ref = {
        r.cells[0].value: r for r in rows_of(output, formulas) if isinstance(r.cells[0], Known)
    }
    assert values(by_ref["C2"]) == ["C2", "B2/B5", "normal", None, None]
    assert values(by_ref["C3"]) == ["C3", "B3/B$5", "shared", 0, "C3:C4"]
    assert values(by_ref["C4"]) == ["C4", None, "shared", 0, None]  # a shared child has no text
    assert values(by_ref["B8"])[2:] == ["array", None, "B8:B9"]
    assert len(by_ref) == 8


def test_a_formula_texts_citation_is_its_f_element() -> None:
    data = fixture("formulas_humanoid_energy.xlsx")
    output = run(data, **HEADED)
    (formulas,) = [
        t for t in tables(output) if _step(t.provenance.evidence.locator[-1]) == "formulas"
    ]
    row = next(r for r in rows_of(output, formulas) if values(r)[0] == "B5")
    evidence = row.cells[1].provenance.evidence  # type: ignore[union-attr]
    span, inside, step = evidence.locator
    assert isinstance(span, ByteRange) and isinstance(inside, ByteRange)
    _, part = inflate(data, span)
    assert part[inside.offset : inside.offset + inside.length] == b"<f>SUM(B2:B4)</f>"
    assert fields(step) == {
        "content": "formula_text", "part": "xl/worksheets/sheet1.xml",
        "ref": "B5", "sheet": "Budget",
    }  # fmt: skip


def test_a_change_log_with_inline_strings_implied_references_and_a_gap() -> None:
    output = run(fixture("changelog_manipulator_cell.xlsx"), **HEADED)
    log = named(output, "Change Log")
    assert isinstance(log.header, Known) and log.header.value[:3] == ("DATE", "CHANGE_ID", "CELL")
    rows = {r.row: r for r in rows_of(output, log)}
    assert values(rows[1])[:3] == [46000, "CHG-0007", "cell-3"] and values(rows[1])[6] is True
    # row 3 has neither row nor cell references: its places are those after the row before it
    assert values(rows[2]) == [
        46002,
        "CHG-0008",
        "cell-3",
        "joint 4",
        "Recalibrated tool centre point",
    ]
    assert fields(cell_step(rows[2].cells[4]))["ref"] == "E3"
    # row 4 holds A and F only: B to E are places with no cell; a merge is not interpreted
    assert [state(c) for c in rows[3].cells] == ["Known"] + ["Unknown"] * 4 + ["Known"]
    assert fields(cell_step(rows[3].cells[2]))["content"] == "missing"
    assert codes(output) == []


def test_every_cell_cites_the_xml_that_declares_it() -> None:
    for name in (
        "workorders_amr_fleet.xlsx",
        "changelog_manipulator_cell.xlsx",
        "epoch1904_quadruped.xlsx",
        "formulas_humanoid_energy.xlsx",
    ):
        data = fixture(name)
        output = run(data, **HEADED)
        checked = 0
        for table in tables(output):
            if _step(table.provenance.evidence.locator[-1]) != "sheet":
                continue
            for record in rows_of(output, table):
                for column, cell in enumerate(record.cells):
                    evidence = record.cell_evidence(table, column)
                    span, inside, step = evidence.locator
                    assert isinstance(span, ByteRange) and isinstance(inside, ByteRange)
                    part_name, part = inflate(data, span)
                    xml = part[inside.offset : inside.offset + inside.length]
                    assert fields(step)["part"] == part_name  # type: ignore[arg-type]
                    assert fields(step)["sheet"] == table.name.value  # type: ignore[arg-type, union-attr]
                    ref = fields(step)["ref"]  # type: ignore[arg-type]
                    element = ElementTree.fromstring(xml)
                    if fields(step)["content"] == "missing":  # type: ignore[arg-type]
                        assert element.tag == "row"
                        assert all(c.get("r") != ref for c in element)
                    else:
                        assert element.tag == "c"
                        explicit = element.get("r")
                        assert explicit is None or explicit == ref
                        if isinstance(cell, Known) and fields(step)["content"] == "value":  # type: ignore[arg-type]
                            text = element.findtext("v")
                            if element.get("t") in (None, "n"):
                                assert text is not None and float(text) == float(cell.value)
                    checked += 1
        assert checked > 5, name


def test_blocks_do_not_change_what_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    data = fixture("workorders_amr_fleet.xlsx")
    whole = run(data, **HEADED)
    monkeypatch.setattr(_xlsx, "BLOCK_ROWS", 1)
    cut = run(data, **HEADED)
    assert len(cut.plan.chunks) > len(whole.plan.chunks)
    assert {r.id for r in cut.records()} == {r.id for r in whole.records()}
    assert [f.code for f in cut.findings()] == [f.code for f in whole.findings()]
    cells = run(data, **HEADED)
    monkeypatch.setattr(_xlsx, "BLOCK_ROWS", 4096)
    monkeypatch.setattr(_xlsx, "BLOCK_CELLS", 12)
    assert {r.id for r in run(data, **HEADED).records()} == {r.id for r in cells.records()}


def test_blocks_keep_formula_rows_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    data = fixture("formulas_humanoid_energy.xlsx")
    whole = run(data, **HEADED)
    monkeypatch.setattr(_xlsx, "BLOCK_ROWS", 2)
    cut = run(data, **HEADED)
    assert {r.id for r in cut.records()} == {r.id for r in whole.records()}


def test_the_header_is_the_first_row_only_when_declared() -> None:
    data = fixture("epoch1904_quadruped.xlsx")
    declared = run(data, csv_header="first_row")
    assert isinstance(named(declared, "Inspections").header, Known)
    assert len(rows_of(declared, named(declared, "Inspections"))) == 3
    undeclared = run(data)
    table = named(undeclared, "Inspections")
    assert isinstance(table.header, Unknown)  # nobody says; row 0 is a record
    assert [r.row for r in rows_of(undeclared, table)] == [0, 1, 2, 3]
    none = run(data, csv_header="none")
    assert isinstance(named(none, "Inspections").header, NotApplicable)
    assert len(rows_of(none, named(none, "Inspections"))) == 4


def test_the_same_workbook_and_config_give_the_same_bytes() -> None:
    for name in (
        "workorders_amr_fleet.xlsx",
        "formulas_humanoid_energy.xlsx",
        "damaged_sheet_xml.xlsx",
    ):
        assert as_bytes(run(fixture(name), **HEADED)) == as_bytes(run(fixture(name), **HEADED))


def test_the_same_parts_in_another_zip_layout_give_the_same_cells(gen: ModuleType) -> None:
    parts = gen.workorders_amr_fleet()
    deflated = gen.zipped(parts)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    stored = out.getvalue()
    assert stored != deflated
    left, right = run(deflated, **HEADED), run(stored, **HEADED)

    def cells(output: SourceOutput) -> list[list[object]]:
        found = [r for r in output.records() if isinstance(r, StructuredRecord)]
        return sorted([values(r) for r in found], key=repr)

    assert cells(left) == cells(right)


# --- Damage ------------------------------------------------------------------------------------


def test_a_truncated_workbook_is_a_finding_and_a_table_not_covered_not_a_failure() -> None:
    output = run(fixture("truncated_workorders.xlsx"), **HEADED)
    (finding,) = output.findings()
    assert finding.code == "tabular.xlsx_corrupt" and finding.details["error"] == "no_end_record"
    (table,) = tables(output)
    assert isinstance(table.name, NotCovered) and isinstance(table.header, NotCovered)
    assert rows_of(output, table) == []


def test_a_sheet_cut_inside_a_row_keeps_the_rows_before_the_cut() -> None:
    output = run(fixture("damaged_sheet_xml.xlsx"), **HEADED)
    sites = named(output, "Sites")
    assert [values(r) for r in rows_of(output, sites)] == [[1, "north"], [2, "south"]]
    (finding,) = output.findings()
    assert finding.code == "tabular.xlsx_corrupt" and finding.details["error"] == "bad_xml"
    assert finding.details["part"] == "xl/worksheets/sheet1.xml"


@pytest.mark.parametrize("keep", [0, 3, 4, 30, 100, 600, 1500])
def test_a_workbook_cut_anywhere_never_raises(keep: int) -> None:
    data = fixture("workorders_amr_fleet.xlsx")[:keep] or b"PK\x03\x04"
    output = run(data, **HEADED)
    assert output.findings() and all(f.code.startswith("tabular.") for f in output.findings())


def test_garbage_after_the_zip_magic_is_a_finding() -> None:
    output = run(b"PK\x03\x04" + bytes(range(256)) * 4, **HEADED)
    assert codes(output) == ["tabular.xlsx_corrupt"]
    (table,) = tables(output)
    assert isinstance(table.name, NotCovered)


def test_a_zip_that_is_not_a_workbook_is_a_finding(gen: ModuleType) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("word/document.xml", "<w/>")
    output = run(out.getvalue())
    (finding,) = output.findings()
    assert finding.code == "tabular.xlsx_corrupt" and finding.details["error"] == "no_workbook"


def test_a_corrupt_part_stream_is_a_finding(gen: ModuleType) -> None:
    data = bytearray(fixture("workorders_amr_fleet.xlsx"))
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        sheet = archive.getinfo("xl/worksheets/sheet1.xml")
    data[sheet.header_offset + 30 + len(sheet.filename) + 20] ^= 0xFF  # inside the deflate stream
    output = run(bytes(data), **HEADED)
    assert "tabular.xlsx_corrupt" in codes(output)


def test_a_sheet_with_a_bad_cell_reference_or_a_row_out_of_order_drops_only_that(
    gen: ModuleType,
) -> None:
    rows = [
        gen.row(1, gen.n("A1", "1"), gen.n("B1", "2")),
        gen.row(3, gen.n("A3", "3"), gen.n("A4", "4"), gen.n("B3", "5")),  # A4 is not row 3
        gen.row(2, gen.n("A2", "9")),  # out of order: after row 3
        gen.row(4, gen.n("C4", "6"), gen.n("B4", "7")),  # B4 does not follow C4
    ]
    output = run(build(gen, [("S", gen.worksheet(rows))], styles=False))
    table = named(output, "S")
    assert [(r.row, values(r)) for r in rows_of(output, table)] == [
        (0, [1, 2]),
        (2, [3, 5]),  # the dropped A4 leaves nothing; B3 is where it says
        (3, [None, None, 6]),
    ]
    assert codes(output) == ["tabular.xlsx_cell_ref", "tabular.xlsx_row_order"]


def test_unreadable_cell_values_are_unknown_with_a_finding(gen: ModuleType) -> None:
    rows = [
        gen.row(
            1,
            '<c r="A1"><v>abc</v></c>',
            '<c r="B1" t="b"><v>maybe</v></c>',
            '<c r="C1" t="s"><v>x</v></c>',
            '<c r="D1" t="s"><v>99</v></c>',
            '<c r="E1" t="weird"><v>1</v></c>',
            '<c r="F1"><v>INF</v></c>',
            '<c r="G1"><v>-INF</v></c>',
            '<c r="H1"><v>1E+3</v></c>',
            '<c r="I1"><v>0.30000000000000004</v></c>',
        )
    ]
    output = run(build(gen, [("S", gen.worksheet(rows))], styles=False))
    (record,) = rows_of(output, named(output, "S"))
    assert [state(c) for c in record.cells[:5]] == ["Unknown"] * 5
    assert [getattr(c, "value", None) for c in record.cells[5:]] == [
        NonFinite.POSITIVE_INFINITY, NonFinite.NEGATIVE_INFINITY, 1000.0, 0.30000000000000004,
    ]  # fmt: skip
    assert codes(output) == [
        "tabular.xlsx_cell_unreadable",
        "tabular.xlsx_shared_string_ref",
    ]
    (unreadable,) = [f for f in output.findings() if f.code == "tabular.xlsx_cell_unreadable"]
    assert unreadable.details["count"] == 4


# --- Limits and hostile workbooks --------------------------------------------------------------


def test_a_zip_bomb_is_refused_by_ratio_before_anything_is_inflated() -> None:
    output = run(fixture("bomb_zeros_sheet.xlsx"), **HEADED)
    (finding,) = output.findings()
    assert finding.code == "tabular.xlsx_limit"
    assert finding.details["limit"] == "xlsx_max_compression_ratio"
    (table,) = tables(output)
    assert isinstance(table.name, NotCovered)


def test_a_part_over_the_ratio_is_not_covered_though_the_zip_is_not_a_bomb() -> None:
    output = run(fixture("bomb_part_ratio.xlsx"), **HEADED)
    (finding,) = output.findings()
    assert finding.code == "tabular.xlsx_limit"
    assert finding.details["part"] == "xl/worksheets/sheet1.xml"
    assert finding.details["limit"] == "xlsx_max_compression_ratio"
    pad = named(output, "Pad")
    assert isinstance(pad.header, NotCovered) and rows_of(output, pad) == []
    # the finding cites the part's bytes in the zip
    assert isinstance(finding.subject.locator[0], ByteRange)  # type: ignore[union-attr]


def test_a_declared_size_over_the_part_cap_is_refused_and_the_boundary_reads() -> None:
    data = fixture("workorders_amr_fleet.xlsx")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        size = max(info.file_size for info in archive.infolist())
    at = run(data, xlsx_max_part_bytes=size, **HEADED)
    assert "tabular.xlsx_limit" not in codes(at)
    assert len(rows_of(at, named(at, "Work Orders"))) == 4
    over = run(data, xlsx_max_part_bytes=size - 1, **HEADED)
    (limit,) = [f for f in over.findings() if f.code == "tabular.xlsx_limit"]
    assert limit.details["limit"] == "xlsx_max_part_bytes"
    assert limit.details["part"] == "xl/sharedStrings.xml"  # the largest part
    # the strings were not read: their cells are not covered, the numbers still are
    first = rows_of(over, named(over, "Work Orders"))[0]
    assert state(first.cells[0]) == "NotCovered" and state(first.cells[6]) == "Known"


def test_a_part_that_inflates_past_its_declared_size_is_not_trusted(gen: ModuleType) -> None:
    """The directory lies low: zipfile stops at the declared size and the CRC then fails."""
    data = bytearray(gen.zipped(gen.workorders_amr_fleet()))
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        info = archive.getinfo("xl/worksheets/sheet1.xml")
    central = bytes(data).rfind(b"PK\x01\x02" + b"\x00" * 0)
    at = -1
    for found in range(len(data)):
        if data[found : found + 4] == b"PK\x01\x02":
            name_length = struct.unpack_from("<H", data, found + 28)[0]
            if bytes(data[found + 46 : found + 46 + name_length]) == info.filename.encode():
                at = found
                break
    assert at >= 0 and central >= 0
    struct.pack_into("<I", data, at + 24, 100)  # declared uncompressed size: 100 bytes
    output = run(bytes(data), **HEADED)
    assert (
        "tabular.xlsx_corrupt" in codes(output)
        or len(rows_of(output, named(output, "Work Orders"))) < 4
    )


def test_a_document_type_declaration_is_refused_and_no_entity_is_expanded() -> None:
    output = run(fixture("hostile_entities_sheet.xlsx"), **HEADED)
    (finding,) = output.findings()
    assert finding.code == "tabular.xlsx_part_refused" and finding.details["reason"] == "doctype"
    boom = named(output, "Boom")
    assert isinstance(boom.header, NotCovered) and rows_of(output, boom) == []


def test_shared_strings_over_the_limit_are_not_covered_not_missing() -> None:
    data = fixture("blowup_shared_strings.xlsx")
    whole = run(data, **HEADED)
    assert codes(whole) == []
    capped = run(data, xlsx_max_shared_strings=1000, **HEADED)
    (finding,) = capped.findings()
    assert finding.code == "tabular.xlsx_limit" and finding.details["read"] == 1000
    labels = named(capped, "Labels")
    # the header is the first row: A1 is string 0 (read), B1 string 1500 and C1 2999 are not
    assert isinstance(labels.header, Unknown)  # a header cell is not covered: no names
    rows = run(data, xlsx_max_shared_strings=1000)
    (record,) = rows_of(rows, named(rows, "Labels"))
    assert [state(c) for c in record.cells] == ["Known", "NotCovered", "NotCovered"]
    assert record.cells[0].value == "label-0000"  # type: ignore[union-attr]


def test_shared_string_bytes_over_the_limit_are_not_covered() -> None:
    data = fixture("blowup_shared_strings.xlsx")
    capped = run(data, xlsx_max_shared_string_bytes=1000)
    (finding,) = capped.findings()
    assert finding.code == "tabular.xlsx_limit"
    (record,) = rows_of(capped, named(capped, "Labels"))
    assert state(record.cells[2]) == "NotCovered"


def test_cells_over_the_sheet_limit_stop_the_sheet_at_a_row(gen: ModuleType) -> None:
    rows = [gen.row(r, *(gen.n(f"{c}{r}", r) for c in "ABC")) for r in range(1, 11)]
    data = build(gen, [("S", gen.worksheet(rows))], styles=False)
    at = run(data, xlsx_max_cells=30)
    assert len(rows_of(at, named(at, "S"))) == 10 and codes(at) == []
    over = run(data, xlsx_max_cells=29)
    assert len(rows_of(over, named(over, "S"))) == 9
    (finding,) = over.findings()
    assert finding.code == "tabular.xlsx_limit" and finding.details["limit"] == "xlsx_max_cells"


def test_sheets_over_the_limit_are_named_and_not_read(gen: ModuleType) -> None:
    sheets = [(f"S{k}", gen.worksheet([gen.row(1, gen.n("A1", k))])) for k in range(5)]
    data = build(gen, sheets, styles=False)
    output = run(data, xlsx_max_sheets=3)
    assert sorted(str(t.name.value) for t in tables(output) if isinstance(t.name, Known)) == [
        "S0", "S1", "S2",
    ]  # fmt: skip
    (finding,) = output.findings()
    assert finding.details["limit"] == "xlsx_max_sheets" and finding.details["sheets"] == 5
    count = [values(r) for r in rows_of(output, workbook_table(output))][1]
    assert count == ["sheet_count", 5]  # the table says how many there were


def test_parts_over_the_limit_refuse_the_workbook() -> None:
    output = run(fixture("workorders_amr_fleet.xlsx"), xlsx_max_parts=3)
    (finding,) = output.findings()
    assert finding.details["limit"] == "xlsx_max_parts"
    (table,) = tables(output)
    assert isinstance(table.name, NotCovered)


def test_styles_over_the_limit_drop_the_formats_not_the_values() -> None:
    data = fixture("workorders_amr_fleet.xlsx")
    output = run(data, xlsx_max_styles=2, **HEADED)
    assert [f.details["limit"] for f in output.findings() if f.code == "tabular.xlsx_limit"] == [
        "xlsx_max_styles"
    ]
    first = rows_of(output, named(output, "Work Orders"))[0]
    assert isinstance(first.cells[5], Known)
    assert "numfmt" not in fields(cell_step(first.cells[5]))


def test_total_declared_bytes_over_the_limit_refuse_the_workbook() -> None:
    output = run(fixture("workorders_amr_fleet.xlsx"), xlsx_max_total_bytes=1000)
    (finding,) = output.findings()
    assert finding.details["limit"] == "xlsx_max_total_bytes"


def test_a_row_over_max_row_bytes_has_no_record(gen: ModuleType) -> None:
    rows = [
        gen.row(1, gen.n("A1", "1")),
        gen.row(2, *(gen.i(f"{c}2", "x" * 50) for c in "ABCDEF")),
        gen.row(3, gen.n("A3", "3")),
    ]
    output = run(build(gen, [("S", gen.worksheet(rows))], styles=False), max_row_bytes=200)
    assert [r.row for r in rows_of(output, named(output, "S"))] == [0, 2]
    (finding,) = output.findings()
    assert finding.code == "tabular.row_too_large" and finding.details["row"] == 2


def test_a_row_with_a_cell_past_max_columns_is_not_decoded(gen: ModuleType) -> None:
    rows = [gen.row(1, gen.n("A1", "1"), gen.n("E1", "2")), gen.row(2, gen.n("B2", "3"))]
    output = run(build(gen, [("S", gen.worksheet(rows))], styles=False), max_columns=3)
    assert [r.row for r in rows_of(output, named(output, "S"))] == [1]
    assert codes(output) == ["tabular.too_many_columns"]


def test_rows_over_max_rows_are_not_read(gen: ModuleType) -> None:
    rows = [gen.row(r, gen.n(f"A{r}", r)) for r in range(1, 7)]
    output = run(build(gen, [("S", gen.worksheet(rows))], styles=False), max_rows=4)
    assert len(rows_of(output, named(output, "S"))) == 4
    assert codes(output) == ["tabular.row_limit"]


def test_xml_nested_past_the_depth_bound_is_a_limit_not_a_crash(gen: ModuleType) -> None:
    deep = b"<x>" * 5000 + b"</x>" * 5000
    output = run(build(gen, [("S", gen.worksheet([], after=deep.decode()))], styles=False))
    assert codes(output) == ["tabular.xlsx_limit"]
    assert isinstance(named(output, "S").header, Unknown | NotCovered)


def test_a_sheet_part_in_utf16_is_refused(gen: ModuleType) -> None:
    xml = gen.worksheet([gen.row(1, gen.n("A1", "1"))]).decode().encode("utf-16")
    output = run(build(gen, [("S", xml)], styles=False))
    assert codes(output) == ["tabular.xlsx_part_refused"]


def test_an_encrypted_or_oddly_compressed_part_is_refused(gen: ModuleType) -> None:
    parts = gen.workorders_amr_fleet()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in parts.items():
            method = (
                zipfile.ZIP_BZIP2 if name == "xl/worksheets/sheet1.xml" else zipfile.ZIP_DEFLATED
            )
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = method
            archive.writestr(info, data)
    output = run(out.getvalue(), **HEADED)
    (finding,) = output.findings()
    assert (
        finding.code == "tabular.xlsx_part_refused" and finding.details["reason"] == "compression"
    )
    raw = bytearray(fixture("workorders_amr_fleet.xlsx"))
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as archive:
        info = archive.getinfo("xl/worksheets/sheet1.xml")
    for marker, offset in ((b"PK\x01\x02", 8),):
        for at in range(len(raw)):
            if raw[at : at + 4] == marker:
                length = struct.unpack_from("<H", raw, at + 28)[0]
                if bytes(raw[at + 46 : at + 46 + length]) == info.filename.encode():
                    raw[at + offset] |= 0x1  # the encrypted flag
    encrypted = run(bytes(raw), **HEADED)
    assert [f.details["reason"] for f in encrypted.findings()] == ["encrypted"]


def test_a_relationship_that_leaves_the_package_is_never_opened(gen: ModuleType) -> None:
    parts = gen.workorders_amr_fleet()
    rels = parts["xl/_rels/workbook.xml.rels"].replace(
        b'Target="worksheets/sheet1.xml"', b'Target="../../../etc/passwd"'
    )
    parts["xl/_rels/workbook.xml.rels"] = rels
    output = run(gen.zipped(parts), **HEADED)
    (finding,) = [f for f in output.findings() if f.code == "tabular.xlsx_sheet_unsupported"]
    assert finding.details["reason"] == "part_missing"
    assert isinstance(named(output, "Work Orders").header, NotCovered)
    assert len(rows_of(output, named(output, "Assets"))) == 2  # the other sheet is read


def test_macros_and_external_links_are_named_and_never_followed_or_run() -> None:
    output = run(fixture("macro_external_links.xlsm"), **HEADED)
    assert codes(output) == ["tabular.xlsx_external_link", "tabular.xlsx_macros"]
    (links,) = [f for f in output.findings() if f.code == "tabular.xlsx_external_link"]
    assert links.details["targets"] == ["https://example.invalid/never-followed"]
    (macros,) = [f for f in output.findings() if f.code == "tabular.xlsx_macros"]
    assert macros.details["part"] == "xl/vbaProject.bin"
    # the macro part's bytes are cited, never read: the finding's range is its stored bytes
    span = macros.subject.locator[0]  # type: ignore[union-attr]
    assert isinstance(span, ByteRange)
    tuning = named(output, "Tuning")
    assert [values(r) for r in rows_of(output, tuning)] == [["gain", 2.5]]


def test_a_chart_sheet_is_a_table_with_no_rows_and_an_info_finding(gen: ModuleType) -> None:
    parts = gen.workorders_amr_fleet()
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b'/worksheet" Target="worksheets/sheet2.xml"',
        b'/chartsheet" Target="worksheets/sheet2.xml"',
    )
    output = run(gen.zipped(parts), **HEADED)
    (finding,) = [f for f in output.findings() if f.code == "tabular.xlsx_sheet_unsupported"]
    assert finding.details["reason"] == "type_chartsheet"
    assert isinstance(named(output, "Assets").header, NotCovered)


def test_a_billion_text_sheet_is_not_held_in_memory(gen: ModuleType) -> None:
    """One cell may not hold more than a row may: a huge string is a row over the bound."""
    text = random.Random(3).randbytes(150_000).hex()  # varied, so the ratio is not what stops it
    rows = [gen.row(1, gen.i("A1", text))]
    output = run(build(gen, [("S", gen.worksheet(rows))], styles=False), max_row_bytes=100_000)
    assert codes(output) == ["tabular.row_too_large"]


# --- Review: boundaries a hostile or odd workbook reaches --------------------------------------


def test_digit_strings_too_long_for_a_number_are_not_numbers_not_crashes(gen: ModuleType) -> None:
    huge = "9" * 5000
    parts = gen.workorders_amr_fleet()
    parts["xl/styles.xml"] = parts["xl/styles.xml"].replace(
        b'numFmtId="14"', f'numFmtId="{huge}"'.encode()
    )
    sheet = parts["xl/worksheets/sheet1.xml"].replace(b' s="2"', f' s="{huge}"'.encode(), 1)
    parts["xl/worksheets/sheet1.xml"] = sheet
    output = run(gen.zipped(parts), **HEADED)
    first = rows_of(output, named(output, "Work Orders"))[0]
    assert isinstance(first.cells[5], Known)  # the value is read; its format is just not named
    shared = gen.formulas_humanoid_energy()
    shared["xl/worksheets/sheet1.xml"] = shared["xl/worksheets/sheet1.xml"].replace(
        b'si="0"', f'si="{huge}"'.encode()
    )
    odd = run(gen.zipped(shared), **HEADED)
    assert all(f.severity.value != "failed" for f in odd.findings())


def test_a_cell_far_from_the_others_is_not_covered_not_a_run_of_blanks(gen: ModuleType) -> None:
    rows = [gen.row(r, gen.n(f"XFD{r}", r)) for r in range(1, 41)]
    data = build(gen, [("S", gen.worksheet(rows))], styles=False)
    output = run(data)
    records = rows_of(output, named(output, "S"))
    assert len(records) == 40
    assert all([state(c) for c in r.cells] == ["NotCovered"] for r in records)  # one cell each
    (finding,) = output.findings()
    assert finding.code == "tabular.xlsx_limit" and finding.details["limit"] == "xlsx_max_gap_ratio"
    assert finding.details["count"] == 40
    assert fields(cell_step(records[0].cells[0]))["content"] == "not_covered"
    assert fields(cell_step(records[0].cells[0]))["ref"] == "A1"  # the place after the last kept


def test_the_gap_budget_is_per_row_and_its_boundary_reads(gen: ModuleType) -> None:
    rows = [
        gen.row(1, gen.n("D1", 1), gen.n("I1", 2), gen.n("N1", 3)),  # blanks 3, 7, 11
        gen.row(2, gen.n("E2", 1)),  # blanks 4
    ]
    data = build(gen, [("S", gen.worksheet(rows))], styles=False)
    at = run(data, xlsx_max_gap_ratio=4)
    first, second = rows_of(at, named(at, "S"))
    # 3 <= 4*1; 7 <= 4*2; 11 <= 4*3: all kept, and E2 (4 <= 4*1) too
    assert [state(c) for c in first.cells].count("Known") == 3 and len(first.cells) == 14
    assert len(second.cells) == 5 and codes(at) == []
    over = run(data, xlsx_max_gap_ratio=3)
    first, second = rows_of(over, named(over, "S"))
    # D1: 3 <= 3 kept; I1: 7 > 3*2 cut, and so is N1; E2: 4 > 3 cut
    assert [state(c) for c in first.cells] == ["Unknown"] * 3 + ["Known", "NotCovered"]
    assert [state(c) for c in second.cells] == ["NotCovered"]
    assert codes(over) == ["tabular.xlsx_limit"]


def sparse_row(gen: ModuleType, number: int, count: int, ratio: int, *, compound: bool) -> str:
    """``count`` cells each placed as far right as a budget allows: a gap of ``ratio * (k + 1)``
    after k cells (what a budget on each gap alone permits, so the row grows quadratically), or
    the farthest the whole row's budget permits."""
    cells, column = [], -1
    for k in range(count):
        column = column + 1 + ratio * (k + 1) if compound else k + ratio * (k + 1)
        cells.append(gen.n(f"{_xlsx._letters(column)}{number}", 1))
    return str(gen.row(number, *cells))


def test_a_rows_blanks_are_bounded_by_its_real_cells_not_by_each_gap(gen: ModuleType) -> None:
    ratio = 4
    rows = [sparse_row(gen, r, 12, ratio, compound=True) for r in range(1, 4)]
    data = build(gen, [("S", gen.worksheet(rows))], styles=False)
    output = run(data, xlsx_max_gap_ratio=ratio)
    records = rows_of(output, named(output, "S"))
    assert len(records) == 3
    for record in records:
        made = [c for c in record.cells if not isinstance(c, NotCovered)]
        real = sum(isinstance(c, Known) for c in record.cells)
        assert real < 12  # the row was cut: each gap fits its own budget, the row's does not
        assert len(made) <= (ratio + 1) * real + ratio
        assert isinstance(record.cells[-1], NotCovered)
    assert codes(output) == ["tabular.xlsx_limit"]
    # the farthest placement the row's own budget permits is all kept: ratio + 1 cells per real
    exact = [sparse_row(gen, 1, 12, ratio, compound=False)]
    kept = run(build(gen, [("S", gen.worksheet(exact))], styles=False), xlsx_max_gap_ratio=ratio)
    (record,) = rows_of(kept, named(kept, "S"))
    assert sum(isinstance(c, Known) for c in record.cells) == 12
    assert len(record.cells) == 11 + ratio * 12 + 1  # 12 cells, the last at column 11 + 4 * 12
    assert codes(kept) == []


def test_the_default_budget_bounds_a_cumulative_sparse_row_at_small_scale(
    gen: ModuleType,
) -> None:
    rows = [sparse_row(gen, r, 6, 64, compound=True) for r in range(1, 6)]
    data = build(gen, [("S", gen.worksheet(rows))], styles=False)
    output = run(data)
    for record in rows_of(output, named(output, "S")):
        real = sum(isinstance(c, Known) for c in record.cells)
        made = [c for c in record.cells if not isinstance(c, NotCovered)]
        assert len(made) <= 65 * real + 64
    assert sum(len(r.cells) for r in rows_of(output, named(output, "S"))) < 5 * (65 * 3 + 65)


def test_gap_cuts_do_not_depend_on_the_blocks(
    gen: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        gen.row(r, gen.n(f"A{r}", r), gen.n(f"Z{r}", r), gen.n(f"XFD{r}", r)) for r in range(1, 9)
    ]
    data = build(gen, [("S", gen.worksheet(rows))], styles=False)
    whole = run(data, xlsx_max_gap_ratio=24)
    monkeypatch.setattr(_xlsx, "BLOCK_CELLS", 30)
    cut = run(data, xlsx_max_gap_ratio=24)
    assert len(cut.plan.chunks) > len(whole.plan.chunks)
    assert {r.id for r in whole.records()} == {r.id for r in cut.records()}
    records = rows_of(whole, named(whole, "S"))
    assert [state(c) for c in records[0].cells].count("Known") == 2  # A and Z; XFD is cut
    assert state(records[0].cells[-1]) == "NotCovered" and len(records[0].cells) == 27


def test_a_sparse_workbook_is_small_and_quick_to_read(gen: ModuleType) -> None:
    rows = [gen.row(r, gen.n(f"XFD{r}", r)) for r in range(1, 3001)]
    data = build(gen, [("S", gen.worksheet(rows))], styles=False)
    output = run(data)  # the default budget: nothing like 3,000 x 16,384 cells
    assert sum(len(r.cells) for r in rows_of(output, named(output, "S"))) == 3000


def test_xml_nested_in_the_workbook_or_its_relationships_is_a_limit(gen: ModuleType) -> None:
    noise = random.Random(5)  # varied, so the ratio is not what stops it
    deep = "".join(f"<a>{noise.randbytes(4).hex()}" for _ in range(100_000))
    for part, marker in (
        ("xl/workbook.xml", b"<sheets>"),
        ("xl/_rels/workbook.xml.rels", b"<Relationship "),
        ("_rels/.rels", b"<Relationship "),
    ):
        parts = gen.workorders_amr_fleet()
        parts[part] = parts[part].replace(marker, deep.encode() + marker, 1)
        output = run(gen.zipped(parts), **HEADED)
        limits = [f for f in output.findings() if f.details.get("limit") == "depth"]
        assert limits and limits[0].details["part"] == part, part


def test_the_header_formulas_reference_follows_an_empty_row_with_no_row_number(
    gen: ModuleType,
) -> None:
    rows = [
        "<row/>",
        '<row><c t="str"><f>"a"</f><v>a</v></c><c t="inlineStr"><is><t>b</t></is></c></row>',
        "<row><c><v>1</v></c><c><v>2</v></c></row>",
    ]
    output = run(build(gen, [("S", gen.worksheet(rows))], styles=False), **HEADED)
    (formulas,) = [
        t for t in tables(output) if _step(t.provenance.evidence.locator[-1]) == "formulas"
    ]
    (record,) = rows_of(output, formulas)
    assert values(record)[:2] == ["A2", '"a"']
    assert [r.row for r in rows_of(output, named(output, "S"))] == [2]


def test_a_minus_zero_stays_as_declared(gen: ModuleType) -> None:
    rows = [
        gen.row(1, '<c r="A1"><v>-0</v></c>', '<c r="B1"><v>-0.0</v></c>', '<c r="C1"><v>0</v></c>')
    ]
    output = run(build(gen, [("S", gen.worksheet(rows))], styles=False))
    (record,) = rows_of(output, named(output, "S"))
    assert values(record) == ["-0", -0.0, 0]
    assert str(values(record)[1]) == "-0.0" and codes(output) == ["tabular.xlsx_number_text"]


def test_a_sheet_tag_too_long_to_cite_is_a_limit_not_a_zero_length_citation(
    gen: ModuleType,
) -> None:
    parts = gen.workorders_amr_fleet()
    parts["xl/workbook.xml"] = parts["xl/workbook.xml"].replace(
        b'name="Work Orders"', b'name="' + random.Random(6).randbytes(100_000).hex().encode() + b'"'
    )
    output = run(gen.zipped(parts), **HEADED)
    assert [f.details["limit"] for f in output.findings() if f.code == "tabular.xlsx_limit"] == [
        "tag"
    ]
    for table in tables(output):
        assert all(
            not isinstance(step, ByteRange) or step.length > 0
            for step in table.provenance.evidence.locator
        )


def test_a_header_over_max_row_bytes_is_not_replaced_by_the_next_row(gen: ModuleType) -> None:
    rows = [
        gen.row(1, *(gen.i(f"{c}1", "h" * 50) for c in "ABCDEF")),
        gen.row(2, gen.n("A2", 1)),
        gen.row(3, gen.n("A3", 2)),
    ]
    data = build(gen, [("S", gen.worksheet(rows))], styles=False)
    output = run(data, max_row_bytes=200, **HEADED)
    table = named(output, "S")
    assert isinstance(table.header, Unknown)  # the header could not be read; no row stands in
    assert [values(r) for r in rows_of(output, table)] == [[1], [2]]
    assert codes(output) == ["tabular.row_too_large"]


def test_a_formula_in_the_header_row_is_a_row_of_the_formulas_table(gen: ModuleType) -> None:
    rows = [
        gen.row(1, '<c r="A1" t="str"><f>"id"&amp;"x"</f><v>idx</v></c>', gen.i("B1", "v")),
        gen.row(2, gen.n("A2", 1), '<c r="B2"><f>A2*2</f><v>2</v></c>'),
    ]
    output = run(build(gen, [("S", gen.worksheet(rows))], styles=False), **HEADED)
    table = named(output, "S")
    assert isinstance(table.header, Known) and table.header.value == ("idx", "v")
    (formulas,) = [
        t for t in tables(output) if _step(t.provenance.evidence.locator[-1]) == "formulas"
    ]
    listed = rows_of(output, formulas)
    assert [(r.row, values(r)[:2]) for r in listed] == [
        (0, ["A1", '"id"&"x"']),
        (1, ["B2", "A2*2"]),
    ]


def test_formula_ordinals_do_not_depend_on_the_blocks_or_on_dropped_cells(
    gen: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        gen.row(1, gen.i("A1", "h")),
        gen.row(2, '<c r="A2"><f>1</f><v>1</v></c>'),
        gen.row(
            2, '<c r="A2"><f>9</f><v>9</v></c>'
        ),  # a repeated row: dropped, its formula counted
        gen.row(3, '<c r="A3"><f>3</f><v>3</v></c>', '<c r="A9"><f>8</f><v>8</v></c>'),
        gen.row(4, '<c r="A4"><f>4</f><v>4</v></c>'),
    ]
    data = build(gen, [("S", gen.worksheet(rows))], styles=False)
    whole = run(data, **HEADED)
    monkeypatch.setattr(_xlsx, "BLOCK_ROWS", 1)
    cut = run(data, **HEADED)
    assert {r.id for r in whole.records()} == {r.id for r in cut.records()}
    pairs = {(r.id, r.row) for r in whole.records() if isinstance(r, StructuredRecord)}
    assert pairs == {(r.id, r.row) for r in cut.records() if isinstance(r, StructuredRecord)}


def test_a_second_formula_element_in_one_cell_is_not_a_second_formula(
    gen: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        gen.row(1, gen.i("A1", "h")),
        gen.row(2, '<c r="A2"><f>1</f><f>2</f><v>1</v></c>', gen.n("B2", 2)),
        gen.row(3, '<c r="A3"><f>3</f><v>3</v></c>'),
    ]
    data = build(gen, [("S", gen.worksheet(rows))], styles=False)
    whole = run(data, **HEADED)
    monkeypatch.setattr(_xlsx, "BLOCK_ROWS", 1)
    cut = run(data, **HEADED)
    assert {r.id for r in whole.records()} == {r.id for r in cut.records()}
    (formulas,) = [
        t for t in tables(whole) if _step(t.provenance.evidence.locator[-1]) == "formulas"
    ]
    assert [r.row for r in rows_of(whole, formulas)] == [0, 1]


def test_a_binary_workbook_is_not_claimed_and_templates_are_named_workbooks(
    gen: ModuleType,
) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.bin", b"\x00" * 10)
    assert probe(out.getvalue(), "book.xlsb") == 0.0
    templates = TabularAdapter().descriptor.formats[-1].extensions
    assert ".xltx" in templates and ".xltm" in templates


def test_two_sheets_over_one_part_are_not_two_tables_of_the_same_rows(gen: ModuleType) -> None:
    parts = gen.workorders_amr_fleet()
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b"worksheets/sheet2.xml", b"worksheets/sheet1.xml"
    )
    output = run(gen.zipped(parts), **HEADED)
    assert len(rows_of(output, named(output, "Work Orders"))) == 4
    assert rows_of(output, named(output, "Assets")) == []
    (finding,) = [f for f in output.findings() if f.code == "tabular.xlsx_sheet_unsupported"]
    assert finding.details["reason"] == "part_shared"
    ids = [r.id for r in output.records()]
    assert len(ids) == len(set(ids))
