"""Contract versions this package is built against (ADR 0001, ``docs/contracts.md``).

Pins are declared, not discovered: a bump in an upstream contract is a change here, reviewed
alongside the code that adapts to it. ``contracts/lock.toml`` states the same two versions;
``tests/test_pins_context.py`` keeps code, lock, registry and docs in step.
"""

from typing import Final

# Ledger catalog API (contracts/catalog-api): the registry version Context reads packages through.
CATALOG_API_VERSION: Final = "1.7.0"

# Memory graph schema (contracts/graph-schema): the registry version of the claim graph read.
GRAPH_SCHEMA_VERSION: Final = "1.6.0"
