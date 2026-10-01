"""Ingest ``tests/fixtures/mcap/robot.mcap`` alone and keep its package's documents as golden files.

Run ``uv run python tests/golden/mcap/make_mcap_golden.py`` after a change that is meant to change
the MCAP adapter's output (and say why in the PR: a golden diff is a compatibility event).
``tests/integration/test_mcap_job.py`` checks that a sandboxed job writes exactly these files.

Kept: ``manifest.json`` (which hashes every file, the Parquet series and the empty record tables
included), ``receipt.json``, ``receipt.md`` and every ``records/<kind>.jsonl`` that holds a
record. The other files are pinned through the manifest's hashes.
"""

import shutil
import tempfile
from pathlib import Path
from typing import Final

from neptune.adapters.builtin import default_registry
from neptune.runtime import IngestJob, JobOptions, JobState
from neptune.store.workspace import Workspace

HERE: Final = Path(__file__).parent
SOURCE: Final = HERE.parents[1] / "fixtures" / "mcap" / "robot.mcap"
NAME: Final = "robot.mcap"


def package_documents(package: Path) -> dict[str, bytes]:
    """The package's golden files, by path relative to the package."""
    kept = [package / "manifest.json", package / "receipt.json", package / "receipt.md"]
    kept += sorted(p for p in (package / "records").glob("*.jsonl") if p.stat().st_size)
    return {path.relative_to(package).as_posix(): path.read_bytes() for path in kept}


def ingest(work: Path, options: JobOptions | None = None) -> Path:
    """A package of the fixture alone, built by a fresh job in ``work``."""
    root = work / "root"
    root.mkdir()
    shutil.copyfile(SOURCE, root / NAME)
    destination = work / "package"
    job = IngestJob(
        root, destination, Workspace(work / "home"), default_registry(), options or JobOptions()
    )
    outcome = job.run()
    assert outcome.state is JobState.COMMITTED
    return destination


def build() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as work:
        return package_documents(ingest(Path(work)))


if __name__ == "__main__":
    for path in HERE.glob("*/"):
        if path.is_dir():
            shutil.rmtree(path)
    for relative, data in build().items():
        target = HERE / "package" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
