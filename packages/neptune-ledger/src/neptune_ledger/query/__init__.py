"""Read-only query planning over the catalog, threads, lineage and the lake (Ledger ADR 0016).

``QueryEngine`` implements the catalog API's ``query(spec)`` and the SQL passthrough over a
scoped answer. ``PostgresCatalog.query`` and ``PostgresCatalog.sql`` delegate to it.
"""

from neptune_ledger.query.budget import QueryLimits
from neptune_ledger.query.engine import QueryEngine

__all__ = ["QueryEngine", "QueryLimits"]
