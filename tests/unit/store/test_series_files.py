"""Series files (ADR 0025): sorted, deterministic, bounded in memory, checked against streams."""

import random
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from neptune.identity import canonical_json
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.ids import RecordId
from neptune.model.knowledge import AssertionKind, Known, NotCovered, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance
from neptune.model.run import Stream
from neptune.model.series import (
    ColumnType,
    SeriesBatch,
    SeriesColumn,
    SeriesProvenance,
    step_template,
)
from neptune.store import series as series_module
from neptune.store.series import (
    SERIES_SETTINGS,
    STREAM_KEY,
    SeriesError,
    arrow_schema,
    check_series,
    check_settings,
    merge_runs,
    read_rows,
    write_run,
    write_series,
)

SOURCE = "sha256:" + "a" * 64
TRANSFORM = transform_record(adapter_id="demo", adapter_version="1.0.0", config={})
CLOCK = RecordId("rec:sha256:" + "c" * 64)
CLOCK_1 = RecordId("rec:sha256:" + "d" * 64)


def make_stream(clocks: tuple[RecordId, ...] = (CLOCK,), declared_at: int = 0) -> Stream:
    provenance = Provenance(
        EvidenceRef(SOURCE, (ByteRange(declared_at, 8),)),  # type: ignore[arg-type]
        TRANSFORM.id,
        AssertionKind.OBSERVED,
    )
    return Stream(
        id=evidence_record_id("stream", provenance.evidence, TRANSFORM),
        provenance=provenance,
        run=RecordId("rec:sha256:" + "b" * 64),
        topic=Known("/imu"),
        schema_name=NotCovered(),
        schema_encoding=NotCovered(),
        schema_definition=NotCovered(),
        message_encoding=NotCovered(),
        metadata=(),
        clocks=clocks,
        message_count=Unknown(),
        first=Unknown(),
        last=Unknown(),
        series=SeriesProvenance(
            SOURCE,  # type: ignore[arg-type]
            (step_template("byte_range", per_row=("length", "offset")),),
            AssertionKind.OBSERVED,
        ),
    )


STREAM = make_stream()


def batch(
    rows: Sequence[tuple[int, int | None]],
    stream: Stream = STREAM,
    extra: Sequence[SeriesColumn] = (),
) -> SeriesBatch:
    """Rows as ``(seq, clock-0 ticks or None)``; a ``None`` time is ``unknown``."""
    seqs = [seq for seq, _ in rows]
    columns = [
        SeriesColumn("seq", ColumnType.INT64, tuple(seqs)),
        SeriesColumn("time/0", ColumnType.INT64, tuple(ticks for _, ticks in rows)),
        SeriesColumn(
            "state/time/0",
            ColumnType.STRING,
            tuple("known" if ticks is not None else "unknown" for _, ticks in rows),
        ),
        SeriesColumn("locator/0/offset", ColumnType.INT64, tuple(8 + 4 * s for s in seqs)),
        SeriesColumn("locator/0/length", ColumnType.INT64, tuple(4 for _ in seqs)),
        SeriesColumn("value/v", ColumnType.FLOAT32, tuple(float(s) / 2 for s in seqs)),
    ]
    return SeriesBatch(stream.id, (*columns, *extra))


def order(rows: Sequence[tuple[int, int | None]]) -> list[tuple[int, int | None]]:
    return sorted(rows, key=lambda row: (row[1] is None, row[1] or 0, row[0]))


def written(tmp_path: Path, batches: list[SeriesBatch], name: str = "s.parquet") -> Path:
    path = tmp_path / name
    write_series(STREAM, batches, path)
    return path


# --- Writing -----------------------------------------------------------------------------------


def test_rows_are_sorted_by_clock_zero_unknown_last_then_seq(tmp_path: Path) -> None:
    rows = [(0, 30), (1, None), (2, 10), (3, 30), (4, None), (5, 20)]
    path = written(tmp_path, [batch(rows[:3]), batch(rows[3:])])
    got = [(row["seq"], row["time/0"]) for row in read_rows(path)]
    assert got == order(rows) == [(2, 10), (5, 20), (0, 30), (3, 30), (1, None), (4, None)]
    assert check_series(STREAM, path) == 6
    for row in read_rows(path):
        STREAM.check_row(row)


def test_a_series_file_alone_rebuilds_its_stream_and_every_rows_provenance(tmp_path: Path) -> None:
    path = written(tmp_path, [batch([(0, 5), (1, 6)])])
    line = pq.ParquetFile(path).schema_arrow.metadata[STREAM_KEY]
    assert line == canonical_json.dumps(STREAM.to_json())
    rows = list(read_rows(path))
    assert STREAM.row_provenance(rows[1]).evidence == EvidenceRef(
        SOURCE,  # type: ignore[arg-type]
        (ByteRange(12, 4),),
    )


