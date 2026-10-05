"""``QueryEngine``: ``query(spec)`` over the catalog and the lake, and SQL over its answer.

The stages (Ledger ADR 0016 §2): candidate and filter the record rows in PostgreSQL, resolve a
lineage preference over whole lineage sets, scan the selected streams' series in DuckDB or
DataFusion, and join, project and budget in Arrow. Every call runs at one catalog point, which
the result returns. A spec outside the contract is refused with findings, never an exception;
the only exception is ``CatalogUnavailable``.
"""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Final

import psycopg
import pyarrow as pa
from psycopg import sql

from neptune.model.knowledge import Knowledge
from neptune_ledger.api import codec
from neptune_ledger.api.arrow import META_KEY, QUERY_RESULT_SCHEMA
from neptune_ledger.api.protocol import CatalogUnavailable
from neptune_ledger.api.types import (
    CatalogFinding,
    PlanStep,
    QueryMeta,
    QuerySpec,
    TransactionKey,
)
from neptune_ledger.catalog.migrate import tenant_schema
from neptune_ledger.lake.indexes import catalog_point
from neptune_ledger.lake.read import DuckDBReader, SeriesReader
from neptune_ledger.lake.series import Locate, SeriesCatalog
from neptune_ledger.lake.store import local_store
from neptune_ledger.query import spec as specs
from neptune_ledger.query.budget import Budget, Clock, QueryLimits, effective
from neptune_ledger.query.join import read_series
from neptune_ledger.query.plan import plan_records
from neptune_ledger.query.records import Lineage, read_records
from neptune_ledger.query.sql import run_sql

Conn = psycopg.Connection[tuple[Any, ...]]
STREAM_BATCH: Final = 65_536


