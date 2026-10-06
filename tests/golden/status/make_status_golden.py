"""The golden packages of the status fixtures (ADR 0071): one per embodiment, each the records its
adapter writes from one log, as a records-only package.

``python tests/golden/status/make_status_golden.py`` rewrites every ``<fixture stem>/``
directory here from ``tests/fixtures/status/``; ``tests/integration/test_status_golden.py``
checks the committed files are exactly what the adapters give today. Kept: ``manifest.json``
(which hashes every file, the empty record tables included), ``receipt.json``, ``receipt.md`` and
every ``records/<kind>.jsonl`` that holds a record. A changed file here is a changed output of an
adapter: explain it in the PR, and bump the adapter's version (ADR 0003).
"""

import io
import shutil
from pathlib import Path
from typing import Final

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.harness import ingest_source
from neptune.discovery.reader import BytesReader
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.source import LocalPath
from neptune.store.package import package_files

HERE: Final = Path(__file__).parent
FIXTURES: Final = HERE.parents[1] / "fixtures" / "status"
# fixture, the adapter that reads it, and the embodiment it stands for
SOURCES: Final = (
    ("arm_cell.mcap", "mcap", "industrial arm"),
    ("mobile_base.bag", "rosbag1", "mobile base"),
    ("av_shuttle.db3", "rosbag2", "autonomous shuttle"),
    ("quad_killswitch.ulg", "flightlog", "multicopter"),
    ("boat_failsafe.bin", "flightlog", "boat"),
)


def packages() -> dict[str, dict[str, bytes]]:
    """Every fixture's whole package, by fixture stem: its files by path in the package."""
    adapters = {adapter.descriptor.id: adapter for adapter in builtin_adapters()}
    found: dict[str, dict[str, bytes]] = {}
    for name, reader, _ in SOURCES:
        data = (FIXTURES / name).read_bytes()
        ledger = SourceLedger()
        artifact = digest_stream(io.BytesIO(data))
        ledger.observe(LocalPath(f"logs/{name}"), artifact)
        output = ingest_source(adapters[reader], BytesReader(data, artifact.content_id))
        records = [*ledger.artifacts(), *ledger.revisions(), *output.package_records()]
        found[Path(name).stem] = package_files(records)
    return found


def build() -> dict[str, bytes]:
    """Every package's kept files, by path relative to this directory."""
    return {
        f"{stem}/{path}": content
        for stem, files in packages().items()
        for path, content in files.items()
        if not path.startswith("records/") or content
    }


if __name__ == "__main__":
    for name, _, _ in SOURCES:
        shutil.rmtree(HERE / Path(name).stem, ignore_errors=True)
    for relative, content in build().items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
