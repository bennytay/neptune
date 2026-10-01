"""The tabular adapter on JSON and JSON Lines: probing, typed leaves, pointers, damage, limits.

The oracle for every citation is the standard library: a cell's evidence names the row's bytes and
a JSON pointer, and decoding those bytes and following the pointer must give the cell's value.
"""

import json
import math
from collections.abc import Iterable
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from neptune.adapters.contract import PROBE_HEAD_SIZE, STRUCTURE, VERIFIED, ProbeHints
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.tabular import TabularAdapter, _json
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.knowledge import Knowledge, Known, KnownAbsent, NotApplicable, Unknown
from neptune.model.provenance import ByteRange, JsonPointer
from neptune.model.scalars import NonFinite
from neptune.model.world import CellValue, StructuredRecord, StructuredTable

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


def codes(output: SourceOutput) -> list[str]:
    return sorted(finding.code for finding in output.findings())


def probe(data: bytes, name: str = "x") -> float:
    return TabularAdapter().probe(data, ProbeHints(name, len(data))).confidence


def as_bytes(output: SourceOutput) -> bytes:
    return b"".join(canonical_json.dumps(r.to_json()) + b"\n" for r in output.package_records())


def pointer_of(cell: Any) -> str:
    step = cell.provenance.evidence.locator[-1]
    assert isinstance(step, JsonPointer)
    return step.pointer


def follow(document: Any, pointer: str) -> Any:
    """RFC 6901, written independently of the adapter."""
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        document = document[int(token)] if isinstance(document, list) else document[token]
    return document


def value_of(cell: Knowledge[CellValue]) -> object:
    return cell.value if isinstance(cell, Known) else None


def values(record: StructuredRecord) -> dict[str, object]:
    return {pointer_of(c): getattr(c, "value", None) for c in record.cells}


# --- Probe -------------------------------------------------------------------------------------


def test_a_json_table_is_recognised_by_its_content_and_verified() -> None:
    assert probe(fixture("joint_states_arm.json"), "blob") == VERIFIED
    assert probe(fixture("events_auv.jsonl"), "blob") == VERIFIED
    assert probe(b"[[1,2],[3,4]]", "m") == VERIFIED  # an array of arrays is a matrix of rows
    assert probe(b'\xef\xbb\xbf[{"a":1}]', "bom.json") == VERIFIED


@pytest.mark.parametrize(
    "data",
    [
        b'{"name":"base","frame":{"parent":"world"}}',  # one object: a configuration
        b'{"type":"FeatureCollection","features":[{"type":"Feature"}]}',  # GeoJSON
        b'{"columns":["a","b"],"rows":[[1,2],[3,4]]}',  # a table-shaped object
        b"[1,2,3]",  # an array of scalars
        b'["a","b"]',
        b"[]",
        b"[ ]\n",
        b"",
        b"null",
        b'{"a":1}\n',  # a single line: not JSON Lines
    ],
)
def test_json_that_is_not_a_table_is_declined(data: bytes) -> None:
    assert probe(data, "config.json") == 0.0


def test_a_damaged_json_table_is_still_a_table_at_structure() -> None:
    assert probe(fixture("truncated.json"), "t.json") == STRUCTURE
    assert probe(fixture("damaged.jsonl"), "d.jsonl") == STRUCTURE
    assert probe(b'[{"a":1},{"a":2}', "cut") == STRUCTURE


def test_mixed_row_kinds_are_not_a_table() -> None:
    # the first row says what the file is: records then a stray row are a damaged table
    assert probe(b'[{"a":1}, 5]', "x") == STRUCTURE
    assert probe(b'{"a":1}\n[1,2]\n', "x") == STRUCTURE
    assert probe(b'[5, {"a":1}]', "x") == 0.0


def test_a_head_that_stops_inside_a_row_is_judged_by_its_whole_rows() -> None:
    rows_ = b",".join(b'{"id":%d,"pad":"%s"}' % (i, b"x" * 40) for i in range(2000))
    data = b"[" + rows_ + b"]"
    head = data[:4096]
    assert TabularAdapter().probe(head, ProbeHints("big.json", len(data))).confidence == VERIFIED


