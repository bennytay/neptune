"""The ``query-packet`` contract's owner module (``contracts/query-packet``, status ``planned``).

What other packages may rely on from Context. Today that is the query language (ADR 0002): its
version, its canonical bytes and id, its reader and its JSON Schema. The context packet joins it
here when it lands; the registry publishes the contract's first version then (ADR 0002 §9).
"""

from neptune_context.query import QUERY_ID_PATTERN, QUERY_VERSION, Query, canonical_bytes, query_id
from neptune_context.query.decode import loads
from neptune_context.query.schema import query_schema

__all__ = [
    "QUERY_ID_PATTERN",
    "QUERY_VERSION",
    "Query",
    "canonical_bytes",
    "loads",
    "query_id",
    "query_schema",
]
