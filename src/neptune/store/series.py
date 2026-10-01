"""Series files: one sorted, deterministic Parquet file per stream (ADR 0018, ADR 0025).

A stream's rows arrive as ``SeriesBatch``es from many chunks, in any order. The store writes them
in two steps, each in memory that does not grow with the stream's length:

1. ``write_run``: one chunk's batches of one stream, sorted, as a Parquet *run*.
2. ``merge_runs``: every run of a stream, merged into the stream's series file.

Rows are sorted by their clock-0 ticks (unknown last), then ``seq`` (ADR 0018 §7). Row groups hold
``ROW_GROUP_ROWS`` rows each, counted along the merged order, each written from one contiguous
array per column, and the writer's settings are pinned (``SERIES_SETTINGS``, recorded in the
manifest's ``store``). So a file's bytes depend only on its rows: the same rows, cut into any
chunks and merged in any order, give the same file.

``check_series`` verifies a series file against its stream and the settings it was written with,
without loading it whole: the stream line in its metadata, its columns and types, the order, the
null rules, and every row's ``seq`` and locator.
"""

import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from neptune.identity import canonical_json
from neptune.model.ids import RecordId, parse_record_id
from neptune.model.jsonvalue import JsonObject
from neptune.model.run import Stream
from neptune.model.series import (
    LOCATOR,
    SEQ,
    SERIES_STATES,
    STATE,
    TIME,
    VALUE,
    ColumnType,
    SeriesBatch,
    time_column,
)

ROW_GROUP_ROWS: Final = 65_536
# Every setting that shapes a series file's bytes, pyarrow's defaults included so that a default
# that moves cannot move the bytes. The manifest records them (ADR 0022 §2), and a new pyarrow
# version is a new writer: its files may differ, so it is part of the settings. The writer is named
# by the Arrow C++ version, the one every file's ``created_by`` carries, so that a development
# build (wheel ``X.Y.Z.devN``, files ``X.Y.Z-SNAPSHOT``) still reads back what it wrote.
SERIES_SETTINGS: Final[JsonObject] = {
    "byte_stream_split": False,
    "compliant_nested_type": True,
    "compression": "zstd",
    "compression_level": 3,
    "data_page_size": 1024 * 1024,
    "data_page_version": "1.0",
    "dictionary": True,
    "dictionary_page_size_limit": 1024 * 1024,
    "format_version": "2.6",
    "max_rows_per_page": 20_000,
    "page_checksum": False,
    "page_index": True,
    "row_group_rows": ROW_GROUP_ROWS,
    "statistics": True,
    "store_schema": True,
    "write_batch_size": 1024,
    "writer": f"pyarrow {pa.cpp_version}",
}
STREAM_KEY: Final = b"neptune.stream"  # a series file's Stream line (ADR 0018 §8)
RUN_KEY: Final = b"neptune.series_run"  # a run's stream id
_CREATED_BY: Final = re.compile(r"parquet-cpp-arrow version (\S+)")  # pyarrow's created_by

# How many runs one merge reads at once, and how many rows of each it holds. Neither shapes the
# output's bytes; together they bound the merge's memory.
FAN_IN: Final = 32
READ_ROWS: Final = 4096

_CLOCK_0: Final = time_column(0)
_SORT: Final = [(_CLOCK_0, "ascending", "at_end"), (SEQ, "ascending", "at_end")]
_ARROW_TYPES: Final = {
    ColumnType.BOOL: pa.bool_(),
    ColumnType.INT8: pa.int8(),
    ColumnType.INT16: pa.int16(),
    ColumnType.INT32: pa.int32(),
    ColumnType.INT64: pa.int64(),
    ColumnType.UINT8: pa.uint8(),
    ColumnType.UINT16: pa.uint16(),
    ColumnType.UINT32: pa.uint32(),
    ColumnType.UINT64: pa.uint64(),
    ColumnType.FLOAT32: pa.float32(),
    ColumnType.FLOAT64: pa.float64(),
    ColumnType.STRING: pa.string(),
    ColumnType.BINARY: pa.binary(),
}
# A repeated cell is a tuple of scalars, so a repeated column's items are never null; the schema
# says so, pyarrow enforces it when writing, and the reader checks it by type.
_VALUE_TYPES: Final = (
    *_ARROW_TYPES.values(),
    *(pa.list_(pa.field("item", kind, nullable=False)) for kind in _ARROW_TYPES.values()),
)

Source = Path | bytes


