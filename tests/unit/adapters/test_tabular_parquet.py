"""The tabular adapter on Parquet: declared types, the footer's tables, damage and limits.

The oracle is pyarrow read directly, which the fixture generator also checks against the format's
official reader (``make_tabular_fixtures.py --check``): every decoded cell must equal what a plain
``read()`` of the same file gives, and each undecoded column must say so.
"""

import datetime
from pathlib import Path
from typing import Any, Final

import pyarrow.parquet as pq
import pytest

from neptune.adapters.contract import SIGNATURE, VERIFIED, ProbeHints
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.tabular import TabularAdapter, _parquet
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.knowledge import Known, KnownAbsent, NotApplicable, Unknown
from neptune.model.provenance import AdapterLocator, ByteRange, Provenance, Row, RowCell
from neptune.model.world import StructuredRecord, StructuredTable

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "tabular"
EPOCH: Final = datetime.date(1970, 1, 1)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes, **config: Any) -> SourceOutput:
    return ingest_source(TabularAdapter(), BytesReader(data), config)


def codes(output: SourceOutput) -> list[str]:
    return sorted(finding.code for finding in output.findings())


def as_bytes(output: SourceOutput) -> bytes:
    return b"".join(canonical_json.dumps(r.to_json()) + b"\n" for r in output.package_records())


def tables(output: SourceOutput) -> dict[str, StructuredTable]:
    """The tables by name: ``data`` (cites the whole file) and the footer's by their step."""
    found: dict[str, StructuredTable] = {}
    for record in output.records():
        if isinstance(record, StructuredTable):
            last = record.provenance.evidence.locator[-1]
            if isinstance(last, AdapterLocator):
                found[last.kind.removeprefix("tabular:")] = record
            else:
                found["data"] = record
    return found


def rows_of(output: SourceOutput, table: StructuredTable) -> list[StructuredRecord]:
    found = [r for r in output.records() if isinstance(r, StructuredRecord) and r.table == table.id]
    return sorted(found, key=lambda record: record.row)


def cells(record: StructuredRecord) -> list[object]:
    return [getattr(cell, "value", None) for cell in record.cells]


HUMANOID: Final = fixture("humanoid_joints.parquet")


@pytest.fixture(scope="module")
def humanoid() -> SourceOutput:
    return run(HUMANOID)


# --- Probe -------------------------------------------------------------------------------------


def probe(data: bytes, size: int | None = None) -> float:
    head = data[:65536]
    return (
        TabularAdapter()
        .probe(head, ProbeHints("x", len(data) if size is None else size))
        .confidence
    )


def test_a_whole_parquet_file_is_verified_by_its_tail() -> None:
    assert probe(HUMANOID) == VERIFIED
    assert probe(HUMANOID, size=10_000_000) == SIGNATURE  # the head is only a prefix of more


def test_a_damaged_parquet_file_is_still_claimed_by_its_magic() -> None:
    assert probe(fixture("truncated.parquet")) == SIGNATURE
    assert probe(fixture("bad_tail.parquet")) == SIGNATURE
    assert probe(b"PAR1") == SIGNATURE


def test_the_magic_is_what_declares_the_format() -> None:
    (parquet,) = [f for f in TabularAdapter().descriptor.formats if f.name == "Parquet"]
    assert [(m.offset, m.data) for m in parquet.magic] == [(0, b"PAR1")]


# --- Declared types ----------------------------------------------------------------------------


def test_the_data_table_cites_the_file_and_its_header_cites_the_footer(
    humanoid: SourceOutput,
) -> None:
    data = tables(humanoid)["data"]
    assert data.provenance.evidence.locator == (ByteRange(0, len(HUMANOID)),)
    assert isinstance(data.header, Known)
    assert data.header.value == (
        "stamp",
        "joint",
        "position_rad",
        "effort_nm",
        "torque",
        "state.mode",
        "state.fault",
        "tags.list.element",
        "raw",
        "day",
    )
    footer_step = data.header.provenance.evidence.locator[0]  # type: ignore[union-attr]
    assert isinstance(footer_step, ByteRange)
    assert footer_step.offset + footer_step.length + 8 == len(HUMANOID)  # the footer, then its tail


