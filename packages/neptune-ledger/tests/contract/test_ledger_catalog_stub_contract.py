"""The catalog contract suite against the stub: every API-calling test is a strict expected failure.

Each test must fail with ``NotImplementedError`` from the stub; any other failure, or a pass, turns
CI red (ADR 0004 §6). The real catalog adds its own ``CatalogContract`` subclass without
``expected_failure`` when it lands.
"""

from pathlib import Path

from neptune_ledger.api import CatalogApi, StubCatalog
from neptune_ledger.contract_tests import CatalogContract


class TestStubCatalog(CatalogContract):
    expected_failure = NotImplementedError

    def make_catalog(self, workdir: Path) -> CatalogApi:
        return StubCatalog()
