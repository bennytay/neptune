"""Ingest the assertion fixtures with the assertion adapter and keep their packages as golden files.

Three packages (ADR 0062): an identity confirmation between two robots of a warehouse fleet (and
the distinct-identity assertion beside it), a baseline acceptance for a manipulator cell (and an
earlier rejection), and a retraction of the fleet confirmation (with an annotation). Run
``make examples`` (or ``uv run python tests/golden/assertion/make_assertion_golden.py``) after a
change to the assertion adapter's output, and explain the diff in the PR (ADR 0003): a changed
golden file is a compatibility change, and one the adapter's version must say. The package's
documents and every non-empty record table are kept; ``tests/integration/test_assertion_golden.py``
checks they are exactly what ingesting the fixtures gives.
"""

import io
from pathlib import Path
from typing import Final

from neptune.adapters.assertion import AssertionAdapter
from neptune.adapters.harness import ingest_source
from neptune.discovery.reader import BytesReader
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.source import LocalPath
from neptune.store.package import package_files

HERE: Final = Path(__file__).parent
FIXTURES: Final = HERE.parents[1] / "fixtures" / "assertion"
NAMES: Final = ("cell_baseline", "fleet_identity", "retraction")


def package(name: str) -> dict[str, bytes]:
    """One fixture's golden files, by path relative to its package directory."""
    data = (FIXTURES / f"{name}.json").read_bytes()
    ledger = SourceLedger()
    ledger.observe(LocalPath(f"ops/assertions/{name}.json"), digest_stream(io.BytesIO(data)))
    output = ingest_source(AssertionAdapter(), BytesReader(data))
    records = [*ledger.artifacts(), *ledger.revisions(), *output.package_records()]
    return {path: content for path, content in package_files(records).items() if content}


def build() -> dict[str, bytes]:
    """Every golden file, by path relative to this directory."""
    return {f"{name}/{path}": content for name in NAMES for path, content in package(name).items()}


if __name__ == "__main__":
    for relative, content in build().items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
