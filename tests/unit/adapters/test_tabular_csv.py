"""The tabular adapter on CSV and TSV: probing, cells as text, citations, damage and limits.

The oracles are independent readings: the standard library's ``csv`` module for well-formed files,
and the same file cut into other blocks or other pieces for the streaming laws.
"""

import csv
import io
from collections.abc import Iterable
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from neptune.adapters.contract import NAME_ONLY, STRUCTURE, ProbeHints
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.tabular import TabularAdapter, _csv
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.finding import IngestFinding
from neptune.model.knowledge import Known, NotApplicable, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef, Row, RowCell
from neptune.model.world import StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "tabular"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes, **config: Any) -> SourceOutput:
    return ingest_source(TabularAdapter(), BytesReader(data), config)


def rows(output: SourceOutput) -> list[StructuredRecord]:
    found = [r for r in output.records() if isinstance(r, StructuredRecord)]
    return sorted(found, key=lambda record: record.row)


def table(output: SourceOutput) -> StructuredTable:
    (found,) = [r for r in output.records() if isinstance(r, StructuredTable)]
    return found


def header_of(output: SourceOutput) -> tuple[str, ...]:
    header = table(output).header
    assert isinstance(header, Known)
    return header.value


def texts(record: StructuredRecord) -> list[object]:
    """Each cell as its value, ``None`` for a cell that holds none."""
    return [cell.value if isinstance(cell, Known) else None for cell in record.cells]


def codes(output: SourceOutput) -> list[str]:
    return sorted(finding.code for finding in output.findings())


def probe(data: bytes, name: str = "x") -> float:
    return TabularAdapter().probe(data, ProbeHints(name, len(data))).confidence


def as_bytes(output: SourceOutput) -> bytes:
    return b"".join(canonical_json.dumps(r.to_json()) + b"\n" for r in output.package_records())


# --- Probe -------------------------------------------------------------------------------------


def test_probing_reads_content_not_names() -> None:
    assert probe(fixture("telemetry_amr.csv"), "no_extension") == STRUCTURE
    assert probe(fixture("inspection_quadruped.tsv"), "register.dat") == STRUCTURE
    assert probe(b"a;b;c\n1;2;3\n", "x") == STRUCTURE
    assert probe(b"\xef\xbb\xbfa,b,c\n1,2,3\n", "x") == STRUCTURE


def test_a_yaml_or_toml_name_is_never_a_sniffed_table() -> None:
    flow = b'a: &a ["lol","lol","lol"]\nb: &b [*a,*a,*a]\nc: &c [*b,*b,*b]\n'
    assert probe(flow, "bomb") == STRUCTURE  # unnamed, the commas agree
    for name in ("bomb.yaml", "bomb.YML", "tool.toml"):
        result = TabularAdapter().probe(flow, ProbeHints(name, len(flow)))
        assert result.confidence == 0.0
        assert [r.code for r in result.reasons] == ["tabular.not_delimited_name"]


def test_two_fields_a_line_need_the_name_to_be_a_table() -> None:
    data = b"a,b\n1,2\n3,4\n"
    assert probe(data, "pairs") == NAME_ONLY
    assert probe(data, "pairs.csv") == STRUCTURE
    assert probe(b"a\tb\n1\t2\n", "pairs.TSV") == STRUCTURE


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"just one line, with commas\n",
        b"prose, with a comma\nbut the next line has none\n",
        b"a,b,c\n1,2\n",
        b"one\ntwo\nthree\n",
        b"a,b\x00c\n1,2,3\n",
        b"\x89PNG\r\n\x1a\n,,,\n,,,\n",
    ],
)
def test_text_that_is_not_a_table_is_declined(data: bytes) -> None:
    assert probe(data, "notes.csv") == 0.0


def test_a_pipe_is_never_sniffed() -> None:
    assert probe(b"a|b|c\n1|2|3\n", "x.csv") == 0.0
    output = run(b"a|b|c\n1|2|3\n", csv_delimiter="|", csv_header="first_row")
    assert texts(rows(output)[0]) == ["1", "2", "3"]


def test_the_sniffed_delimiter_is_the_one_with_the_most_agreed_fields() -> None:
    assert _csv.sniff(b"a,b;c,d\n1,2;3,4\n", complete=True) == _csv.Dialect(",", 3, 2)
    assert _csv.sniff(b"a\tb\n1\t2\n", complete=True) == _csv.Dialect("\t", 2, 2)
    # a tie on fields goes to the earlier delimiter in the fixed order
    assert _csv.sniff(b"a,b\tc\n1,2\t3\n", complete=True) == _csv.Dialect(",", 2, 2)


