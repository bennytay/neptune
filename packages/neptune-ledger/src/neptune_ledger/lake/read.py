"""Scan series files in place across packages, with DuckDB or DataFusion, into Arrow (ADR 0013 §5).

``plan_series`` turns series files and optional time windows into a ``SeriesPlan``: one scan per
file, each on one of the stream's declared clocks, and the output schema. ``DuckDBReader`` and
``DataFusionReader`` render the same plan as SQL over the files where they lie and return the
same table. Each window is a predicate on the file's ``time/<i>`` column in ticks, placed directly
on the scan so the engine prunes row groups by their statistics; ``explain`` shows it there.

The result is one table in ADR 0003 §3's world order, applied to rows: a **partition** per clock,
partitions ordered by (smallest registration key among their files, clock id bytes), and inside
a partition by ``(ticks, package_id, stream_id, seq)``, unknown ticks last. Rows on two clocks
are never interleaved; adjacency means something only within one ``clock`` value.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from neptune.model.ids import parse_record_id
from neptune.model.time import INT64_MAX, INT64_MIN
from neptune_ledger.api.types import CatalogFinding, TimeWindow
from neptune_ledger.lake.series import SeriesFile
from neptune_ledger.lake.store import LocalObjectStore, Location, S3Settings, arrow_s3

FIXED: Final = ("package_id", "stream_id", "clock", "ticks", "ticks_state", "seq")
_PARTITION: Final = "__partition"
_SCAN: Final = "__scan"
_PREFIXES: Final = ("value/", "state/value/", "locator/")


class LakeRequestError(ValueError):
    """A read request outside the lake's contract: a caller error, not a package's fault."""


@dataclass(frozen=True)
class Scan:
    """One file scanned on one of its stream's clocks, with its window if one was given."""

    file: SeriesFile
    clock: str
    column: str
    window: TimeWindow | None
    partition: int
    names: tuple[str, ...]  # the file's columns, as its footer states them

    @property
    def state_column(self) -> str | None:
        """The file's state column for the scanned clock, if its ticks are wrapped."""
        state = f"state/{self.column}"
        return state if state in self.names else None


@dataclass(frozen=True)
class SeriesPlan:
    """What a reader runs: the scans, the value columns kept, the output schema, and why any
    file is not scanned."""

    scans: tuple[Scan, ...]
    columns: tuple[str, ...]
    schema: Any  # pyarrow.Schema
    findings: tuple[CatalogFinding, ...]


def _check_windows(windows: Sequence[TimeWindow]) -> dict[str, TimeWindow]:
    by_clock: dict[str, TimeWindow] = {}
    for window in windows:
        if not isinstance(window, TimeWindow):
            raise LakeRequestError(f"not a TimeWindow: {window!r}")
        try:
            parse_record_id(window.clock)
        except (TypeError, ValueError) as exc:
            raise LakeRequestError(f"a window's clock is a record id: {exc}") from exc
        for bound in (window.first, window.last):
            if type(bound) is not int or not INT64_MIN <= bound <= INT64_MAX:
                raise LakeRequestError(f"window bounds are int64 ticks: {bound!r}")
        if window.first > window.last:
            raise LakeRequestError(f"window first {window.first} is after last {window.last}")
        if window.clock in by_clock:
            raise LakeRequestError(f"two windows on clock {window.clock}")
        by_clock[window.clock] = window
    return by_clock