def test_every_decoded_cell_equals_what_pyarrow_reads(humanoid: SourceOutput) -> None:
    oracle = pq.read_table(FIXTURES / "humanoid_joints.parquet").to_pylist()
    records = rows_of(humanoid, tables(humanoid)["data"])
    assert [r.row for r in records] == list(range(10))
    for record, expected in zip(records, oracle, strict=True):
        stamp = expected["stamp"]
        position = expected["position_rad"]
        assert cells(record) == [
            _micros(stamp),
            expected["joint"],
            position,
            expected["effort_nm"],
            format(expected["torque"], "f"),
            expected["state"]["mode"],
            expected["state"]["fault"],
            None,  # a list's items are not cells
            None,  # bytes are not cells
            (expected["day"] - EPOCH).days,
        ]


def _micros(stamp: datetime.datetime) -> int:
    delta = stamp - datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def test_a_null_is_known_absent_citing_the_footer_that_declares_the_column(
    humanoid: SourceOutput,
) -> None:
    data = tables(humanoid)["data"]
    row3 = rows_of(humanoid, data)[3]
    cell = row3.cells[2]
    assert isinstance(cell, KnownAbsent)
    assert isinstance(data.header, Known)
    assert isinstance(cell.provenance, Provenance)
    assert isinstance(data.header.provenance, Provenance)
    assert cell.provenance.evidence == data.header.provenance.evidence


def test_types_are_kept_and_nothing_is_converted(humanoid: SourceOutput) -> None:
    row = rows_of(humanoid, tables(humanoid)["data"])[5]
    values = cells(row)
    assert values[0] == 1772438400100000 and type(values[0]) is int  # microseconds, not a datetime
    assert values[3] == 7.5 and type(values[3]) is float  # a FLOAT widened exactly
    assert values[4] == "0.625"  # a DECIMAL is exact text at its scale
    assert values[6] is False and values[9] == 20515  # days since the epoch, not a date


def test_the_schema_table_says_what_the_integers_mean(humanoid: SourceOutput) -> None:
    schema = tables(humanoid)["schema"]
    by_path = {
        cells(r)[0]: dict(zip(_parquet.SCHEMA, cells(r), strict=True))
        for r in rows_of(humanoid, schema)
    }
    assert by_path["stamp"]["time_unit"] == "microseconds"
    assert by_path["stamp"]["adjusted_to_utc"] is True
    assert by_path["torque"]["precision"] == 10 and by_path["torque"]["scale"] == 3
    assert by_path["day"]["logical_type"] == "Date"
    assert by_path["position_rad"]["physical_type"] == "DOUBLE"
    assert by_path["joint"]["converted_type"] == "UTF8"
    schema_cells = rows_of(humanoid, schema)[0].cells
    assert isinstance(schema_cells[5], NotApplicable)  # a timestamp has no bit width


def test_row_group_statistics_are_cells_of_their_own_table(humanoid: SourceOutput) -> None:
    groups = tables(humanoid)["row_groups"]
    records = rows_of(humanoid, groups)
    assert [r.row for r in records] == list(range(30))  # 3 row groups x 10 columns
    named = {
        (cells(r)[0], cells(r)[1]): dict(zip(_parquet.ROW_GROUPS, cells(r), strict=True))
        for r in records
    }
    position = named[(0, "position_rad")]
    assert position["num_rows"] == 4 and position["null_count"] == 1
    assert position["min"] == -1.0 and position["max"] == 0.0
    assert named[(1, "joint")]["min"] == "hip_l" and named[(1, "joint")]["max"] == "knee_r"
    assert named[(0, "torque")]["max"] == "0.375"
    assert named[(2, "day")]["min"] == 20515
    # statistics for a column that is not decoded are Unknown, not guessed
    assert isinstance(named_cell(records, 0, "raw", "min"), Unknown)
    row = records[0]
    assert row.provenance.evidence.locator[-1] == Row(row.row)


