"""Contract tests for the Ledger catalog API, runnable against any implementation.

``CatalogContract`` (``neptune_ledger.contract_tests.suite``) holds golden calls over the
compiler's four worked-example packages, the error cases (unknown package, tampered manifest and
record table, unresolvable evidence, missing preference) and determinism (the same call twice
gives identical bytes). A downstream package subclasses it; see the suite's docstring and
``docs/catalog-api.md``. Needs pytest and jsonschema; the worked examples are found in this
repository or at ``$NEPTUNE_WORKED_EXAMPLES``.
"""

from neptune_ledger.contract_tests.suite import CatalogContract

__all__ = ["CatalogContract"]
