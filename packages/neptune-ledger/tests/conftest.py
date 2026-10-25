"""A real PostgreSQL 16 for the catalog tests, from the ``pgserver`` wheel: no service needed.

One server per test session; each test that asks for ``pg`` gets a fresh, empty database.
"""

import itertools
import os
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


def new_database(server_uri: str) -> str:
    """A new, empty, C-collated database on the server; returns its URI."""
    name = f"catalog_{next(_names)}"
    with psycopg.connect(server_uri, autocommit=True) as admin:
        # The catalog requires byte-order collation (ADR 0005 §4), whatever the server's default.
        admin.execute(
            sql.SQL(
                "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
            ).format(sql.Identifier(name))
        )
    uri = server_uri.replace("/postgres?", f"/{name}?", 1)
    assert name in uri
    return uri


@pytest.fixture
def pg_uri(pg_server: str) -> str:
    """The URI of a new, empty, C-collated database on the session's server."""
    return new_database(pg_server)


@pytest.fixture
def pg(pg_uri: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """An autocommit connection to a new, empty, C-collated database on the session's server."""
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        yield conn


# In CI, an unmarked test slower than SLOW_BUDGET_S fails: mark it `slow` (the nightly runs it).
SLOW_BUDGET_S = 180.0


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()  # type: ignore[attr-defined]
    if (
        os.environ.get("CI")
        and report.when == "call"
        and report.passed
        and "slow" not in item.keywords
        and report.duration > SLOW_BUDGET_S
    ):
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid} took {report.duration:.0f} s, over the {SLOW_BUDGET_S:.0f} s budget "
            "for an unmarked test: mark it @pytest.mark.slow"
        )