class SeriesError(ValueError):
    """Batches, runs or a series file break the series contract (ADR 0018, ADR 0025)."""


# --- Schema ------------------------------------------------------------------------------------


def _never_null(name: str) -> bool:
    return name == SEQ or name.startswith((f"{LOCATOR}/", f"{STATE}/"))


def arrow_schema(columns: Sequence[tuple[str, ColumnType, bool]]) -> Any:
    """The Arrow schema of a series with these columns, in name order."""
    fields = []
    for name, kind, repeated in sorted(columns):
        arrow = _ARROW_TYPES[kind]
        if repeated:
            arrow = pa.list_(pa.field("item", arrow, nullable=False))
        fields.append(pa.field(name, arrow, nullable=not _never_null(name)))
    return pa.schema(fields)


def _table(batch: SeriesBatch, schema: Any) -> Any:
    columns = {column.name: column for column in batch.columns}
    arrays = [pa.array(columns[field.name].values, type=field.type) for field in schema]
    return pa.Table.from_arrays(arrays, schema=schema)


def _open(source: Source) -> Any:
    try:
        return pq.ParquetFile(pa.BufferReader(source) if isinstance(source, bytes) else source)
    except (pa.ArrowException, OSError) as exc:
        raise SeriesError(f"not a readable Parquet file: {exc}") from exc


def _writer(destination: Path, schema: Any) -> Any:
    return pq.ParquetWriter(
        destination,
        schema,
        compression=SERIES_SETTINGS["compression"],
        compression_level=SERIES_SETTINGS["compression_level"],
        data_page_size=SERIES_SETTINGS["data_page_size"],
        data_page_version=SERIES_SETTINGS["data_page_version"],
        dictionary_pagesize_limit=SERIES_SETTINGS["dictionary_page_size_limit"],
        max_rows_per_page=SERIES_SETTINGS["max_rows_per_page"],
        store_schema=SERIES_SETTINGS["store_schema"],
        use_byte_stream_split=SERIES_SETTINGS["byte_stream_split"],
        use_compliant_nested_type=SERIES_SETTINGS["compliant_nested_type"],
        use_dictionary=SERIES_SETTINGS["dictionary"],
        version=SERIES_SETTINGS["format_version"],
        write_batch_size=SERIES_SETTINGS["write_batch_size"],
        write_page_checksum=SERIES_SETTINGS["page_checksum"],
        write_page_index=SERIES_SETTINGS["page_index"],
        write_statistics=SERIES_SETTINGS["statistics"],
    )


def _write_group(writer: Any, group: Any) -> None:
    """One row group from one contiguous array per column.

    pyarrow cuts pages and falls back from dictionary encoding per array it is handed, checking
    the limits every ``write_batch_size`` values from each array's start. A group assembled from
    the pieces the merge produced would carry those seams into its bytes; combined, the bytes
    depend on the rows alone. The copy is one row group, so memory stays bounded.
    """
    writer.write_table(group.combine_chunks(), row_group_size=ROW_GROUP_ROWS)


def _write(destination: Path, schema: Any, tables: Iterator[Any]) -> None:
    """Write ``tables``, already in order, as row groups of exactly ``ROW_GROUP_ROWS`` rows."""
    writer = _writer(destination, schema)
    try:
        pending, rows = [], 0
        for table in tables:
            pending.append(table)
            rows += table.num_rows
            while rows >= ROW_GROUP_ROWS:
                whole = pa.concat_tables(pending)
                _write_group(writer, whole.slice(0, ROW_GROUP_ROWS))
                pending, rows = [whole.slice(ROW_GROUP_ROWS)], whole.num_rows - ROW_GROUP_ROWS
        if rows:
            _write_group(writer, pa.concat_tables(pending))
    finally:
        writer.close()


# --- Runs --------------------------------------------------------------------------------------


def write_run(batches: Sequence[SeriesBatch], destination: Path) -> None:
    """One chunk's batches of one stream, sorted, as a run file. A run may hold no rows."""
    if not batches:
        raise SeriesError("a run holds at least one batch, even an empty one")
    stream, schema = batches[0].stream, batches[0].schema()
    for batch in batches:
        if batch.stream != stream or batch.schema() != schema:
            raise SeriesError("a run's batches are of one stream and agree on their columns")
    arrow = arrow_schema(schema).with_metadata({RUN_KEY: stream.encode()})
    table = pa.concat_tables([_table(batch, arrow) for batch in batches])
    table = table.take(pc.sort_indices(table, sort_keys=_SORT))
    _write(destination, arrow, iter([table]))