# --- Types are JSON's --------------------------------------------------------------------------


def one(text: str, **config: Any) -> dict[str, object]:
    """The leaves of a one-row table by pointer."""
    output = run(f"[{text}, {{}}]".encode(), **config)
    return values(rows(output)[0])


def test_json_types_survive_and_null_is_known_absent() -> None:
    output = run(b'[{"s":"hi","i":-3,"f":2.5,"t":true,"b":false,"n":null,"z":0}]')
    (record,) = rows(output)
    assert values(record) == {
        "/s": "hi",
        "/i": -3,
        "/f": 2.5,
        "/t": True,
        "/b": False,
        "/n": None,
        "/z": 0,
    }
    kinds = {pointer_of(c): type(c) for c in record.cells}
    assert kinds["/n"] is KnownAbsent and kinds["/s"] is Known
    assert isinstance(record.cells[3].value, bool)  # type: ignore[union-attr]


def test_a_null_cites_itself_and_a_missing_key_is_not_a_cell() -> None:
    output = run(b'[{"a":null,"b":1},{"b":2}]')
    first, second = rows(output)
    assert len(first.cells) == 2 and len(second.cells) == 1  # nothing says "a" for row 1
    (null,) = [c for c in first.cells if isinstance(c, KnownAbsent)]
    assert pointer_of(null) == "/a"


def test_integers_are_exact_within_64_bits_and_text_beyond() -> None:
    cells = one(
        '{"min":-9223372036854775808,"max":9223372036854775807,"umax":18446744073709551615,'
        '"over":18446744073709551616,"under":-9223372036854775809,'
        '"huge":123456789012345678901234567890}'
    )
    assert cells["/min"] == -(2**63) and cells["/max"] == 2**63 - 1
    assert cells["/umax"] == 2**64 - 1
    assert cells["/over"] == "18446744073709551616"
    assert cells["/under"] == "-9223372036854775809"
    assert cells["/huge"] == "123456789012345678901234567890"


def test_a_number_is_a_double_only_when_the_double_holds_it_exactly() -> None:
    cells = one('{"a":0.1,"b":1.0,"c":1e3,"d":1.10,"e":0.30000000000000004,"f":-0.0}')
    assert cells["/a"] == 0.1 and cells["/b"] == 1.0 and cells["/c"] == 1000.0
    assert cells["/d"] == 1.1 and cells["/e"] == 0.30000000000000004
    assert math.copysign(1, cells["/f"]) == -1  # type: ignore[arg-type]
    # more digits than a double keeps, and an exponent no double reaches: the literal's text
    text = one('{"p":0.1000000000000000055511151231257827,"q":1e400,"r":-1e999}')
    assert text == {
        "/p": "0.1000000000000000055511151231257827",
        "/q": "1e400",
        "/r": "-1e999",
    }


def test_python_style_non_finite_numbers_are_the_reals_they_spell() -> None:
    cells = one('{"a":NaN,"b":Infinity,"c":-Infinity}')
    assert cells == {
        "/a": NonFinite.NAN,
        "/b": NonFinite.POSITIVE_INFINITY,
        "/c": NonFinite.NEGATIVE_INFINITY,
    }


def test_a_declared_string_is_a_value_even_when_it_is_only_whitespace() -> None:
    output = run(b'[{"a":"","b":"  \\t","c":{},"d":[],"e":"x"}]')
    (record,) = rows(output)
    kinds = {pointer_of(c): type(c) for c in record.cells}
    # a whitespace string is a declared value; "" cannot be (the model holds no empty cell text,
    # ADR 0020 §5), so it is Unknown, citing the "" at its pointer; empty containers hold nothing
    assert kinds == {"/a": Unknown, "/b": Known, "/c": Unknown, "/d": Unknown, "/e": Known}
    assert values(record)["/b"] == "  \t"


def test_a_scalar_row_is_cited_at_the_empty_pointer() -> None:
    output = run(b"[5, 6]")
    assert [(pointer_of(r.cells[0]), r.cells[0].value) for r in rows(output)] == [  # type: ignore[union-attr]
        ("", 5),
        ("", 6),
    ]