class QueryEngine:
    """Read-only queries over one tenant's catalog and the packages' series files."""

    def __init__(
        self,
        conninfo: str,
        tenant_id: str,
        *,
        locate: Locate = local_store,
        reader: SeriesReader | None = None,
        limits: QueryLimits | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self._conninfo = conninfo
        self._tenant = tenant_id
        self._schema = tenant_schema(tenant_id)
        self._locate = locate
        self._reader = reader or DuckDBReader()
        self.limits = limits or QueryLimits()
        self._clock = clock
        self._conn: Conn | None = None
        self._series: SeriesCatalog | None = None

    def __enter__(self) -> "QueryEngine":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._series is not None:
            self._series.close()
            self._series = None

    # --- query -----------------------------------------------------------------------------------

    def query(self, spec: QuerySpec) -> Any:
        """The answer to ``spec`` as a ``pyarrow.Table`` with ``QueryMeta`` in its metadata."""
        budget = Budget(effective(_budget(spec), self.limits), self._clock)
        problems = specs.problems(spec, self.limits)
        with self._read() as conn:
            point, as_of, beyond = catalog_point(
                conn, self._tenant, None if problems else spec.as_of
            )
            if problems:
                return _rejected(point, problems)
            if beyond:
                detail = "as_of is beyond the latest committed catalog point"
                return _rejected(
                    point, (CatalogFinding("as_of_out_of_range", str(spec.as_of), detail),)
                )
            plan = plan_records(spec, self._tenant, as_of)
            steps = [plan.step()]
            lineage = None
            if spec.lineage is not None:
                lineage = Lineage(conn, self._tenant, spec.lineage, spec.thread_id, as_of)
                steps.append(
                    PlanStep(
                        "postgres",
                        "lineage",
                        f"resolve each lineage set the rows touch under {spec.lineage.tag},"
                        " over all its members at the catalog point"
                        + (" in the thread" if spec.thread_id else " (record_by_evidence)")
                        + "; keep the rows of each Known set's transform (ADR 0016 §3)",
                    )
                )
            if spec.series is not None:
                want = self.limits.max_streams + 1
            else:
                want = min(spec.limit or budget.max_rows + 1, budget.max_rows + 1)
            after = None
            if spec.after is not None:
                after = (spec.after.kind, spec.after.record_id, spec.after.package_id)
            read = read_records(
                conn,
                plan,
                want=want,
                after=after,
                budget=budget,
                batch_rows=self.limits.batch_rows,
                lineage=lineage,
            )
        findings = list(read.findings)
        records = _record_table(read.rows)
        if spec.series is not None:
            if records.num_rows > self.limits.max_streams:
                detail = (
                    f"more than {self.limits.max_streams} streams match; a series join reads at"
                    " most that many: narrow the spec (ADR 0016 §4)"
                )
                return _rejected(point, (CatalogFinding("invalid_request", "series", detail),))
            assert spec.window is not None  # checked by specs.problems
            answer = read_series(
                records,
                spec.window,
                spec.series.columns,
                self._series_catalog(),
                self._reader,
                budget,
            )
            if answer.refused:
                return _rejected(point, tuple(answer.findings))
            findings += answer.findings
            steps += answer.steps
            if answer.table is None or "time" in budget.exceeded:
                # A stream selection or a scan stopped by the deadline gives no series row:
                # the empty prefix of the answer, never a subset of its streams.
                table = _project(_empty_join(records), spec)
            else:
                table = _project(answer.table, spec)
        else:
            table = _project(records, spec)
        steps.append(_budget_step(budget))
        table = budget.cut(table)
        meta = QueryMeta(
            as_of=point,
            findings=(*findings, *budget.findings(table)),
            budget=budget.report(table),
            plan=tuple(steps) if spec.explain else None,
        )
        return _with_meta(table, meta)

    def stream(self, spec: QuerySpec, batch_rows: int = STREAM_BATCH) -> Any:
        """The same answer as ``query`` as a ``pyarrow.RecordBatchReader`` of ``batch_rows``
        rows per batch; the schema carries the same metadata. The answer is computed under its
        budget first (ADR 0016 §5)."""
        table = self.query(spec)
        batches = table.to_batches(max_chunksize=batch_rows)
        return pa.RecordBatchReader.from_batches(table.schema, batches)

    def explain(self, spec: QuerySpec, *, analyze: bool = False) -> str:
        """PostgreSQL's own plan of the record statement, as JSON text, for operators: costs and
        timings vary between runs, so this is not part of the contract (ADR 0016 §2)."""
        problems = specs.problems(spec, self.limits)
        if problems:
            return json.dumps([codec.to_json(p) for p in problems])
        with self._read() as conn:
            _, as_of, _ = catalog_point(conn, self._tenant, spec.as_of)
            plan = plan_records(spec, self._tenant, as_of)
            params = {
                **plan.params,
                "after_kind": "",
                "after_record": "",
                "after_package": "",
                "batch": self.limits.batch_rows,
            }
            verb = "EXPLAIN (ANALYZE, FORMAT JSON) " if analyze else "EXPLAIN (FORMAT JSON) "
            row = conn.execute(verb + plan.statement, params).fetchone()
        return json.dumps(row[0] if row else None)

    # --- SQL passthrough -------------------------------------------------------------------------

    def sql(self, statement: str, scope: QuerySpec) -> Any:
        """``statement`` (one SELECT) over the views of ``scope``'s answer (ADR 0016 §7).

        ``records`` holds the scope's record rows, and with ``scope.series`` the view ``series``
        holds its joined series rows instead. The statement runs in a sealed, per-call DuckDB
        under the scope's budget, with the passthrough's default time limit."""
        answer = self.query(scope)
        meta = codec.loads(QueryMeta, answer.schema.metadata[META_KEY])
        if meta.budget is None:  # the scope was refused: so is the statement
            return answer
        views = {
            "series" if scope.series is not None else "records": answer.replace_schema_metadata()
        }
        budget = Budget(effective(_budget(scope), self.limits, sql=True), self._clock)
        table, findings = run_sql(statement, views, budget, self.limits.sql_memory)
        if table is None:
            return _rejected(meta.as_of, (*meta.findings, *findings))
        table = budget.cut(table)
        report = budget.report(table)
        # A statement over a scope the deadline cut is no more reproducible than the scope.
        report = replace(report, reproducible=report.reproducible and meta.budget.reproducible)
        out = QueryMeta(
            as_of=meta.as_of,
            findings=(*meta.findings, *findings, *budget.findings(table)),
            budget=report,
        )
        return _with_meta(table, out)

    # --- connections -----------------------------------------------------------------------------

    def _series_catalog(self) -> SeriesCatalog:
        if self._series is None:
            self._series = SeriesCatalog(self._conninfo, self._tenant, locate=self._locate)
        return self._series

    def _connection(self) -> Conn:
        if self._conn is None or self._conn.closed:
            conn: Conn = psycopg.connect(self._conninfo, autocommit=True)
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self._schema)))
            self._conn = conn
        return self._conn

    @contextmanager
    def _read(self) -> Iterator[Conn]:
        """One read-only transaction; a store failure is ``CatalogUnavailable``."""
        try:
            conn = self._connection()
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                yield conn
        except psycopg.OperationalError as exc:
            self.close()
            raise CatalogUnavailable(f"the catalog store is unreachable: {exc}") from exc
        except psycopg.Error as exc:
            raise CatalogUnavailable(f"the catalog store refused the call: {exc}") from exc