def test_a_head_cut_inside_a_record_ignores_the_cut_record() -> None:
    whole = b"a,b,c\n1,2,3\n4,5,6\n"
    assert _csv.sniff(whole[:-4], complete=False) == _csv.Dialect(",", 3, 2)
    assert _csv.sniff(whole[:-4], complete=True) is None  # a complete file ends as it ends


# --- Cells are the text written ----------------------------------------------------------------


def test_cells_keep_the_text_exactly_and_blank_is_unknown() -> None:
    output = run(b"a,b,c,d\n007,1e5,  x ,\n 0x1F,TRUE,,   \n", csv_header="first_row")
    first, second = rows(output)
    assert texts(first) == ["007", "1e5", "  x ", None]
    assert texts(second) == [" 0x1F", "TRUE", None, None]
    assert isinstance(first.cells[3], Unknown) and isinstance(second.cells[3], Unknown)


def test_quotes_delimiters_and_line_breaks_inside_a_field_are_text() -> None:
    data = b'id,note\r\n1,"a, ""quoted""\r\nline two"\r\n2,plain\r\n'
    output = run(data, csv_header="first_row")
    assert [texts(r) for r in rows(output)] == [
        ["1", 'a, "quoted"\r\nline two'],
        ["2", "plain"],
    ]


def test_the_telemetry_fixture_reads_as_written() -> None:
    output = run(fixture("telemetry_amr.csv"), csv_header="first_row")
    assert header_of(output) == ("t_ms", "battery_pct", "note", "x_m", "y_m")
    assert [texts(r) for r in rows(output)] == [
        ["0", "97.5", "docked, charging", "0.0", "0.0"],
        ["1000", "97.1", 'said "go"', "0.25", "0.0"],
        ["2000", None, None, "0.5", "0.01"],
        ["3000", "96.8", "n/a", "0.75", "0.02"],
    ]
    # "n/a" is not a blank and not a null the file defines: it stays the text it is
    assert isinstance(rows(output)[3].cells[2], Known)


def test_the_tsv_fixture_is_sniffed_and_reads_unicode() -> None:
    output = run(fixture("inspection_quadruped.tsv"), csv_header="first_row")
    assert [texts(r) for r in rows(output)] == [
        ["A-001", "Hip actuator FL", "OK", "2026-03-02"],
        ["A-002", "Foot sensor ÅR", "WORN", "2026-03-02"],
        ["A-003", "Battery bay", None, None],
    ]
    (dialect,) = [f for f in output.findings() if f.code == "tabular.csv_dialect"]
    assert dialect.details["delimiter"] == "\t" and dialect.details["delimiter_rule"] == "sniffed"


def test_a_bom_is_skipped_and_reported() -> None:
    output = run(b"\xef\xbb\xbfa,b\n1,2\n", csv_header="first_row")
    assert header_of(output) == ("a", "b")
    (dialect,) = [f for f in output.findings() if f.code == "tabular.csv_dialect"]
    assert dialect.details["bom"] is True
    # the BOM's bytes are not a record: the header row starts after them
    (record,) = rows(output)
    assert texts(record) == ["1", "2"]


def test_the_header_is_the_configs_to_say() -> None:
    data = b"a,b\n1,2\n"
    declared = run(data, csv_header="first_row")
    assert isinstance(table(declared).header, Known)
    assert [r.row for r in rows(declared)] == [1]

    none = run(data, csv_header="none")
    assert isinstance(table(none).header, NotApplicable)
    assert [r.row for r in rows(none)] == [0, 1]

    undeclared = run(data)  # nobody says: the first row is a record and the header is unknown
    assert isinstance(table(undeclared).header, Unknown)
    assert [texts(r) for r in rows(undeclared)] == [["a", "b"], ["1", "2"]]


def test_every_csv_says_how_it_was_read() -> None:
    output = run(b"a,b,c\n1,2,3\r\n")
    (dialect,) = [f for f in output.findings() if f.code == "tabular.csv_dialect"]
    assert dialect.details == {
        "bom": False,
        "delimiter": ",",
        "delimiter_rule": "sniffed",
        "encoding": "utf-8",
        "header": "undeclared",
        "line_endings": {"crlf": 1, "lf": 1},
        "quote": '"',
    }
    declared = run(b"a;b\n1;2\n", csv_delimiter=";")
    (again,) = [f for f in declared.findings() if f.code == "tabular.csv_dialect"]
    assert again.details["delimiter_rule"] == "configured"


# --- Citations ---------------------------------------------------------------------------------


def test_a_row_is_cited_by_its_position_and_a_cell_by_row_column_and_name() -> None:
    output = run(b"id,name\n7,arm\n8,base\n", csv_header="first_row")
    header = table(output)
    second = rows(output)[1]
    assert second.provenance.evidence.locator == (Row(2),)
    assert second.cell_evidence(header, 1).locator == (RowCell(2, 1, "name"),)
    assert second.cell_evidence(header, 0).locator == (RowCell(2, 0, "id"),)


