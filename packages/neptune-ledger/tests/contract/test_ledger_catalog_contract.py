"""The catalog contract suite, run against the stub and against the real catalog.

- ``TestStubCatalog``: every API-calling test is a strict expected failure with
  ``NotImplementedError`` (ADR 0004 §6); any other failure, or a pass, turns CI red.
- ``TestPostgresCatalog``: ``register`` and ``verify`` (MVL-90) must pass. Each test that reaches a
  call not implemented yet is listed in ``PENDING`` with the issue that owns it, and is a strict
  expected failure with ``NotImplementedError``: when that call lands, the test passes, the strict
  xfail turns CI red, and the entry is deleted.
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

RESOLVE: Final = "resolve() is not implemented yet (MVL-91)"
THREAD: Final = "thread(), threads_of() and lineage() are not implemented yet (MVL-92)"
QUERY: Final = "query() is not implemented yet (MVL-98)"

# Contract tests that reach a call the real catalog does not implement yet, by the first such call.
PENDING: Final = {
    "test_a_moved_source_registers_as_another_package": RESOLVE,
    "test_resolve_every_cited_anchor": RESOLVE,
    "test_resolve_unknown_source": RESOLVE,
    "test_same_call_twice_gives_identical_bytes": RESOLVE,
    "test_lineage_of_every_record": THREAD,
    "test_lineage_of_an_unknown_record": THREAD,
    "test_machine_threads_hold_exactly_their_declared_members": THREAD,
    "test_world_order_partitions_by_clock": THREAD,
    "test_two_clocks_across_packages_stay_apart_in_registration_order": THREAD,
    "test_two_clocks_in_one_package_order_by_clock_key_bytes": THREAD,
    "test_current_view_is_within_history": THREAD,
    "test_latest_transform_resolves_to_the_dominant_version": THREAD,
    "test_latest_transform_is_ambiguous_between_equal_versions": THREAD,
    "test_pinned_selects_exactly_one_transform": THREAD,
    "test_as_registered_by_follows_one_package": THREAD,
    "test_as_of_beyond_the_catalog_is_refused": THREAD,
    "test_thread_without_a_preference_is_rejected": THREAD,
    "test_unknown_thread_is_empty_not_an_error": THREAD,
    "test_threads_of_reports_every_machine_membership": THREAD,
    "test_as_of_replays_an_earlier_catalog_point": THREAD,
    "test_query_by_kind_returns_catalog_rows": QUERY,
    "test_query_time_window_stays_on_one_clock": QUERY,
    "test_query_pages_by_cursor": QUERY,
    "test_query_rejects_an_inverted_window": QUERY,
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