def named_cell(records: list[StructuredRecord], group: int, column: str, field: str) -> Any:
    (found,) = [r for r in records if cells(r)[:2] == [group, column]]
    return found.cells[_parquet.ROW_GROUPS.index(field)]


def test_the_footers_key_value_metadata_is_a_table(humanoid: SourceOutput) -> None:
    found = {cells(r)[0]: cells(r)[1] for r in rows_of(humanoid, tables(humanoid)["key_value"])}
    assert found["robot"] == "humanoid-h1" and found["frame"] == "base_link"


def test_columns_without_a_cell_type_say_so_once_each(humanoid: SourceOutput) -> None:
    undecoded = [f for f in humanoid.findings() if f.code == "tabular.parquet_column_not_decoded"]
    assert sorted(str(f.details["column"]) for f in undecoded) == ["raw", "tags.list.element"]
    assert codes(humanoid) == ["tabular.parquet_column_not_decoded"] * 2


def test_a_cell_traces_to_its_row_and_column_in_the_file(humanoid: SourceOutput) -> None:
    data = tables(humanoid)["data"]
    record = rows_of(humanoid, data)[6]
    evidence = record.cell_evidence(data, 1)
    assert evidence.locator[-1] == RowCell(6, 1, "joint")
    assert evidence.source == record.provenance.evidence.source
    oracle = pq.read_table(FIXTURES / "humanoid_joints.parquet").column("joint")[6].as_py()
    assert cells(record)[1] == oracle


# --- Blocks and determinism --------------------------------------------------------------------


def snapshot(output: SourceOutput) -> list[bytes]:
    return sorted(canonical_json.dumps(r.to_json()) for r in output.records())


def test_a_row_group_is_a_chunk(humanoid: SourceOutput) -> None:
    assert len(humanoid.plan.chunks) == 1 + 3


def test_slicing_a_row_group_does_not_change_what_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    whole = run(HUMANOID)
    monkeypatch.setattr(_parquet, "BLOCK_ROWS", 3)
    cut = run(HUMANOID)
    assert len(cut.plan.chunks) > len(whole.plan.chunks)
    assert snapshot(cut) == snapshot(whole)


def test_the_same_source_and_config_give_the_same_bytes() -> None:
    assert as_bytes(run(HUMANOID)) == as_bytes(run(HUMANOID))


def test_inspect_reads_the_footer_and_no_pages() -> None:
    adapter = TabularAdapter()
    from neptune.adapters.contract import configure

    reader = BytesReader(HUMANOID)
    summary = adapter.inspect(reader, configure(adapter.descriptor, {})).summary
    assert summary["layout"] == "parquet" and summary["rows"] == 10
    assert summary["row_groups"] == 3 and summary["columns"] == 10


# --- Damage ------------------------------------------------------------------------------------


def test_a_truncated_file_is_one_finding_and_no_records() -> None:
    output = run(fixture("truncated.parquet"))
    assert codes(output) == ["tabular.parquet_footer"]
    assert output.records() == ()


def test_a_wrong_closing_magic_is_a_footer_finding() -> None:
    assert codes(run(fixture("bad_tail.parquet"))) == ["tabular.parquet_footer"]


def test_an_encrypted_footer_is_reported_and_never_decoded() -> None:
    data = HUMANOID[:-4] + b"PARE"
    output = run(data)
    assert codes(output) == ["tabular.parquet_encrypted"] and output.records() == ()


@pytest.mark.parametrize("data", [b"PAR1", b"PAR1PAR1", b"PAR1" + bytes(20) + b"PAR1"])
def test_files_too_small_to_hold_a_footer_are_findings(data: bytes) -> None:
    output = run(data)
    assert codes(output) == ["tabular.parquet_footer"] and output.records() == ()


def test_a_footer_length_beyond_the_file_is_refused_before_anything_is_read() -> None:
    data = HUMANOID[:-8] + (10**9).to_bytes(4, "little") + b"PAR1"
    output = run(data)
    assert codes(output) == ["tabular.parquet_footer"] and output.records() == ()