def test_without_a_header_a_cell_names_no_column() -> None:
    output = run(b"7,arm\n8,base\n", csv_header="none")
    cell = rows(output)[0].cell_evidence(table(output), 1).locator[0]
    assert isinstance(cell, RowCell) and cell.row == 0 and cell.column == 1


def messy() -> bytes:
    lines = [b"key,value,comment"]
    for i in range(1, 40):
        lines.append(f'{i},{i * 1.5},"line {i}, with ""quotes""\nand a break"'.encode())
        if i % 7 == 0:
            lines.append(f"{i},,".encode())
    return b"\n".join(lines) + b"\n"


def test_every_cell_traces_to_the_row_and_column_an_independent_reader_finds() -> None:
    data = messy()
    output = run(data, csv_header="first_row")
    expected = list(csv.reader(io.StringIO(data.decode(), newline="")))
    assert len(rows(output)) == len(expected) - 1
    for record in rows(output):
        oracle = expected[record.row]
        assert len(record.cells) == len(oracle)
        for column, cell in enumerate(record.cells):
            evidence = record.cell_evidence(table(output), column)
            step = evidence.locator[-1]
            assert isinstance(step, RowCell) and (step.row, step.column) == (record.row, column)
            assert step.column_name == expected[0][column]
            if oracle[column].strip():
                assert isinstance(cell, Known) and cell.value == oracle[column]
            else:
                assert isinstance(cell, Unknown)


def test_a_findings_bytes_hold_the_rows_it_names() -> None:
    data = fixture("ragged.csv")
    output = run(data, csv_delimiter=",", csv_header="first_row")
    (ragged,) = [f for f in output.findings() if f.code == "tabular.csv_ragged_rows"]
    assert ragged.details["rows"] == [2, 3]
    assert isinstance(ragged.subject, EvidenceRef)
    (step,) = ragged.subject.locator
    assert isinstance(step, ByteRange)
    assert data[step.offset : step.offset + step.length] == b"2,south\n3,east,7.0,extra\n"


# --- Damage ------------------------------------------------------------------------------------


def test_ragged_rows_keep_their_own_length_and_are_reported_once_per_block() -> None:
    output = run(fixture("ragged.csv"), csv_delimiter=",", csv_header="first_row")
    assert [len(r.cells) for r in rows(output)] == [3, 2, 4, 3, 3]
    assert codes(output) == [
        "tabular.csv_dialect",
        "tabular.csv_malformed_quote",
        "tabular.csv_ragged_rows",
        "tabular.invalid_utf8",
    ]
    by_row = {r.row: r for r in rows(output)}
    assert texts(by_row[4]) == ["4", "westx", "1.0"]  # text after a closing quote is kept
    assert texts(by_row[5]) == ["5", None, "2.0"]  # a cell that is not UTF-8 is Unknown
    assert isinstance(by_row[5].cells[1], Unknown)
    (bad,) = [f for f in output.findings() if f.code == "tabular.invalid_utf8"]
    assert bad.records == (by_row[5].id,)


def test_an_unclosed_quote_runs_to_the_end_and_says_so() -> None:
    output = run(fixture("unclosed_quote.csv"), csv_header="first_row")
    last = rows(output)[-1]
    assert texts(last) == ["2", "the operator wrote that the arm\n"]
    (finding,) = [f for f in output.findings() if f.code == "tabular.csv_unterminated_quote"]
    assert finding.details == {"row": 2} and finding.records == (last.id,)


def test_a_truncated_file_loses_only_its_cut_record() -> None:
    data = fixture("telemetry_amr.csv")
    for cut in range(len(data) + 1):
        output = run(data[:cut], csv_delimiter=",")
        assert len(rows(output)) <= 5  # never invents a record
        for record in rows(output):
            assert len(record.cells) in range(1, 6)


def test_empty_and_degenerate_files_are_tables_with_no_rows() -> None:
    assert rows(run(b"")) == []
    assert rows(run(b"\n\r\n\n")) == []
    only_header = run(b"a,b,c\n", csv_header="first_row")
    assert rows(only_header) == [] and isinstance(table(only_header).header, Known)
    assert [texts(r) for r in rows(run(b"x", csv_delimiter=","))] == [["x"]]


def test_a_row_over_max_row_bytes_has_no_record_and_the_others_survive() -> None:
    data = b"a,b\n1,2\n" + b"3," + b"x" * 5000 + b"\n5,6\n"
    output = run(data, max_row_bytes=100)
    assert [texts(r) for r in rows(output)] == [["a", "b"], ["1", "2"], ["5", "6"]]
    (finding,) = [f for f in output.findings() if f.code == "tabular.row_too_large"]
    assert finding.details["row"] == 2 and finding.details["bytes"] == 5002


