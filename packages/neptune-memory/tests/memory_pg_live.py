"""The live-PostgreSQL fixture shared by the store tests and the G1 stress tests.

Live tests run only when ``NEPTUNE_MEMORY_PG_DSN`` names a PostgreSQL with pgvector and psycopg
imports; otherwise they skip. Apache AGE is optional (ADR 0004 Decision 3): teardown drops the
snapshot graph only when the extension is installed.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

from neptune_memory.store import PostgresStore

if TYPE_CHECKING:
    from collections.abc import Iterator

SCHEMA = "memory_test"
GRAPH = "memory_test_graph"


def has_age(conn: Any) -> bool:
    """Whether Apache AGE is installed in the connected database (ends its own transaction)."""
    row = conn.execute("SELECT count(*) FROM pg_extension WHERE extname = 'age'").fetchone()
    conn.rollback()
    return bool(row[0])


def open_live() -> Iterator[tuple[Any, PostgresStore]]:
    """The body of a ``live`` fixture: a fresh ``memory_test`` schema, dropped afterwards."""
    dsn = os.environ.get("NEPTUNE_MEMORY_PG_DSN")
    if not dsn:
        pytest.skip("NEPTUNE_MEMORY_PG_DSN not set (no PostgreSQL with pgvector)")
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(dsn) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.commit()
        store = PostgresStore(conn, schema=SCHEMA, dimensions=3)
        store.create()
        yield conn, store
        conn.rollback()
        conn.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
        conn.commit()
        if has_age(conn):
            conn.execute(
                "SELECT ag_catalog.drop_graph(name, true) FROM ag_catalog.ag_graph "
                "WHERE name = %(graph)s",
                {"graph": GRAPH},
            )
            conn.commit()