def plan_series(
    files: Iterable[SeriesFile],
    *,
    windows: Sequence[TimeWindow] | None = None,
    columns: Sequence[str] | None = None,
) -> SeriesPlan:
    """Plan a read of ``files``.

    Without ``windows``, every row of every file, each on its clock 0. With ``windows`` (at most
    one per clock, ``[first, last]`` inclusive, as ``TimeWindow`` everywhere in the catalog
    API), each file is scanned on the one window clock its stream carries, keeping rows whose
    ticks on it lie in the window; a file carrying none of them is not scanned and is reported
    (``unknown_clock``), and one carrying two is a request error. ``columns`` are the value,
    state and locator columns to keep, each in every scanned file with one type; by default,
    every such column all of them share.
    """
    files = list(files)
    if windows is not None and not windows:
        raise LakeRequestError("windows is None for whole files, or at least one window")
    by_clock = _check_windows(windows) if windows is not None else None
    findings: list[CatalogFinding] = []
    chosen: list[tuple[SeriesFile, str, TimeWindow | None]] = []
    for file in files:
        if by_clock is None:
            chosen.append((file, file.clocks[0], None))
            continue
        carried = [clock for clock in file.clocks if clock in by_clock]
        if not carried:
            detail = "the stream carries none of the window clocks; it is not read"
            findings.append(CatalogFinding("unknown_clock", file.stream_id, detail))
        elif len(carried) > 1:
            raise LakeRequestError(f"stream {file.stream_id} carries two window clocks: {carried}")
        else:
            chosen.append((file, carried[0], by_clock[carried[0]]))
    schemas: dict[str, Any] = {}
    readable = []
    for file, clock, window in chosen:
        if not _unchanged(file):
            detail = f"{file.location.url} is missing, a link, or not the size its manifest says"
            findings.append(CatalogFinding("file_missing", file.stream_id, detail))
            continue
        schema = _schema(file.location)
        column = str(file.time_column(clock))
        if schema is None or not all(
            name in schema.names and schema.field(name).type == pa.int64()
            for name in ("seq", column)
        ):
            detail = f"{file.location.url} is not Parquet holding int64 seq and {column}"
            findings.append(CatalogFinding("file_digest_mismatch", file.stream_id, detail))
            continue
        schemas[file.stream_id] = schema
        readable.append((file, clock, window))
    kept = _columns([schemas[f.stream_id] for f, _, _ in readable], columns)
    first: dict[str, int] = {}
    for file, clock, _ in readable:
        first[clock] = min(first.get(clock, file.registration_key), file.registration_key)
    order = sorted(first, key=lambda clock: (first[clock], clock.encode("utf-8")))
    rank = {clock: index for index, clock in enumerate(order)}
    # Scans in output order, so a scan's index sorts as (partition, package id, stream id) do.
    readable.sort(key=lambda c: (rank[c[1]], c[0].package_id.encode(), c[0].stream_id.encode()))
    scans = tuple(
        Scan(
            file,
            clock,
            str(file.time_column(clock)),
            window,
            rank[clock],
            tuple(schemas[file.stream_id].names),
        )
        for file, clock, window in readable
    )
    out = _output_schema([schemas[s.file.stream_id] for s in scans], kept)
    return SeriesPlan(scans, kept, out, tuple(findings))


def _unchanged(file: SeriesFile) -> bool:
    """A local file is still a plain file, reached without a link, of its manifest's size.

    Checked again just before its footer is read, because the engines open the path by name and
    would follow a link swapped in since ``SeriesCatalog.files``. An object store has no links;
    its size was checked when the file was resolved.
    """
    if file.location.s3 is not None:
        return True
    head, _, name = file.location.path.rpartition("/")
    return LocalObjectStore(head or "/").size(name) == file.size


def _schema(location: Location) -> Any:
    try:
        if location.s3 is None:
            return pq.read_schema(location.path)
        return pq.read_schema(location.path, filesystem=arrow_s3(location.s3))
    except (pa.ArrowException, OSError, ValueError):
        return None


def _columns(schemas: list[Any], columns: Sequence[str] | None) -> tuple[str, ...]:
    if columns is not None:
        if isinstance(columns, str) or len(set(columns)) != len(columns):
            raise LakeRequestError("columns are distinct column names")
        for name in columns:
            if not isinstance(name, str) or not name.startswith(_PREFIXES):
                # time/<i> and state/time/<i> name a different clock in each file (ADR 0013 §5).
                detail = f"only value/, state/value/ and locator/ columns: {name!r}"
                raise LakeRequestError(detail)
            types = {str(s.field(name).type) if name in s.names else None for s in schemas}
            if None in types or len(types) > 1:
                raise LakeRequestError(f"column {name!r} is not in every file with one type")
        return tuple(columns)
    if not schemas:
        return ()
    shared = [n for n in schemas[0].names if n.startswith(_PREFIXES)]
    return tuple(
        sorted(
            name
            for name in shared
            if all(
                name in s.names and s.field(name).type == schemas[0].field(name).type
                for s in schemas
            )
        )
    )


_ID: Final = pa.dictionary(pa.int32(), pa.string())


