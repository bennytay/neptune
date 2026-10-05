"""The series stage: the window's series rows of the selected streams, joined to their record rows
in Arrow (Ledger ADR 0016 §4).

The lake reads each stream's file in place on the window's clock, with the window and the row
budget pushed down (ADR 0013). Each series row then gets its stream's record columns: strings as
dictionaries over the stream rows, since they repeat on every row of a stream, and integers
taken per row. Rows stay in the lake's order, which on one clock is ``(ticks, package_id,
stream_id, seq)``: total, so the join is deterministic.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
import pyarrow as pa
import pyarrow.compute as pc

from neptune_ledger.api.protocol import CatalogUnavailable
from neptune_ledger.api.types import CatalogFinding, PlanStep, TimeWindow
from neptune_ledger.lake.read import (
    LakeRequestError,
    ScanInterrupted,
    SeriesReader,
    plan_series,
    series_sql,
)
from neptune_ledger.lake.series import SeriesCatalog
from neptune_ledger.query.budget import Budget


@dataclass
class SeriesAnswer:
    """The joined rows (None when the scan was stopped at the deadline), why any selected
    stream is not in them, the steps run and whether the request was outside the lake's rules."""

    table: Any
    findings: list[CatalogFinding]
    steps: list[PlanStep]
    refused: bool = False


def read_series(
    streams: Any,
    window: TimeWindow,
    columns: Sequence[str] | None,
    catalog: SeriesCatalog,
    reader: SeriesReader,
    budget: Budget,
) -> SeriesAnswer:
    """The series rows of the stream rows in ``streams`` (a record table) within ``window``."""
    pairs = list(
        zip(
            streams.column("package_id").to_pylist(),
            streams.column("record_id").to_pylist(),
            strict=True,
        )
    )
    try:
        selection = catalog.files(pairs)
    except psycopg.OperationalError as exc:
        catalog.close()
        raise CatalogUnavailable(f"the catalog store is unreachable: {exc}") from exc
    except psycopg.Error as exc:
        raise CatalogUnavailable(f"the catalog store refused the call: {exc}") from exc
    findings = list(selection.findings)
    try:
        plan = plan_series(
            selection.files, windows=[window], columns=columns, limit=budget.max_rows + 1
        )
    except LakeRequestError as exc:
        refusal = CatalogFinding("invalid_request", "series", str(exc).splitlines()[0][:300])
        return SeriesAnswer(None, [refusal], [], refused=True)
    findings += plan.findings
    tables = [f"series_{i}" for i in range(len(plan.scans))]
    steps = [
        PlanStep(
            "arrow",
            "series.files",
            f"{len(plan.scans)} series files on the window's clock, from the selected streams'"
            " registered packages (ADR 0013 §4); window and row budget pushed into each scan",
        ),
        PlanStep(
            "datafusion" if reader.name == "datafusion" else "duckdb",
            "series.scan",
            series_sql(plan, tables) if plan.scans else "no file to scan",
        ),
    ]
    if budget.out_of_time():
        return SeriesAnswer(None, findings, steps)
    try:
        read = reader.read(plan, timeout=budget.remaining())
    except ScanInterrupted:
        budget.time_ran_out()
        return SeriesAnswer(None, findings, steps)
    except LakeRequestError as exc:  # an engine's own limits, e.g. DuckDB's case-folded names
        refusal = CatalogFinding("invalid_request", "series", str(exc).splitlines()[0][:300])
        return SeriesAnswer(None, [refusal], [], refused=True)
    findings += read.findings[len(plan.findings) :]
    steps.append(
        PlanStep(
            "arrow",
            "join",
            "each series row takes its stream's record columns, on (package_id, stream_id) ="
            " (package_id, record_id); string columns dictionary-encoded",
        )
    )
    return SeriesAnswer(_join(read.table, streams), findings, steps)


def _join(series: Any, streams: Any) -> Any:
    """``series`` rows with the columns of their stream's row in ``streams`` first."""
    index = {
        key: i
        for i, key in enumerate(
            zip(
                streams.column("package_id").to_pylist(),
                streams.column("record_id").to_pylist(),
                strict=True,
            )
        )
    }
    packages = series.column("package_id").combine_chunks()
    stream_ids = series.column("stream_id").combine_chunks()
    width = max(1, len(stream_ids.dictionary))
    combo = pc.add(
        pc.multiply(packages.indices.cast(pa.int64()), width), stream_ids.indices.cast(pa.int64())
    )
    unique = pc.unique(combo)
    lookup = pa.array(
        [
            index[
                (
                    packages.dictionary[value // width].as_py(),
                    stream_ids.dictionary[value % width].as_py(),
                )
            ]
            for value in unique.to_pylist()
        ],
        pa.int32(),
    )
    rows = pc.take(lookup, pc.index_in(combo, value_set=unique))
    arrays, fields = [], []
    for field in streams.schema:
        values = streams.column(field.name).combine_chunks()
        if pa.types.is_string(field.type):
            present = pc.take(pc.is_valid(values), rows)
            indices = pc.if_else(present, rows, pa.scalar(None, pa.int32()))
            array = pa.DictionaryArray.from_arrays(indices, pc.fill_null(values, ""))
            fields.append(pa.field(field.name, array.type, nullable=field.nullable))
        else:
            array = pc.take(values, rows)
            fields.append(pa.field(field.name, field.type, nullable=field.nullable))
        arrays.append(array)
    for field in series.schema:
        if field.name in ("package_id", "stream_id"):
            continue
        arrays.append(series.column(field.name).combine_chunks())
        fields.append(field)
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))