def _run_stream(run: Any) -> RecordId:
    metadata = run.schema_arrow.metadata or {}
    try:
        return parse_record_id(metadata[RUN_KEY].decode())
    except (KeyError, UnicodeDecodeError, ValueError) as exc:
        raise SeriesError("a run names its stream in its metadata") from exc


# --- Merging -----------------------------------------------------------------------------------

_Key = tuple[bool, int, int]  # (clock 0 unknown, its ticks or 0, seq)


def _key(table: Any, row: int) -> _Key:
    ticks = table.column(_CLOCK_0)[row].as_py()
    return (ticks is None, ticks if ticks is not None else 0, table.column(SEQ)[row].as_py())


def _count_up_to(table: Any, key: _Key) -> int:
    """How many leading rows of a sorted ``table`` sort at or before ``key``."""
    unknown, ticks, seq = key
    clock, seqs = table.column(_CLOCK_0), table.column(SEQ)
    at_most_seq = pc.less_equal(seqs, seq)
    if unknown:
        mask = pc.or_(pc.is_valid(clock), pc.and_(pc.is_null(clock), at_most_seq))
    else:
        earlier = pc.fill_null(pc.less(clock, ticks), False)
        same = pc.fill_null(pc.equal(clock, ticks), False)
        mask = pc.or_(earlier, pc.and_(same, at_most_seq))
    return int(pc.sum(pc.cast(mask, pa.int64())).as_py() or 0)


def _merged(runs: Sequence[Any]) -> Iterator[Any]:
    """The rows of sorted runs, in sorted order, as sorted tables: a bounded-memory k-way merge.

    Each round emits every buffered row at or before the frontier, the smallest last buffered key
    among runs with rows left to read. Every row still unread sorts after the frontier, because
    keys are unique (``seq`` is), so the rounds' outputs follow one another in order.
    """
    readers = [run.iter_batches(batch_size=READ_ROWS) for run in runs]
    schema = runs[0].schema_arrow
    buffers: list[Any] = [None] * len(runs)
    exhausted = [False] * len(runs)

    def refill(index: int) -> None:
        while not exhausted[index] and (buffers[index] is None or not buffers[index].num_rows):
            batch = next(readers[index], None)
            if batch is None:
                exhausted[index] = True
            else:
                buffers[index] = pa.Table.from_batches([batch], schema=schema)

    for index in range(len(runs)):
        refill(index)
    while True:
        live = [i for i, b in enumerate(buffers) if b is not None and b.num_rows]
        if not live:
            return
        reading = [_key(buffers[i], buffers[i].num_rows - 1) for i in live if not exhausted[i]]
        pieces = []
        for index in live:
            buffer = buffers[index]
            count = _count_up_to(buffer, min(reading)) if reading else buffer.num_rows
            pieces.append(buffer.slice(0, count))
            buffers[index] = buffer.slice(count)
        merged = pa.concat_tables(pieces)
        yield merged.take(pc.sort_indices(merged, sort_keys=_SORT))
        for index in live:
            refill(index)


def merge_runs(stream: Stream, runs: Sequence[Path], destination: Path) -> None:
    """Merge every run of ``stream`` into its series file at ``destination``.

    A stream with no samples still has a run: an empty one, which types its columns. More than
    ``FAN_IN`` runs are merged in rounds, through temporary runs beside ``destination``.
    """
    if not runs:
        raise SeriesError(f"stream {stream.id} has no runs; an empty run types an empty series")
    opened = [_open(run) for run in runs]
    schema = opened[0].schema_arrow.remove_metadata()
    for run in opened:
        if _run_stream(run) != stream.id:
            raise SeriesError(f"a run of {_run_stream(run)} is not a run of {stream.id}")
        if not run.schema_arrow.remove_metadata().equals(schema):
            raise SeriesError(f"stream {stream.id}: runs disagree on their columns")
    _check_columns(stream, schema)
    if len(runs) > FAN_IN:
        with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
            rounds = []
            for start in range(0, len(runs), FAN_IN):
                part = Path(scratch) / f"{start}.parquet"
                group = opened[start : start + FAN_IN]
                _write(part, group[0].schema_arrow, _merged(group))
                rounds.append(part)
            merge_runs(stream, rounds, destination)
        return
    line = canonical_json.dumps(stream.to_json())
    _write(destination, schema.with_metadata({STREAM_KEY: line}), _merged(opened))