def _output_schema(schemas: list[Any], columns: tuple[str, ...]) -> Any:
    fields = [
        pa.field("package_id", _ID, nullable=False),
        pa.field("stream_id", _ID, nullable=False),
        pa.field("clock", _ID, nullable=False),
        pa.field("ticks", pa.int64()),
        pa.field("ticks_state", _ID, nullable=False),
        pa.field("seq", pa.int64(), nullable=False),
    ]
    for name in columns:
        found = [s.field(name) for s in schemas]
        nullable = any(f.nullable for f in found) if found else True
        kind = found[0].type if found else pa.null()
        fields.append(pa.field(name, kind, nullable=nullable))
    return pa.schema(fields)


# --- SQL ---------------------------------------------------------------------------------------


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _tick(value: int) -> str:
    # -2^63 is not a literal in either dialect (it parses as the negation of 2^63).
    return f"({value + 1} - 1)" if value == INT64_MIN else str(value)


def series_sql(plan: SeriesPlan, tables: Sequence[str]) -> str:
    """The plan as one SQL statement over ``tables`` (one per scan), in both engines' dialect.

    Each row carries its scan's index, not its ids: scans are in output order, so ordering by
    the index orders by (partition, package id, stream id), and ``_conform`` turns the index into
    dictionary-encoded id columns without the engine materialising a string per row.
    """
    branches = []
    for index, (scan, table) in enumerate(zip(plan.scans, tables, strict=True)):
        ticks = _ident(scan.column)
        wrapped = scan.state_column
        state = f"CAST({_ident(wrapped)} AS VARCHAR)" if wrapped else "CAST(NULL AS VARCHAR)"
        values = "".join(f", {_ident(c)}" for c in plan.columns)
        branch = (
            f"SELECT CAST({scan.partition} AS INTEGER) AS {_PARTITION},"
            f" CAST({index} AS INTEGER) AS {_SCAN}, {ticks} AS ticks,"
            f" {state} AS ticks_state, seq{values} FROM {table}"
        )
        if scan.window is not None:
            low, high = _tick(scan.window.first), _tick(scan.window.last)
            branch += f" WHERE {ticks} >= {low} AND {ticks} <= {high}"
        branches.append(branch)
    keep = ", ".join([_SCAN, "ticks", "ticks_state", "seq", *(_ident(c) for c in plan.columns)])
    union = " UNION ALL ".join(branches)
    return (
        f"SELECT {keep} FROM ({union}) AS rows"
        f" ORDER BY {_PARTITION}, ticks NULLS LAST, {_SCAN}, seq"
    )


class SeriesReader(Protocol):
    """An engine that runs a ``SeriesPlan`` over the files in place."""

    name: str

    def read(self, plan: SeriesPlan) -> Any:  # pyarrow.Table
        ...

    def explain(self, plan: SeriesPlan, *, analyze: bool = False) -> str: ...


def _empty(plan: SeriesPlan) -> Any:
    return plan.schema.empty_table()


def _ids(scan_index: Any, per_scan: list[str]) -> Any:
    """A dictionary-encoded id column: each row's scan's id, from the scan index column."""
    values = sorted(set(per_scan), key=lambda v: v.encode("utf-8"))
    position = {v: i for i, v in enumerate(values)}
    mapping = pa.array([position[v] for v in per_scan], pa.int32())
    return pa.DictionaryArray.from_arrays(pc.take(mapping, scan_index), pa.array(values))


def _states(column: Any, rows: int) -> Any:
    """The ticks' knowledge states: the file's state column where it has one, else ``known``
    (a time column without a state column holds a value in every row: root ADR 0018 §6)."""
    if column.null_count == rows:
        zeros = pa.repeat(pa.scalar(0, pa.int32()), rows)
        return pa.DictionaryArray.from_arrays(zeros, pa.array(["known"]))
    filled = pc.fill_null(column.combine_chunks().cast(pa.string()), "known")
    return filled.dictionary_encode().cast(_ID)


def _conform(table: Any, plan: SeriesPlan) -> Any:
    """The engine's table in the plan's schema: the scan index made into id columns, string
    views and list field names normalised."""
    scan_index = table.column(0).cast(pa.int32()).combine_chunks()
    arrays = [
        _ids(scan_index, [s.file.package_id for s in plan.scans]),
        _ids(scan_index, [s.file.stream_id for s in plan.scans]),
        _ids(scan_index, [s.clock for s in plan.scans]),
        table.column(1).cast(pa.int64()),
        _states(table.column(2), table.num_rows),
    ]
    # The engine's columns from seq on are the schema's, two places earlier.
    arrays += [table.column(i - 2).cast(f.type) for i, f in enumerate(plan.schema) if i >= 5]
    return pa.Table.from_arrays(arrays, schema=plan.schema)