def test_keys_that_need_escaping_are_cited_by_a_valid_pointer() -> None:
    row = {"a/b": 1, "c~d": 2, "": 3, "0": [4], "é": 5, "x y": {"z": 6}}
    data = json.dumps([row, {}]).encode()
    (first, _) = rows(run(data))
    for cell in first.cells:
        assert follow(row, pointer_of(cell)) == cell.value  # type: ignore[union-attr]
    assert {pointer_of(c) for c in first.cells} >= {"/a~1b", "/c~0d", "/", "/0/0"}


def test_an_unusual_key_lone_surrogate_is_not_cited_but_the_row_survives_as_unknown() -> None:
    output = run(b'[{"ok":1},{"\\ud800":2},{"v":"\\ud800"}]')
    found = rows(output)
    assert [r.row for r in found] == [0, 2]  # the row whose key cannot be cited has no record
    assert isinstance(found[1].cells[0], Unknown)  # a string with a lone surrogate
    assert "tabular.invalid_utf8" in codes(output)


# --- Every cell traces to its place ------------------------------------------------------------


@pytest.mark.parametrize("name", ["joint_states_arm.json", "events_auv.jsonl"])
def test_every_cell_resolves_to_its_value_in_the_bytes_it_cites(name: str) -> None:
    data = fixture(name)
    output = run(data)
    assert rows(output)
    for record in rows(output):
        (span,) = record.provenance.evidence.locator
        assert isinstance(span, ByteRange)
        document = json.loads(data[span.offset : span.offset + span.length])
        for cell in record.cells:
            assert cell.provenance.evidence.locator[0] == span  # type: ignore[union-attr]
            found = follow(document, pointer_of(cell))
            if isinstance(cell, KnownAbsent):
                assert found is None
            elif isinstance(cell, Unknown):
                assert found in ({}, [], "")
            elif isinstance(value_of(cell), str) and isinstance(found, int):  # beyond 64 bits
                assert int(str(value_of(cell))) == found
            else:
                assert value_of(cell) == found


def test_the_events_fixture_reads_as_written() -> None:
    output = run(fixture("events_auv.jsonl"))
    assert isinstance(table(output).header, NotApplicable)  # every row is an object
    by_row = {r.row: values(r) for r in rows(output)}
    assert by_row[0] == {"/t": 0, "/kind": "dive", "/depth_m": 0.0, "/gps": None}
    assert by_row[1]["/sensors/ctd/sal"] == 35.1
    assert by_row[2]["/code"] == 2**64 - 1 and by_row[2]["/legs/2"] is True
    assert by_row[3]["/big"] == "123456789012345678901234567890"  # kept as text, not rounded
    assert codes(output) == ["tabular.json_number_text"]  # the blank line is not a row


def test_a_table_of_mixed_rows_has_an_unknown_header() -> None:
    assert isinstance(table(run(b"[[1,2],[3]]")).header, Unknown)
    assert isinstance(table(run(b'[{"a":1},{"a":2}]')).header, NotApplicable)


# --- Damage ------------------------------------------------------------------------------------


def test_a_syntax_error_is_one_row_without_a_record() -> None:
    output = run(fixture("damaged.jsonl"))
    assert [r.row for r in rows(output)] == [0, 3, 4]  # 1: syntax, 2: not UTF-8
    assert codes(output) == [
        "tabular.invalid_utf8",
        "tabular.json_duplicate_key",
        "tabular.json_syntax",
    ]
    duplicate = rows(output)[1]
    assert values(duplicate) == {"/i": None}  # both members are ambiguous: neither is kept
    assert isinstance(duplicate.cells[0], Unknown)


def test_a_truncated_array_keeps_its_whole_rows_and_says_where_it_broke() -> None:
    data = fixture("truncated.json")
    output = run(data)
    assert [values(r) for r in rows(output)] == [{"/a": 1}, {"/a": 2}]
    (finding,) = [f for f in output.findings() if f.code == "tabular.json_structure"]
    assert finding.details == {"offset": len(data), "rows": 2}