def test_every_column_type_round_trips(tmp_path: Path) -> None:
    extra = {
        "a": SeriesColumn("value/a", ColumnType.UINT64, (2**64 - 1, 0)),
        "b": SeriesColumn("value/b", ColumnType.INT8, (-128, 127)),
        "c": SeriesColumn("value/c", ColumnType.BOOL, (True, False)),
        "d": SeriesColumn("value/d", ColumnType.STRING, ("é", "")),
        "e": SeriesColumn("value/e", ColumnType.BINARY, (b"\x00\xff", b"")),
        "f": SeriesColumn("value/f", ColumnType.FLOAT64, (float("nan"), -0.0)),
        "g": SeriesColumn("value/g", ColumnType.FLOAT32, ((1.5, 2.25), ()), repeated=True),
        "h": SeriesColumn("value/h", ColumnType.UINT16, (None, 7)),
        "i": SeriesColumn("state/value/h", ColumnType.STRING, ("not_covered", "known")),
    }
    path = written(tmp_path, [batch([(0, 1), (1, 2)], extra=tuple(extra.values()))])
    rows = list(read_rows(path))
    assert [row["value/a"] for row in rows] == [2**64 - 1, 0]
    assert [row["value/g"] for row in rows] == [[1.5, 2.25], []]
    assert [row["value/e"] for row in rows] == [b"\x00\xff", b""]
    assert rows[0]["value/f"] != rows[0]["value/f"]  # NaN stays NaN
    assert check_series(STREAM, path) == 2
    schema = pq.ParquetFile(path).schema_arrow
    assert schema.field("value/a").type == pa.uint64()
    assert schema.field("value/g").type == pa.list_(pa.field("item", pa.float32(), nullable=False))
    assert not schema.field("value/g").type.value_field.nullable  # a repeated cell has no null item
    assert not schema.field("seq").nullable and not schema.field("state/time/0").nullable


def test_a_stream_with_no_samples_has_a_typed_empty_series(tmp_path: Path) -> None:
    path = written(tmp_path, [batch([])])
    assert list(read_rows(path)) == []
    assert check_series(STREAM, path) == 0
    assert pq.ParquetFile(path).schema_arrow.field("value/v").type == pa.float32()


def group_rows(monkeypatch: pytest.MonkeyPatch, rows: int) -> None:
    """Row groups of ``rows`` rows: the writer's constant and the setting that records it."""
    monkeypatch.setattr(series_module, "ROW_GROUP_ROWS", rows)
    monkeypatch.setitem(series_module.SERIES_SETTINGS, "row_group_rows", rows)


