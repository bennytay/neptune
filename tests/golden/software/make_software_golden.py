"""Package one software-identity fixture and keep the package's documents as golden files.

The fixture is the CycloneDX BOM (``tests/fixtures/software/sbom/robot.cdx.json``): seven items
of four kinds, both digest kinds, a firmware release and two identity findings. Run
``uv run python tests/golden/software/make_software_golden.py`` after the adapter's output
changes, and explain the diff in the PR (ADR 0003). ``tests/integration/test_software_job.py``
checks the committed files are exactly what reading the fixture gives.
"""

import io
from pathlib import Path
from typing import Final

from neptune.adapters.harness import ingest_source
from neptune.adapters.software import SoftwareAdapter
from neptune.discovery.reader import BytesReader
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.source import LocalPath
from neptune.store.package import MANIFEST, RECEIPT, RECEIPT_TEXT, package_files

HERE: Final = Path(__file__).parent
FIXTURE: Final = HERE.parents[1] / "fixtures" / "software" / "sbom" / "robot.cdx.json"
LOCATION: Final = "sbom/robot.cdx.json"
KEPT: Final = (
    MANIFEST,
    RECEIPT,
    RECEIPT_TEXT,
    "records/ingest_finding.jsonl",
    "records/software_configuration.jsonl",
)


def build() -> dict[str, bytes]:
    """The golden files, by path relative to this directory."""
    data = FIXTURE.read_bytes()
    ledger = SourceLedger()
    ledger.observe(LocalPath(LOCATION), digest_stream(io.BytesIO(data)))
    output = ingest_source(SoftwareAdapter(), BytesReader(data))
    package = package_files([*ledger.artifacts(), *ledger.revisions(), *output.package_records()])
    return {f"cyclonedx/{name}": package[name] for name in KEPT}


if __name__ == "__main__":
    for relative, content in build().items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