def test_every_truncation_of_a_table_loses_only_what_it_cuts() -> None:
    data = fixture("joint_states_arm.json")
    whole = {r.row: values(r) for r in rows(run(data))}
    for cut in range(len(data)):
        got = run(data[:cut])
        for record in rows(got):
            assert values(record) == whole[record.row]


def test_text_after_the_root_array_is_reported_and_the_array_is_kept() -> None:
    output = run(b'[{"a":1}] trailing')
    assert len(rows(output)) == 1
    assert codes(output) == ["tabular.json_structure"]


def test_a_missing_comma_ends_the_rows_there() -> None:
    output = run(b'[{"a":1} {"a":2}]')
    assert [values(r) for r in rows(output)] == [{"/a": 1}]
    assert codes(output) == ["tabular.json_structure"]


def test_a_json_bom_is_skipped_and_reported() -> None:
    output = run(b'\xef\xbb\xbf[{"a":1}]')
    assert [values(r) for r in rows(output)] == [{"/a": 1}]
    assert codes(output) == ["tabular.json_bom"]
    (row,) = rows(output)
    (span,) = row.provenance.evidence.locator
    assert isinstance(span, ByteRange) and span.offset == 4  # after the BOM and '['


def test_json_lines_tolerates_crlf_blank_lines_and_a_missing_final_newline() -> None:
    output = run(b'{"a":1}\r\n\r\n{"a":2}\n\n{"a":3}')
    assert [values(r) for r in rows(output)] == [{"/a": 1}, {"/a": 2}, {"/a": 3}]
    assert codes(output) == []


# --- Hostile sizes -----------------------------------------------------------------------------


def test_a_row_over_max_row_bytes_has_no_record_and_the_rest_survive() -> None:
    data = b'[{"a":1},{"a":"' + b"x" * 5000 + b'"},{"a":3}]'
    output = run(data, max_row_bytes=100)
    assert [r.row for r in rows(output)] == [0, 2]
    (finding,) = [f for f in output.findings() if f.code == "tabular.row_too_large"]
    assert finding.details["row"] == 1 and finding.details["max_row_bytes"] == 100


def test_nesting_past_max_json_depth_is_refused_without_recursion() -> None:
    deep = b"[" * 200_000 + b"]" * 200_000
    output = run(b'[{"a":1},' + deep + b",{}]", max_row_bytes=1 << 30)
    assert [r.row for r in rows(output)] == [0, 2]
    assert "tabular.json_too_deep" in codes(output)
    fits = run(b'[{"a":' + b"[" * 60 + b"1" + b"]" * 60 + b"}]")
    assert len(rows(fits)) == 1


def test_a_row_with_more_leaves_than_max_columns_has_no_record() -> None:
    wide = json.dumps({f"k{i}": i for i in range(300)}).encode()
    output = run(b"[" + wide + b',{"a":1}]', max_columns=100)
    assert [r.row for r in rows(output)] == [1]
    assert "tabular.too_many_columns" in codes(output)


def test_rows_past_max_rows_are_not_read() -> None:
    data = b"\n".join(b'{"i":%d}' % i for i in range(20))
    output = run(data, max_rows=5)
    assert [r.row for r in rows(output)] == [0, 1, 2, 3, 4]
    (finding,) = [f for f in output.findings() if f.code == "tabular.row_limit"]
    assert finding.details == {"max_rows": 5, "row": 5}


def test_json_lines_with_rows_larger_than_the_probe_head_are_still_json_lines() -> None:
    big = [{"i": i, "blob": "x" * 100_000} for i in range(3)]
    data = b"\n".join(json.dumps(row).encode() for row in big) + b"\n"
    output = run(data)
    assert [r.row for r in rows(output)] == [0, 1, 2] and codes(output) == []
    assert [values(r)["/i"] for r in rows(output)] == [0, 1, 2]
    arrays = b"\n".join(json.dumps(["y" * 100_000, i]).encode() for i in range(3)) + b"\n"
    assert [r.row for r in rows(run(arrays))] == [0, 1, 2]
    # a head holds no second row, so probing cannot claim it; a manifest's choice plans it right
    head = data[:PROBE_HEAD_SIZE]
    assert TabularAdapter().probe(head, ProbeHints("x", len(data))).confidence == 0.0