class DuckDBReader:
    """DuckDB in memory: local files through its own Parquet reader, S3 objects through pyarrow
    datasets (no DuckDB extension is installed or loaded, so a read never touches the network
    beyond the store)."""

    name = "duckdb"

    def _connect(self, plan: SeriesPlan) -> tuple[Any, list[str]]:
        import duckdb
        import pyarrow.dataset as ds

        con = duckdb.connect(
            ":memory:",
            config={"autoinstall_known_extensions": "false", "autoload_known_extensions": "false"},
        )
        tables = []
        for index, scan in enumerate(plan.scans):
            name = f"series_{index}"
            location = scan.file.location
            folded = {n.lower() for n in scan.names}
            if len(folded) != len(scan.names):
                # DuckDB matches identifiers without case and renames the later twin, so it
                # would return one column's data under the other's name.
                detail = f"{location.url} has columns differing only in case; use DataFusion"
                raise LakeRequestError(detail)
            if location.s3 is None:
                con.read_parquet(location.path).create_view(name)
            else:
                fs = arrow_s3(location.s3)
                con.register(name, ds.dataset(location.path, filesystem=fs, format="parquet"))
            tables.append(name)
        return con, tables

    def read(self, plan: SeriesPlan) -> Any:
        if not plan.scans:
            return _empty(plan)
        con, tables = self._connect(plan)
        try:
            return _conform(con.execute(series_sql(plan, tables)).to_arrow_table(), plan)
        finally:
            con.close()

    def explain(self, plan: SeriesPlan, *, analyze: bool = False) -> str:
        """DuckDB's physical plan as JSON: an operator tree whose scans list their filters."""
        if not plan.scans:
            return ""
        con, tables = self._connect(plan)
        try:
            verb = "EXPLAIN (ANALYZE, FORMAT JSON) " if analyze else "EXPLAIN (FORMAT JSON) "
            rows = con.execute(verb + series_sql(plan, tables)).fetchall()
            return "\n".join(str(row[-1]) for row in rows)
        finally:
            con.close()


class DataFusionReader:
    """Apache DataFusion: local files and S3 objects through its own Parquet reader and object
    store, so row groups and pages are pruned from footers and page indexes."""

    name = "datafusion"

    def _context(self, plan: SeriesPlan) -> tuple[Any, list[str]]:
        from datafusion import SessionContext
        from datafusion.object_store import AmazonS3

        ctx = SessionContext()
        buckets: dict[str, S3Settings] = {}
        tables = []
        for index, scan in enumerate(plan.scans):
            name = f"series_{index}"
            location = scan.file.location
            if location.s3 is not None:
                bucket = str(location.bucket)
                held = buckets.setdefault(bucket, location.s3)
                if held != location.s3:
                    raise LakeRequestError(f"bucket {bucket} is named with two sets of settings")
            tables.append(name)
        for bucket, s3 in buckets.items():
            store = AmazonS3(
                bucket_name=bucket,
                region=s3.region,
                access_key_id=s3.access_key,
                secret_access_key=s3.secret_key,
                session_token=s3.session_token,
                endpoint=s3.endpoint,
                allow_http=s3.allow_http,
            )
            ctx.register_object_store("s3://", store, bucket)
        for scan, name in zip(plan.scans, tables, strict=True):
            ctx.register_parquet(name, scan.file.location.url)
        return ctx, tables

    def read(self, plan: SeriesPlan) -> Any:
        if not plan.scans:
            return _empty(plan)
        ctx, tables = self._context(plan)
        return _conform(ctx.sql(series_sql(plan, tables)).to_arrow_table(), plan)

    def explain(self, plan: SeriesPlan, *, analyze: bool = False) -> str:
        if not plan.scans:
            return ""
        ctx, tables = self._context(plan)
        verb = "EXPLAIN ANALYZE " if analyze else "EXPLAIN "
        rows = ctx.sql(verb + series_sql(plan, tables)).to_arrow_table().to_pylist()
        return "\n".join(f"{row['plan_type']}: {row['plan']}" for row in rows)