def _budget(spec: object) -> Any:
    return spec.budget if isinstance(spec, QuerySpec) and _valid_budget(spec) else None


def _valid_budget(spec: QuerySpec) -> bool:
    try:
        if spec.budget is not None:
            codec.to_json(spec.budget)
    except (codec.CodecError, TypeError, ValueError):
        return False
    return True


def _record_table(rows: list[tuple[Any, ...]]) -> Any:
    """The record rows as a ``QUERY_RESULT_SCHEMA`` table, one chunk."""
    columns = list(zip(*rows, strict=True)) if rows else [()] * len(QUERY_RESULT_SCHEMA)
    arrays = [
        pa.array(list(values), type=field.type)
        for values, field in zip(columns, QUERY_RESULT_SCHEMA, strict=True)
    ]
    return pa.Table.from_arrays(arrays, schema=QUERY_RESULT_SCHEMA)


def _project(table: Any, spec: QuerySpec) -> Any:
    """Drop the record columns the spec's projection leaves out; keys and series stay."""
    if spec.columns is None:
        return table
    keep = {"kind", "record_id", "package_id", *spec.columns}
    record = set(QUERY_RESULT_SCHEMA.names)
    return table.select([n for n in table.schema.names if n in keep or n not in record])


def _empty_join(records: Any) -> Any:
    """A join with no rows: the record columns, as dictionaries for strings, then the lake's
    fixed series columns (no value column: no file was read to name one)."""
    ids = pa.dictionary(pa.int32(), pa.string())
    fields = [
        pa.field(f.name, ids if pa.types.is_string(f.type) else f.type, nullable=f.nullable)
        for f in records.schema
    ]
    fields += [
        pa.field("clock", ids, nullable=False),
        pa.field("ticks", pa.int64()),
        pa.field("ticks_state", ids, nullable=False),
        pa.field("seq", pa.int64(), nullable=False),
    ]
    return pa.schema(fields).empty_table()


def _budget_step(budget: Budget) -> PlanStep:
    limits = budget.limits
    millis = "none" if limits.max_millis is None else f"{limits.max_millis} ms"
    return PlanStep(
        "arrow",
        "budget",
        f"rows <= {limits.max_rows}, bytes <= {limits.max_bytes}, time {millis}: the longest"
        " prefix that fits, as one chunk (ADR 0016 §6)",
    )


def _rejected(point: Knowledge[TransactionKey], findings: tuple[CatalogFinding, ...]) -> Any:
    return _with_meta(QUERY_RESULT_SCHEMA.empty_table(), QueryMeta(point, findings))


def _with_meta(table: Any, meta: QueryMeta) -> Any:
    return table.replace_schema_metadata({META_KEY: codec.dumps(meta)})
