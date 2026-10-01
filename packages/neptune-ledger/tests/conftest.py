"""A real PostgreSQL 16 for the catalog tests, from the ``pgserver`` wheel: no service needed.

One server per test session; each test that asks for ``pg`` gets a fresh, empty database.
"""

import itertools
from collections.abc import Iterator

import psycopg
import pytest
from pgserver.postgres_server import get_server
from psycopg import sql

_names = itertools.count()


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    server = get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="stop")
    try:
        yield str(server.get_uri())
    finally:
        server.cleanup()


@pytest.fixture
def pg(pg_server: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """An autocommit connection to a new, empty, C-collated database on the session's server."""
    name = f"catalog_{next(_names)}"
    with psycopg.connect(pg_server, autocommit=True) as admin:
        # The catalog requires byte-order collation (ADR 0005 §4), whatever the server's default.
        admin.execute(
            sql.SQL(
                "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
            ).format(sql.Identifier(name))
        )
    uri = pg_server.replace("/postgres?", f"/{name}?", 1)
    assert name in uri
    with psycopg.connect(uri, autocommit=True) as conn:
        yield conn
