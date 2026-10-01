"""The Arrow form of a ``query`` result: columns from ``QueryRow``, metadata from ``QueryMeta``.

pyarrow ships no type information, so this module is the only place the API touches it untyped.
"""

import dataclasses
import typing
from typing import Any, Final, Literal, get_origin

import pyarrow as pa

from neptune_ledger.api import codec
from neptune_ledger.api.types import QueryMeta, QueryRow

META_KEY: Final = b"neptune.catalog_api"


def _column(name: str, hint: Any) -> Any:
    base, _ = codec.split_annotated(hint)
    nullable = False
    optional = codec.optional_inner(base)
    if optional is not None:
        base, _ = codec.split_annotated(optional)
        nullable = True
    if base is int:
        arrow_type = pa.int64()
    elif base is str or get_origin(base) is Literal:
        arrow_type = pa.string()
    else:  # pragma: no cover - guarded by the schema test
        raise TypeError(f"QueryRow.{name}: no Arrow type for {base!r}")
    return pa.field(name, arrow_type, nullable=nullable)


def _row_schema() -> Any:
    hints = typing.get_type_hints(QueryRow, include_extras=True)
    return pa.schema([_column(f.name, hints[f.name]) for f in dataclasses.fields(QueryRow)])


# The columns of every query result, in order. Metadata is added per result by ``query_table``.
QUERY_RESULT_SCHEMA: Final = _row_schema()


def query_table(rows: typing.Sequence[QueryRow], meta: QueryMeta) -> Any:
    """Build a ``query`` result. Implementations should use this so results are byte-stable.

    ``rows`` must already be in the contract's order: ``(kind, record_id, package_id)``.
    """
    for row in rows:
        codec.to_json(row)  # enforce the row's constraints
    columns = {f.name: [getattr(row, f.name) for row in rows] for f in dataclasses.fields(QueryRow)}
    schema = QUERY_RESULT_SCHEMA.with_metadata({META_KEY: codec.dumps(meta)})
    return pa.Table.from_pydict(columns, schema=schema)


def query_meta(table: Any) -> QueryMeta:
    """The ``as_of`` point and findings a ``query`` result carries in its schema metadata."""
    metadata = table.schema.metadata or {}
    if META_KEY not in metadata:
        raise codec.CodecError("not a catalog query result: no neptune.catalog_api metadata")
    return codec.loads(QueryMeta, metadata[META_KEY])


def query_rows(table: Any) -> tuple[QueryRow, ...]:
    """The rows of a ``query`` result as records; checks the columns are exactly the contract's."""
    if not table.schema.remove_metadata().equals(QUERY_RESULT_SCHEMA):
        raise codec.CodecError("query result columns differ from QUERY_RESULT_SCHEMA")
    out = []
    for row in table.to_pylist():
        present = {k: v for k, v in row.items() if v is not None}
        out.append(codec.from_json(QueryRow, present))
    return tuple(out)


def ipc_bytes(table: Any) -> bytes:
    """The Arrow IPC stream bytes of a table: what "same call, identical bytes" compares."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


__all__ = ["QUERY_RESULT_SCHEMA", "ipc_bytes", "query_meta", "query_rows", "query_table"]
