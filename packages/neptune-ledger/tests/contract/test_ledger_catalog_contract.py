"""The catalog contract suite, run against the stub and against the real catalog.

- ``TestStubCatalog``: every API-calling test is a strict expected failure with
  ``NotImplementedError`` (ADR 0004 §6); any other failure, or a pass, turns CI red.
- ``TestPostgresCatalog``: ``register``, ``verify`` (MVL-90), ``resolve`` (MVL-91), ``thread``,
  ``threads_of`` and ``lineage`` (MVL-92) must pass.
  Each test that reaches a call not implemented yet is listed in ``PENDING`` with the issue that
  owns it, and is a strict expected failure with ``NotImplementedError``: when that call lands,
  the test passes, the strict xfail turns CI red, and the entry is deleted.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Final

import psycopg
import pytest

from conftest import new_database
from neptune_ledger.api import CatalogApi, StubCatalog
from neptune_ledger.catalog.migrate import apply_migrations
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.contract_tests import CatalogContract

QUERY: Final = "query() is not implemented yet (MVL-98)"

# Contract tests that reach a call the real catalog does not implement yet, by the first such call.
# The first three pass every thread(), threads_of() and lineage() call (MVL-92) and then reach
# query(); test_ledger_threads.py repeats their thread and lineage halves without it.
PENDING: Final = {
    "test_same_call_twice_gives_identical_bytes": QUERY,
    "test_as_of_beyond_the_catalog_is_refused": QUERY,
    "test_as_of_replays_an_earlier_catalog_point": QUERY,
    "test_query_by_kind_returns_catalog_rows": QUERY,
    "test_query_time_window_stays_on_one_clock": QUERY,
    "test_query_pages_by_cursor": QUERY,
    "test_query_rejects_an_inverted_window": QUERY,
    "test_query_rejects_a_kind_no_schema_version_declares": QUERY,
}


class TestStubCatalog(CatalogContract):
    expected_failure = NotImplementedError

    def make_catalog(self, workdir: Path) -> CatalogApi:
        return StubCatalog()

    def make_tenant_catalog(self, workdir: Path, package_roots: tuple[Path, ...]) -> CatalogApi:
        return StubCatalog()


class TestPostgresCatalog(CatalogContract):
    """The real catalog, one fresh C-collated database and tenant schema per catalog."""

    _server: str
    _open: list[PostgresCatalog]

    @pytest.fixture(autouse=True)
    def _postgres(self, request: pytest.FixtureRequest, pg_server: str) -> Iterator[None]:
        reason = PENDING.get(request.function.__name__)
        if reason is not None:
            request.applymarker(
                pytest.mark.xfail(raises=NotImplementedError, strict=True, reason=reason)
            )
        self._server, self._open = pg_server, []
        yield
        for catalog in self._open:
            catalog.close()

    def _fresh(self, package_roots: tuple[Path, ...] | None) -> PostgresCatalog:
        uri = new_database(self._server)
        with psycopg.connect(uri, autocommit=True) as conn:
            apply_migrations(conn, "acme")
        catalog = PostgresCatalog(uri, "acme", package_roots=package_roots)
        self._open.append(catalog)
        return catalog

    def make_catalog(self, workdir: Path) -> CatalogApi:
        """A fresh catalog with no package-root limit: every test directory is accepted."""
        return self._fresh(None)

    def make_tenant_catalog(self, workdir: Path, package_roots: tuple[Path, ...]) -> CatalogApi:
        return self._fresh(package_roots)
