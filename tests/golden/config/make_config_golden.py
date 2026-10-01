"""Ingest the Nav2 parameters fixture with the config adapter and keep its package as golden files.

Run ``make examples`` (or ``uv run python tests/golden/config/make_config_golden.py``) after a
change to the config adapter's output, and explain the diff in the PR (ADR 0003): a changed golden
file is a compatibility change, and one the adapter's version must say. The package's documents
and every non-empty record table are kept; ``tests/integration/test_config_golden.py`` checks they
are exactly what ingesting the fixture gives.
"""

import io
from pathlib import Path
from typing import Final

from neptune.adapters.config import ConfigAdapter
from neptune.adapters.harness import ingest_source
from neptune.discovery.reader import BytesReader
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.source import LocalPath
from neptune.store.package import package_files

HERE: Final = Path(__file__).parent
FIXTURE: Final = HERE.parents[1] / "fixtures" / "config" / "nav2_params.yaml"
NAME: Final = "nav2_params"


def build() -> dict[str, bytes]:
    """The golden files, by path relative to this directory: documents and non-empty tables."""
    data = FIXTURE.read_bytes()
    ledger = SourceLedger()
    ledger.observe(LocalPath(f"bringup/params/{FIXTURE.name}"), digest_stream(io.BytesIO(data)))
    output = ingest_source(ConfigAdapter(), BytesReader(data))
    records = [*ledger.artifacts(), *ledger.revisions(), *output.package_records()]
    return {
        f"{NAME}/{path}": content for path, content in package_files(records).items() if content
    }


if __name__ == "__main__":
    for relative, content in build().items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