def test_a_row_with_more_cells_than_max_columns_has_no_record() -> None:
    wide = b",".join(b"c" for _ in range(50))
    output = run(b"a,b\n" + wide + b"\n1,2\n", max_columns=10, csv_delimiter=",")
    assert [r.row for r in rows(output)] == [0, 2]
    assert "tabular.too_many_columns" in codes(output)


def test_rows_past_max_rows_are_not_read() -> None:
    data = b"a,b\n" + b"".join(b"%d,%d\n" % (i, i) for i in range(10))
    output = run(data, max_rows=4, csv_delimiter=",")
    assert [r.row for r in rows(output)] == [0, 1, 2, 3]
    (finding,) = [f for f in output.findings() if f.code == "tabular.row_limit"]
    assert finding.details == {"max_rows": 4, "row": 4}


def test_a_hostile_quote_run_is_linear_and_bounded() -> None:
    # many opening quotes, no closing one: one record, no recursion, no quadratic scan
    data = b"a,b\n" + b',"' * 20000 + b"\n"
    output = run(data, max_row_bytes=1 << 20, max_columns=100_000, csv_delimiter=",")
    assert len(rows(output)) <= 2


# --- Streaming and determinism ----------------------------------------------------------------


def snapshot(output: SourceOutput) -> list[tuple[int, list[object]]]:
    return [(r.row, texts(r)) for r in rows(output)]


def test_blocks_do_not_change_what_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    data = messy()
    whole = run(data, csv_header="first_row")
    monkeypatch.setattr(_csv, "BLOCK_ROWS", 3)
    cut = run(data, csv_header="first_row")
    assert len(cut.plan.chunks) > len(whole.plan.chunks) + 5
    assert snapshot(cut) == snapshot(whole)
    assert {r.id for r in cut.records()} == {r.id for r in whole.records()}


def first_row(found: IngestFinding) -> int:
    rows = found.details["rows"]
    assert isinstance(rows, list) and isinstance(rows[0], int)
    return rows[0]


def test_findings_are_counted_per_block(monkeypatch: pytest.MonkeyPatch) -> None:
    data = b"a,b\n" + b"1\n" * 7
    monkeypatch.setattr(_csv, "BLOCK_ROWS", 3)
    output = run(data, csv_delimiter=",", csv_header="first_row")
    ragged = [f for f in output.findings() if f.code == "tabular.csv_ragged_rows"]
    ragged.sort(key=first_row)  # findings come sorted by id, not by place
    assert [f.details["count"] for f in ragged] == [3, 3, 1]


def test_the_same_source_and_config_give_the_same_bytes() -> None:
    data = messy()
    assert as_bytes(run(data, csv_header="first_row")) == as_bytes(
        run(data, csv_header="first_row")
    )
    assert as_bytes(run(data)) != as_bytes(run(data, csv_header="first_row"))  # config is in the id


def pieces_of(data: bytes, cuts: Iterable[int]) -> list[bytes]:
    points = sorted({0, len(data), *(c % (len(data) + 1) for c in cuts)})
    return [data[a:b] for a, b in pairwise(points)]


FIELD: Final = st.one_of(
    st.text(alphabet='ab ,"\n\r;é1', max_size=6),
    st.sampled_from(["", " ", '"', '""', "x,y", "q\nr"]),
)


def encode(table_rows: list[list[str]]) -> bytes:
    sink = io.StringIO(newline="")
    writer = csv.writer(sink, lineterminator="\n")
    writer.writerows(table_rows)
    return sink.getvalue().encode()


@settings(max_examples=150, deadline=None)
@given(
    st.lists(st.lists(FIELD, min_size=1, max_size=4), max_size=8),
    st.lists(st.integers(0, 200), max_size=6),
)
def test_scanning_in_any_pieces_finds_the_same_records(
    table_rows: list[list[str]], cuts: list[int]
) -> None:
    data = encode(table_rows)
    whole = list(_csv.records([data], b",", 0))
    split = list(_csv.records(pieces_of(data, cuts), b",", 0))
    assert split == whole


@settings(max_examples=150, deadline=None)
@given(st.lists(st.lists(FIELD, min_size=1, max_size=4), min_size=1, max_size=8))
def test_a_written_table_reads_back_as_the_csv_module_reads_it(table_rows: list[list[str]]) -> None:
    data = encode(table_rows)
    # the module's own writer wrote it, so its reader is the oracle (empty rows have no record)
    expected = [r for r in csv.reader(io.StringIO(data.decode(), newline="")) if r]
    found = list(_csv.records([data], b",", 0))
    assert len(found) == len(expected)
    for record, oracle in zip(found, expected, strict=True):
        fields, malformed = _csv.split(data[record.start : record.stop], b",")
        assert not malformed
        assert [f.decode() for f in fields] == oracle
        assert record.fields == len(oracle)
