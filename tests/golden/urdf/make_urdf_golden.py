"""The golden package of one robot description: the diff-drive Xacro, ingested by ``urdf``.

``python tests/golden/urdf/make_urdf_golden.py`` rewrites ``diff_drive_xacro/`` from the fixture
``tests/fixtures/urdf/xacro/diff_drive.urdf.xacro``; ``tests/integration/test_urdf_golden.py``
checks the committed files are exactly what the adapter gives today. A changed file here is a
changed output of the adapter: explain it in the PR, and bump the adapter's version (ADR 0003).
"""

import io
from pathlib import Path
from typing import Final

from neptune.adapters.harness import ingest_source
from neptune.adapters.urdf import UrdfAdapter
from neptune.discovery.reader import BytesReader
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.source import LocalPath
from neptune.store.package import package_files

HERE: Final = Path(__file__).parent
FIXTURE: Final = HERE.parents[1] / "fixtures" / "urdf" / "xacro" / "diff_drive.urdf.xacro"
NAME: Final = "diff_drive_xacro"


def build() -> dict[str, bytes]:
    """The package's files, by path relative to this directory."""
    data = FIXTURE.read_bytes()
    ledger = SourceLedger()
    artifact = digest_stream(io.BytesIO(data))
    ledger.observe(LocalPath("urdf/diff_drive.urdf.xacro"), artifact)
    output = ingest_source(UrdfAdapter(), BytesReader(data, artifact.content_id))
    records = [*ledger.artifacts(), *ledger.revisions(), *output.package_records()]
    return {f"{NAME}/{path}": content for path, content in package_files(records).items()}


if __name__ == "__main__":
    for relative, content in build().items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