@pytest.mark.parametrize("first", [0, 50])
def test_a_corrupt_footer_is_a_finding_not_a_crash(first: int) -> None:
    damaged = bytearray(HUMANOID)
    end = len(damaged) - 8
    for index in range(end - 300 + first, end - 250 + first):
        damaged[index] ^= 0x55
    output = run(bytes(damaged))
    assert codes(output) == ["tabular.parquet_footer"] and output.records() == ()


def test_corrupt_pages_lose_their_row_group_and_the_others_survive() -> None:
    damaged = bytearray(HUMANOID)
    for index in range(4, 60):  # the first page headers of row group 0
        damaged[index] ^= 0xFF
    output = run(bytes(damaged))
    assert "tabular.parquet_rows_unreadable" in codes(output)
    data = tables(output)["data"]
    assert [r.row for r in rows_of(output, data)] == [4, 5, 6, 7, 8, 9]
    (lost,) = [f for f in output.findings() if f.code == "tabular.parquet_rows_unreadable"]
    assert lost.details["first"] == 0 and lost.details["rows"] == 4
    # the footer's own tables are intact
    assert len(rows_of(output, tables(output)["schema"])) == 10


# --- Hostile sizes -----------------------------------------------------------------------------


def test_a_footer_over_max_footer_bytes_is_not_decoded() -> None:
    output = run(HUMANOID, max_footer_bytes=100)
    assert codes(output) == ["tabular.parquet_footer_too_large"] and output.records() == ()


def test_a_schema_with_more_leaves_than_max_columns_is_not_decoded() -> None:
    output = run(HUMANOID, max_columns=5)
    assert codes(output) == ["tabular.too_many_columns"] and output.records() == ()


def test_column_chunks_decoding_past_the_limit_are_not_read() -> None:
    output = run(HUMANOID, max_column_chunk_bytes=90)
    too_large = [f for f in output.findings() if f.code == "tabular.parquet_row_group_too_large"]
    assert sorted(str(f.details["row_group"]) for f in too_large) == ["0", "1"]
    data = tables(output)["data"]
    assert [r.row for r in rows_of(output, data)] == [8, 9]


def test_rows_past_max_rows_are_not_read() -> None:
    output = run(HUMANOID, max_rows=5)
    assert "tabular.row_limit" in codes(output)
    data = tables(output)["data"]
    assert [r.row for r in rows_of(output, data)] == [0, 1, 2, 3]


def test_an_empty_parquet_file_is_a_table_without_rows(tmp_path: Path) -> None:
    import pyarrow as pa

    path = tmp_path / "empty.parquet"
    pq.write_table(pa.table({"a": pa.array([], pa.int64())}), path)
    output = run(path.read_bytes())
    data = tables(output)["data"]
    assert rows_of(output, data) == [] and isinstance(data.header, Known)
    assert codes(output) == []


def test_a_file_whose_every_column_is_undecoded_still_has_its_rows(tmp_path: Path) -> None:
    import pyarrow as pa

    path = tmp_path / "bytes.parquet"
    pq.write_table(pa.table({"raw": [b"\x00", b"\xff"]}), path)
    output = run(path.read_bytes())
    data = tables(output)["data"]
    records = rows_of(output, data)
    assert [isinstance(r.cells[0], Unknown) for r in records] == [True, True]
    assert codes(output) == ["tabular.parquet_column_not_decoded"]


def test_undecoded_columns_do_not_make_rows_the_data_cannot_back(tmp_path: Path) -> None:
    import pyarrow as pa

    path = tmp_path / "bytes.parquet"
    pq.write_table(pa.table({"raw": [b"\x00" * 50] * 20}), path, compression="none")
    damaged = bytearray(path.read_bytes())
    for index in range(4, 40):  # the column chunk's page header
        damaged[index] ^= 0xFF
    output = run(bytes(damaged))
    assert codes(output) == [
        "tabular.parquet_column_not_decoded",
        "tabular.parquet_rows_unreadable",
    ]
    assert rows_of(output, tables(output)["data"]) == []
