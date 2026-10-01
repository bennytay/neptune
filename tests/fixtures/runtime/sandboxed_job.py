"""Run an ingest job with the hostile fixture adapter registered: the kill-mid-call test's victim.

Usage: ``python sandboxed_job.py ROOT HOME DESTINATION WALL_SECONDS``

The job runs every adapter call in the sandbox with ``WALL_SECONDS`` as its wall limit. A test
kills this process while one of those calls hangs, then checks that the call's child died with
it and that the workspace holds no trace of the chunk it was reading.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.registry import AdapterRegistry
from neptune.runtime import IngestJob, JobOptions, Limits
from neptune.store.workspace import Workspace

ADAPTERS = Path(__file__).parents[1] / "adapters"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ADAPTERS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    root, home, destination = (Path(arg) for arg in argv[:3])
    tally, hostile = load("tally_adapter"), load("hostile_adapter")
    registry = AdapterRegistry(
        [*builtin_adapters(), tally.TallyAdapter(rows_per_chunk=2), hostile.HostileAdapter()]
    )
    options = JobOptions(attempts=1, limits=Limits(wall_seconds=int(argv[3])))
    outcome = IngestJob(root, destination, Workspace(home), registry, options).run()
    return 0 if outcome.package is not None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
