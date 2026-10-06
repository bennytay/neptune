"""Contract versions this package is built against (ADR 0001, ``docs/contracts.md``).

Pins are declared, not discovered: a version bump in a dependency is a change here, reviewed
alongside the code that adapts to it.
"""

from __future__ import annotations

from typing import Final

from neptune.model.record import SCHEMA_VERSION
from neptune_memory.schema import GRAPH_SCHEMA_RELEASE as _GRAPH_SCHEMA_RELEASE
from neptune_memory.schema import GRAPH_SCHEMA_VERSION as _GRAPH_SCHEMA_VERSION

# Compiler package schema: the ``schema_version`` stamped on every canonical record. The compiler
# owns it; this re-export is what Memory declares it was written against.
COMPILER_SCHEMA_VERSION: Final[int] = SCHEMA_VERSION

# Ledger catalog API: pending until MVL-85 lands the API; pinned in the PR that adopts it.
CATALOG_API_VERSION: Final[str] = "pending: pinned when MVL-85 (Ledger catalog API) lands"

# Graph schema Memory itself publishes (contracts/graph-schema/, ADR 0006): the registry major,
# and the full release its graph documents name (ADR 0019 §3).
GRAPH_SCHEMA_VERSION: Final[int] = _GRAPH_SCHEMA_VERSION
GRAPH_SCHEMA_RELEASE: Final[str] = _GRAPH_SCHEMA_RELEASE
