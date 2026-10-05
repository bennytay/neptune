"""The catalog contract suite, run against the stub and against the real catalog.

- ``TestStubCatalog``: every API-calling test is a strict expected failure with
  ``NotImplementedError`` (ADR 0004 §6); any other failure, or a pass, turns CI red.
- ``TestPostgresCatalog``: every call (``register`` and ``verify``, MVL-90; ``resolve``,
  MVL-91; ``thread``, ``threads_of`` and ``lineage``, MVL-92; ``query``, MVL-98) must pass.
"""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from conftest import new_database
from neptune_ledger.api import CatalogApi, StubCatalog
from neptune_ledger.catalog.migrate import apply_migrations
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.contract_tests import CatalogContract


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
    def _postgres(self, pg_server: str) -> Iterator[None]:
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