@pytest.fixture
def small_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row groups of 4 rows, reads of 3 and merges of 2 runs, so small tests reach every path."""
    group_rows(monkeypatch, 4)
    monkeypatch.setattr(series_module, "READ_ROWS", 3)
    monkeypatch.setattr(series_module, "FAN_IN", 2)


@pytest.mark.usefixtures("small_groups")
def test_row_groups_hold_a_fixed_number_of_rows_of_the_merged_order(tmp_path: Path) -> None:
    rows = [(seq, (seq * 7) % 11) for seq in range(10)]
    path = written(tmp_path, [batch(rows[i : i + 3]) for i in range(0, 10, 3)])
    metadata = pq.ParquetFile(path).metadata
    assert [metadata.row_group(i).num_rows for i in range(metadata.num_row_groups)] == [4, 4, 2]
    assert [(r["seq"], r["time/0"]) for r in read_rows(path)] == order(rows)


ROWS = st.lists(
    st.tuples(st.integers(-5, 5) | st.none(), st.booleans()), min_size=0, max_size=40
).map(lambda rows: [(seq, ticks) for seq, (ticks, _) in enumerate(rows)])


@pytest.mark.usefixtures("small_groups")
@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ROWS, st.data())
def test_the_same_rows_in_any_chunks_and_order_give_the_same_file(
    tmp_path: Path, rows: list[tuple[int, int | None]], data: st.DataObject
) -> None:
    def cut(seed: int) -> list[SeriesBatch]:
        shuffled = random.Random(seed).sample(rows, len(rows))
        bounds = sorted(random.Random(seed + 1).sample(range(1, len(rows) + 1), min(len(rows), 4)))
        pieces = [shuffled[a:b] for a, b in zip([0, *bounds], [*bounds, len(rows)], strict=True)]
        return [batch(piece) for piece in pieces if piece] or [batch([])]

    first, second = data.draw(st.integers(0, 10**6)), data.draw(st.integers(0, 10**6))
    one = written(tmp_path, cut(first), f"{first}-a.parquet")
    two = written(tmp_path, cut(second), f"{second}-b.parquet")
    assert one.read_bytes() == two.read_bytes()
    assert [(r["seq"], r["time/0"]) for r in read_rows(one)] == order(rows)
    assert check_series(STREAM, one) == len(rows)


def wide_batch(rows: Sequence[tuple[int, int | None]]) -> SeriesBatch:
    """Rows with a 36-float64 covariance, as a ROS pose message carries one."""
    covariance = SeriesColumn(
        "value/covariance",
        ColumnType.FLOAT64,
        tuple(tuple(float(seq * 36 + k) / 7 for k in range(36)) for seq, _ in rows),
        repeated=True,
    )
    return batch(rows, extra=(covariance,))


def test_bytes_do_not_depend_on_how_wide_rows_were_chunked(tmp_path: Path) -> None:
    """pyarrow cuts pages and abandons dictionaries per array it is handed, so a row group must
    be written from whole arrays, not from the pieces the merge produced (review of MVL-72).

    70,000 rows of 36 doubles cross the 1 MiB page limit and the 1 MiB dictionary limit many times
    in each row group, and the second row group starts inside the rows.
    """
    count = 70_000
    rows: list[tuple[int, int | None]] = [(seq, seq * 10) for seq in range(count)]
    quarter = count // 4
    cuts = {
        "one": [rows],
        "quarters-reversed": [rows[i : i + quarter] for i in range(0, count, quarter)][::-1],
        "interleaved": [rows[k::5] for k in range(5)],  # every run spans the whole time range
        "uneven": [rows[:23_333], rows[23_333:46_666], rows[46_666:]],
    }
    files = {}
    for name, pieces in cuts.items():
        path = written(tmp_path, [wide_batch(piece) for piece in pieces], f"{name}.parquet")
        files[name] = path.read_bytes()
    assert len(set(files.values())) == 1, {name: len(data) for name, data in files.items()}
    metadata = pq.ParquetFile(tmp_path / "one.parquet").metadata
    assert [metadata.row_group(i).num_rows for i in range(2)] == [65_536, count - 65_536]
    group = metadata.row_group(0)
    covariance = next(
        group.column(i)
        for i in range(group.num_columns)
        if group.column(i).path_in_schema.startswith("value/covariance")
    )
    assert {"RLE_DICTIONARY", "PLAIN"} <= set(covariance.encodings)  # the dictionary gave up
    assert covariance.total_uncompressed_size > 8 * 1024 * 1024  # many 1 MiB pages
    assert check_series(STREAM, tmp_path / "one.parquet") == count


@pytest.mark.usefixtures("small_groups")
def test_many_runs_merge_in_rounds(tmp_path: Path) -> None:
    rows = [(seq, 1000 - seq if seq % 3 else None) for seq in range(37)]
    runs = []
    for index in range(0, 37, 2):
        run = tmp_path / f"run-{index}.parquet"
        write_run([batch(rows[index : index + 2])], run)
        runs.append(run)
    merge_runs(STREAM, runs, tmp_path / "out.parquet")
    assert [(r["seq"], r["time/0"]) for r in read_rows(tmp_path / "out.parquet")] == order(rows)
    assert sorted(p.name for p in tmp_path.iterdir() if not p.name.startswith("run-")) == [
        "out.parquet"
    ]


def test_merging_needs_memory_that_does_not_grow_with_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readers hold one row group per run and the writer one row group, whatever the total."""
    group_rows(monkeypatch, 512)
    monkeypatch.setattr(series_module, "READ_ROWS", 256)

    def peak(rows: int) -> int:
        runs = []
        for start in range(0, rows, rows // 8):
            run = tmp_path / f"{rows}-{start}.parquet"
            # Every run spans the whole time range, so the merge interleaves all eight.
            write_run([batch([(s, s * 8 % rows) for s in range(start, start + rows // 8)])], run)
            runs.append(run)
        pool = pa.proxy_memory_pool(pa.default_memory_pool())
        previous = pa.default_memory_pool()
        pa.set_memory_pool(pool)
        try:
            merge_runs(STREAM, runs, tmp_path / f"{rows}.parquet")
        finally:
            pa.set_memory_pool(previous)
        assert check_series(STREAM, tmp_path / f"{rows}.parquet") == rows
        return int(pool.max_memory())

    small, large = peak(2**14), peak(2**17)  # eight times the rows
    assert large < 1.5 * small, (small, large)


# --- What is refused ---------------------------------------------------------------------------


def test_runs_must_belong_to_the_stream_and_agree(tmp_path: Path) -> None:
    other = make_stream(declared_at=64)
    write_run([batch([(0, 1)])], tmp_path / "mine.parquet")
    with pytest.raises(SeriesError, match="is not a run of"):
        merge_runs(other, [tmp_path / "mine.parquet"], tmp_path / "out.parquet")
    extra = SeriesColumn("value/w", ColumnType.INT8, (1,))
    write_run([batch([(1, 2)], extra=(extra,))], tmp_path / "wider.parquet")
    with pytest.raises(SeriesError, match="disagree"):
        merge_runs(STREAM, [tmp_path / "mine.parquet", tmp_path / "wider.parquet"], tmp_path / "o")
    with pytest.raises(SeriesError, match="no runs"):
        merge_runs(STREAM, [], tmp_path / "o")
    with pytest.raises(SeriesError, match="at least one batch"):
        write_run([], tmp_path / "none.parquet")
    with pytest.raises(SeriesError, match="one stream"):
        write_run([batch([(0, 1)]), batch([(1, 1)], stream=other)], tmp_path / "two.parquet")


def test_a_series_missing_a_clock_or_locator_column_is_refused(tmp_path: Path) -> None:
    two_clocks = make_stream((CLOCK, CLOCK_1))
    run = tmp_path / "run.parquet"
    write_run([batch([(0, 1)], stream=two_clocks)], run)
    with pytest.raises(SeriesError, match=r"lacks columns \['time/1'\]"):
        merge_runs(two_clocks, [run], tmp_path / "out.parquet")


def raw(tmp_path: Path, table: Any, metadata: bytes | None = None) -> Path:
    """A file written around the series writer, for what it would never write."""
    path = tmp_path / "raw.parquet"
    line = canonical_json.dumps(STREAM.to_json()) if metadata is None else metadata
    pq.write_table(table.replace_schema_metadata({STREAM_KEY: line}), path)
    return path


def good_table(rows: Sequence[tuple[int, int | None]]) -> Any:
    one = batch(rows)
    schema = arrow_schema(one.schema())
    return pa.Table.from_arrays(
        [pa.array({c.name: c for c in one.columns}[f.name].values, type=f.type) for f in schema],
        schema=schema,
    )


def test_check_series_refuses_what_the_writer_never_writes(tmp_path: Path) -> None:
    rows = [(0, 1), (1, 2)]
    assert check_series(STREAM, raw(tmp_path, good_table(rows))) == 2
    with pytest.raises(SeriesError, match="does not hold stream"):
        check_series(STREAM, raw(tmp_path, good_table(rows), b"{}"))
    with pytest.raises(SeriesError, match="not sorted"):
        check_series(STREAM, raw(tmp_path, good_table([(1, 2), (0, 1)])))
    with pytest.raises(SeriesError, match="not sorted"):
        check_series(STREAM, raw(tmp_path, good_table([(0, None), (1, 5)])))
    table = good_table(rows)
    state = table.schema.get_field_index("state/time/0")
    bad_state = table.set_column(state, table.schema.field(state), pa.array(["known", "maybe"]))
    with pytest.raises(SeriesError, match="not a series state"):
        check_series(STREAM, raw(tmp_path, bad_state))
    value = table.schema.get_field_index("value/v")
    nulled = table.set_column(value, "value/v", pa.array([1.0, None], type=pa.float32()))
    with pytest.raises(SeriesError, match="holds nulls but has no state column"):
        check_series(STREAM, raw(tmp_path, nulled))
    known_null = table.set_column(
        table.schema.get_field_index("time/0"), "time/0", pa.array([1, None], type=pa.int64())
    )
    with pytest.raises(SeriesError, match="exactly where its state is known"):
        check_series(STREAM, raw(tmp_path, known_null))
    stray = table.append_column("extra", pa.array([1, 2]))
    with pytest.raises(SeriesError, match="breaks the contract"):
        check_series(STREAM, raw(tmp_path, stray))
    locator = table.schema.get_field_index("locator/0/offset")
    loose = table.set_column(locator, pa.field("locator/0/offset", pa.int64()), table[locator])
    with pytest.raises(SeriesError, match="not nullable"):
        check_series(STREAM, raw(tmp_path, loose))
    with pytest.raises(SeriesError, match="not a readable Parquet file"):
        check_series(STREAM, b"PAR1 not really PAR1")


@pytest.mark.parametrize(
    "values",
    [
        pa.array([1, 2], type=pa.timestamp("ns", tz="UTC")),
        pa.array([Decimal("1.50"), Decimal("2.25")], type=pa.decimal128(10, 2)),
        pa.array([{"x": 1}, {"x": 2}], type=pa.struct([("x", pa.int64())])),
        pa.array(["a", "b"]).dictionary_encode(),
        pa.array([[1.0], [2.0, None]], type=pa.list_(pa.float64())),
        pa.array([[[1.0]], [[2.0]]], type=pa.list_(pa.list_(pa.float64()))),
        pa.array(["a", "b"], type=pa.large_string()),
    ],
    ids=["timestamp", "decimal", "struct", "dictionary", "nullable items", "nested", "large"],
)
def test_a_value_column_of_a_type_no_adapter_emits_is_refused(tmp_path: Path, values: Any) -> None:
    """A value column is one ColumnType or a list of it with no null item (ADR 0025 §5)."""
    table = good_table([(0, 1), (1, 2)]).append_column("value/x", values)
    assert pq.read_schema(raw(tmp_path, table)).field("value/x").type == values.type
    with pytest.raises(SeriesError, match=r"column value/x \(.*\) breaks the contract"):
        check_series(STREAM, raw(tmp_path, table))


def test_every_row_is_checked_wherever_it_sits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locator or seq that does not parse is refused in the middle of a batch and in a later one,
    not only at the head of a batch (review of MVL-72)."""
    monkeypatch.setattr(series_module, "READ_ROWS", 3)
    rows: list[tuple[int, int | None]] = [(seq, 100 + seq) for seq in range(7)]
    table = good_table(rows)
    for row in read_rows(raw(tmp_path, table)):
        STREAM.check_row(row)
    assert check_series(STREAM, raw(tmp_path, table)) == 7
    offset = table.schema.get_field_index("locator/0/offset")
    for bad in (0, 1, 5):  # at the head of the first batch, inside it, inside the last
        offsets = [8 + 4 * seq for seq in range(7)]
        offsets[bad] = -4
        negative = table.set_column(offset, table.schema.field(offset), pa.array(offsets))
        with pytest.raises(SeriesError, match=rf"seq {bad}: .*offset"):
            check_series(STREAM, raw(tmp_path, negative))
        with pytest.raises(ValueError, match="offset"):
            STREAM.row_provenance(list(read_rows(raw(tmp_path, negative)))[bad])
    seq = table.schema.get_field_index("seq")
    seqs = table.set_column(seq, table.schema.field(seq), pa.array([0, 1, 2, 3, -4, 5, 6]))
    with pytest.raises(SeriesError, match="never negative"):
        check_series(STREAM, raw(tmp_path, seqs))


def test_the_settings_name_the_writer() -> None:
    assert SERIES_SETTINGS["writer"] == f"pyarrow {pa.cpp_version}"
    assert SERIES_SETTINGS["row_group_rows"] == series_module.ROW_GROUP_ROWS
    assert check_settings(dict(SERIES_SETTINGS)) == SERIES_SETTINGS


def test_a_file_must_agree_with_the_settings_recorded_for_it(tmp_path: Path) -> None:
    """The settings' shape is pinned; their values are held against the file's own metadata."""
    path = written(tmp_path, [batch([(0, 1), (1, 2)])])
    assert check_series(STREAM, path, SERIES_SETTINGS) == 2
    for key, value, message in (
        ("writer", "pyarrow 0.0.0", f"written by 'pyarrow {pa.cpp_version}'"),
        ("format_version", "2.4", "Parquet format 2.6"),
        ("row_group_rows", 1, "row groups do not hold 1 rows"),
    ):
        with pytest.raises(SeriesError, match=message):
            check_series(STREAM, path, {**SERIES_SETTINGS, key: value})
    for bad in (
        None,
        {},
        [*SERIES_SETTINGS.items()],
        {**SERIES_SETTINGS, "extra": 1},
        {key: value for key, value in SERIES_SETTINGS.items() if key != "writer"},
        {**SERIES_SETTINGS, "row_group_rows": "65536"},
        {**SERIES_SETTINGS, "dictionary": 1},
    ):
        with pytest.raises(SeriesError, match="series setting"):
            check_settings(bad)


def test_a_stream_line_changes_the_file(tmp_path: Path) -> None:
    other = replace(STREAM, topic=Known("/imu2"))
    a, b = tmp_path / "a.parquet", tmp_path / "b.parquet"
    write_series(STREAM, [batch([(0, 1)])], a)
    write_series(other, [batch([(0, 1)])], b)
    assert a.read_bytes() != b.read_bytes()
