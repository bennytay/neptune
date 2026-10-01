"""Persistence of the bi-temporal claim graph behind the ``MemoryStore`` seam.

Rule: packages are read only through ``neptune_memory.ledger.LedgerReader``; this subpackage never
opens package files and never imports ``neptune.store``. The chosen engine is PostgreSQL 16 +
pgvector, queried with SQL (``postgres.PostgresStore``; an Apache AGE snapshot is optional).
``neo4j.Neo4jStore`` is a stub kept so the store stays swappable (ADR 0004). Database drivers are
never imported here: callers pass a connection.
"""

from neptune_memory.store.neo4j import Neo4jStore
from neptune_memory.store.postgres import PostgresStore
from neptune_memory.store.protocol import MemoryStore
from neptune_memory.store.records import (
    AsOf,
    ClaimEmbedding,
    ClaimRecord,
    Neighbour,
    VectorHit,
)

__all__ = [
    "AsOf",
    "ClaimEmbedding",
    "ClaimRecord",
    "MemoryStore",
    "Neighbour",
    "Neo4jStore",
    "PostgresStore",
    "VectorHit",
]