def test_a_single_object_larger_than_the_probe_head_is_still_one_document() -> None:
    document = json.dumps({"k": "x" * 100_000, "n": 1}).encode()
    assert [r.row for r in rows(run(document))] == [0]


def test_a_depth_limit_beyond_the_interpreters_reach_is_a_finding_not_a_crash() -> None:
    deep = b"[" * 20_000 + b"1" + b"]" * 20_000
    output = run(b"[" + deep + b",[1]]", max_json_depth=100_000)
    assert "tabular.json_too_deep" in codes(output)
    assert [r.row for r in rows(output)] == [1]


def test_a_giant_string_is_one_cell_and_linear_to_scan() -> None:
    data = b'[{"s":"' + b"a\\\\" * 400_000 + b'"}]'
    output = run(data, max_row_bytes=len(data) + 1)
    assert len(rows(output)) == 1


# --- Streaming and determinism -----------------------------------------------------------------


def snapshot(output: SourceOutput) -> list[tuple[int, dict[str, object]]]:
    return [(r.row, values(r)) for r in rows(output)]


@pytest.mark.parametrize("name", ["joint_states_arm.json", "events_auv.jsonl", "damaged.jsonl"])
def test_blocks_do_not_change_what_is_read(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    data = fixture(name)
    whole = run(data)
    monkeypatch.setattr(_json, "BLOCK_ROWS", 1)
    cut = run(data)
    assert len(cut.plan.chunks) > len(whole.plan.chunks)
    assert snapshot(cut) == snapshot(whole)
    assert {r.id for r in cut.records()} == {r.id for r in whole.records()}


def test_the_same_source_and_config_give_the_same_bytes() -> None:
    for name in ("joint_states_arm.json", "events_auv.jsonl", "damaged.jsonl"):
        assert as_bytes(run(fixture(name))) == as_bytes(run(fixture(name)))


JSON_VALUES: Final = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**70), 2**70)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=4), children, max_size=3)
    ),
    max_leaves=12,
)


def pieces_of(data: bytes, cuts: Iterable[int]) -> list[bytes]:
    points = sorted({0, len(data), *(c % (len(data) + 1) for c in cuts)})
    return [data[a:b] for a, b in pairwise(points)]


@settings(max_examples=200, deadline=None)
@given(st.lists(JSON_VALUES, max_size=6), st.lists(st.integers(0, 400), max_size=6))
def test_the_array_scanner_finds_each_element_in_any_pieces(
    elements: list[object], cuts: list[int]
) -> None:
    data = json.dumps(elements, indent=1, ensure_ascii=False).encode()
    scanner = _json.ArrayScanner(0)
    found = list(_json.array_elements(scanner, pieces_of(data, cuts)))
    assert scanner.broken is None
    assert [json.loads(data[e.start : e.end]) for e in found] == elements


@settings(max_examples=150, deadline=None)
@given(
    st.lists(st.dictionaries(st.text(max_size=3), JSON_VALUES, max_size=3), min_size=1, max_size=5)
)
def test_every_leaf_of_a_written_table_resolves_to_its_value(
    table_rows: list[dict[str, Any]],
) -> None:
    data = json.dumps(table_rows).encode()
    output = run(data)
    assert [r.row for r in rows(output)] == list(range(len(table_rows)))
    for record in rows(output):
        (span,) = record.provenance.evidence.locator
        assert isinstance(span, ByteRange)
        document = json.loads(data[span.offset : span.offset + span.length])
        assert document == table_rows[record.row]
        for cell in record.cells:
            found = follow(document, pointer_of(cell))
            if isinstance(cell, KnownAbsent):
                assert found is None
            elif isinstance(cell, Unknown):
                assert found in ({}, [], "")
            elif isinstance(found, bool | str):
                assert value_of(cell) is found or value_of(cell) == found
            elif isinstance(value_of(cell), str):  # an integer beyond 64 bits, kept as its text
                assert int(str(value_of(cell))) == found
            else:
                assert value_of(cell) == found