def write_series(stream: Stream, batches: Sequence[SeriesBatch], destination: Path) -> None:
    """``batches`` from one chunk each, in any order, as ``stream``'s series file."""
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        runs = []
        for index, batch in enumerate(batches):
            run = Path(scratch) / f"{index}.parquet"
            write_run([batch], run)
            runs.append(run)
        merge_runs(stream, runs, destination)


# --- Reading and checking ----------------------------------------------------------------------


def read_rows(source: Source) -> Iterator[dict[str, object]]:
    """Every row of a series file, in file order, as column name to cell."""
    for batch in _open(source).iter_batches(batch_size=READ_ROWS):
        yield from batch.to_pylist()


def _check_columns(stream: Stream, schema: Any) -> None:
    """The column contract by name and type (ADR 0018 §4)."""
    names = set(schema.names)
    missing = [name for name in stream.series_columns() if name not in names]
    if missing:
        raise SeriesError(f"stream {stream.id}: series lacks columns {missing}")
    for field in schema:
        name, kind = field.name, field.type
        if name == SEQ or name.startswith(f"{TIME}/"):
            if name != SEQ and name not in stream.series_columns():
                raise SeriesError(f"{name} is not a clock of stream {stream.id}")
            ok = kind == pa.int64()
        elif name.startswith(f"{LOCATOR}/"):
            if name not in stream.series_columns():
                raise SeriesError(f"{name} is not a locator field of stream {stream.id}")
            ok = kind in (pa.int64(), pa.float64(), pa.string())
        elif name.startswith(f"{STATE}/"):
            target = name.removeprefix(f"{STATE}/")
            qualifies = target.startswith(f"{TIME}/") or target.startswith(f"{VALUE}/")
            ok = kind == pa.string() and target in names and qualifies
        else:
            # A value column holds one ColumnType, or a list of it with no null items (ADR 0025
            # §5); a timestamp, decimal, struct or dictionary type asserts what no adapter did.
            ok = name.startswith(f"{VALUE}/") and len(name) > len(VALUE) + 1
            ok = ok and any(kind.equals(allowed) for allowed in _VALUE_TYPES)
        if not ok:
            raise SeriesError(f"stream {stream.id}: column {name} ({kind}) breaks the contract")
        if _never_null(name) and field.nullable:
            raise SeriesError(f"{name} is never null, so its field is not nullable")


def _check_order(table: Any, previous: _Key | None) -> None:
    """Rows sort strictly by clock-0 ticks (unknown last), then ``seq``, also across tables."""
    if not table.num_rows:
        return
    if previous is not None and not previous < _key(table, 0):
        raise SeriesError(f"rows are out of order at seq {_key(table, 0)[2]}")
    clock, seqs = table.column(_CLOCK_0), table.column(SEQ)
    if table.num_rows < 2:
        return
    t1, t2 = clock.slice(0, table.num_rows - 1), clock.slice(1)
    s1, s2 = seqs.slice(0, table.num_rows - 1), seqs.slice(1)
    known1, known2 = pc.is_valid(t1), pc.is_valid(t2)
    earlier = pc.fill_null(pc.less(t1, t2), False)
    same = pc.fill_null(pc.equal(t1, t2), False)
    seq_after = pc.less(s1, s2)
    both_known = pc.and_(known1, known2)
    ordered = pc.or_(
        pc.and_(known1, pc.invert(known2)),
        pc.or_(
            pc.and_(both_known, pc.or_(earlier, pc.and_(same, seq_after))),
            pc.and_(pc.and_(pc.invert(known1), pc.invert(known2)), seq_after),
        ),
    )
    if not pc.all(ordered).as_py():
        raise SeriesError("rows are not sorted by clock-0 ticks, unknown last, then seq")


def _check_nulls(table: Any) -> None:
    """Never-null columns hold no null; a wrapped column is null exactly where not known."""
    states = pa.array(sorted(map(str, SERIES_STATES)))
    for name in table.column_names:
        column = table.column(name)
        state_name = f"{STATE}/{name}"
        if name.startswith(f"{STATE}/"):
            if column.null_count or not pc.all(pc.is_in(column, value_set=states)).as_py():
                raise SeriesError(f"{name} holds a value that is not a series state")
        elif state_name in table.column_names:
            known = pc.equal(table.column(state_name), "known")
            if not pc.all(pc.equal(known, pc.is_valid(column))).as_py():
                raise SeriesError(f"{name} must hold a value exactly where its state is known")
        elif column.null_count:
            raise SeriesError(f"{name} holds nulls but has no state column")


