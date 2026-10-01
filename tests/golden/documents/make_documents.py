"""Ingest the SOP as a PDF and as Markdown through a real job, and keep the package as golden files.

Run ``uv run python tests/golden/documents/make_documents.py`` after the ``pdf`` or ``markdown``
adapter's output changes on purpose, and explain the diff in the PR (ADR 0003).
``tests/integration/test_document_ingest.py`` checks the committed files are exactly what a job
writes today. The package is kept whole but for its empty record tables, which the manifest lists
with their hashes, and its volatile envelope.
"""

import shutil
import tempfile
from pathlib import Path
from typing import Final

from neptune.adapters.builtin import default_registry
from neptune.runtime import IngestJob, Isolation, JobOptions, JobState
from neptune.store.workspace import Workspace

HERE: Final = Path(__file__).parent
FIXTURES: Final = HERE.parents[1] / "fixtures"
SOURCES: Final = (FIXTURES / "pdf" / "pump_sop.pdf", FIXTURES / "markdown" / "pump_sop.md")
PACKAGE: Final = "pump_sop"


def build() -> dict[str, bytes]:
    """The golden files, by path relative to this directory."""
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        root = work / "site"
        root.mkdir()
        for source in SOURCES:
            shutil.copy(source, root / source.name)
        options = JobOptions(isolation=Isolation.IN_PROCESS, job="golden")
        home = Workspace(work / "home")
        job = IngestJob(root, work / "package", home, default_registry(), options)
        outcome = job.run()
        if outcome.state is not JobState.COMMITTED:
            raise RuntimeError(f"the golden job ended {outcome.state}")
        files: dict[str, bytes] = {}
        for path in sorted((work / "package").rglob("*")):
            relative = path.relative_to(work / "package").as_posix()
            if path.is_file() and not relative.startswith("volatile/") and path.stat().st_size:
                files[f"{PACKAGE}/{relative}"] = path.read_bytes()
        return files


if __name__ == "__main__":
    shutil.rmtree(HERE / PACKAGE, ignore_errors=True)
    for relative, data in build().items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
