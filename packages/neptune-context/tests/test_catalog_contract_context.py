"""The Ledger's catalog contract suite, run from Context against the Ledger's stub (ADR 0001 §4).

Context codes against ``CatalogApi``. Until it has a real catalog to read, the suite runs against
``StubCatalog`` as strict expected failures: each API-calling test must fail with
``NotImplementedError``, and a pass turns CI red. When Context gains a real or in-memory catalog
client, one more subclass of ``CatalogContract`` runs the same suite with ``expected_failure``
unset.
"""

from pathlib import Path

from neptune_ledger.api import CatalogApi, StubCatalog
from neptune_ledger.contract_tests import CatalogContract


class TestStubCatalogFromContext(CatalogContract):
    expected_failure = NotImplementedError

    def make_catalog(self, workdir: Path) -> CatalogApi:
        return StubCatalog()

    def make_tenant_catalog(self, workdir: Path, package_roots: tuple[Path, ...]) -> CatalogApi:
        return StubCatalog()