def _check_rows(stream: Stream, table: Any) -> None:
    """What ``Stream.check_row`` checks that the whole-batch checks do not: ``seq`` and the locator.

    Names and types (``_check_columns``), the null rules (``_check_nulls``) and the clock-0 ticks
    (``_check_order``) are checked for every row at once. ``seq`` is a position, so never
    negative, which one minimum settles. Each row's locator must parse (``Stream.row_evidence``):
    it is read from the locator columns alone, so a batch of wide value columns is never
    materialised row by row, and only one batch is in memory at a time.
    """
    if not table.num_rows:
        return
    least = pc.min(table.column(SEQ)).as_py()
    if least < 0:
        raise SeriesError(f"stream {stream.id}: seq is a position, never negative; got {least}")
    for row in table.select([SEQ, *stream.series.columns]).to_pylist():
        try:
            stream.row_evidence(row)
        except ValueError as exc:
            raise SeriesError(f"stream {stream.id}, seq {row[SEQ]}: {exc}") from exc


def check_settings(settings: object) -> JsonObject:
    """``store.series`` as a manifest records it: every pinned setting, each of its pinned type.

    Values are not pinned to this writer's: a package written by another pyarrow stays readable.
    ``check_series`` holds each file to what its own metadata can show (writer, format version,
    row-group sizes); the other settings are the writer's inputs, which the file's hash pins.
    """
    if not isinstance(settings, Mapping) or set(settings) != set(SERIES_SETTINGS):
        raise SeriesError(f"series settings must hold exactly {sorted(SERIES_SETTINGS)}")
    for key, pinned in SERIES_SETTINGS.items():
        if type(settings[key]) is not type(pinned):
            raise SeriesError(f"series setting {key} must be a {type(pinned).__name__}")
    return dict(settings)


def _check_written_with(stream: Stream, metadata: Any, settings: JsonObject) -> None:
    """What a file's own metadata tells of its writer agrees with the settings recorded for it."""
    match = _CREATED_BY.fullmatch(metadata.created_by or "")
    writer = f"pyarrow {match[1]}" if match else metadata.created_by
    if writer != settings["writer"]:
        raise SeriesError(
            f"stream {stream.id}: written by {writer!r}, the settings say {settings['writer']!r}"
        )
    if metadata.format_version != settings["format_version"]:
        raise SeriesError(
            f"stream {stream.id}: Parquet format {metadata.format_version}, the settings say"
            f" {settings['format_version']}"
        )
    group_rows = settings["row_group_rows"]
    sizes = [metadata.row_group(i).num_rows for i in range(metadata.num_row_groups)]
    full, last = sizes[:-1], sizes[-1:]
    if any(size != group_rows for size in full) or any(
        not (isinstance(group_rows, int) and 0 < size <= group_rows) for size in last
    ):
        raise SeriesError(f"stream {stream.id}: row groups do not hold {group_rows} rows each")


def check_series(stream: Stream, source: Source, settings: JsonObject = SERIES_SETTINGS) -> int:
    """Verify a series file against ``stream`` and ``settings``; return its row count.

    ``settings`` are what the file was written with, as its package's manifest records them
    (``check_settings``); by default, this writer's. The writer, format version and row-group sizes
    in the file's own metadata must agree with them.
    Reads one batch at a time; every row is checked for order, the null rules, its ``seq`` and
    its locator, which is parsed as ``Stream.check_row`` would. ``seq`` is unique wherever two
    rows share clock-0 ticks; across ticks, the ingest's checks (``neptune.adapters.check``) see
    it.
    """
    series = _open(source)
    metadata = series.schema_arrow.metadata or {}
    if metadata.get(STREAM_KEY) != canonical_json.dumps(stream.to_json()):
        raise SeriesError(f"the series file does not hold stream {stream.id}'s line")
    _check_written_with(stream, series.metadata, settings)
    _check_columns(stream, series.schema_arrow)
    rows, previous = 0, None
    for batch in series.iter_batches(batch_size=READ_ROWS):
        table = pa.Table.from_batches([batch])
        _check_nulls(table)
        _check_order(table, previous)
        _check_rows(stream, table)
        if table.num_rows:
            previous = _key(table, table.num_rows - 1)
        rows += table.num_rows
    return rows
