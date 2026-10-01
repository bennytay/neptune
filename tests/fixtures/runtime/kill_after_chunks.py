"""Run an ingest job and SIGKILL the process after N chunks commit: the resume test's victim.

Usage: ``python kill_after_chunks.py ROOT HOME DESTINATION PROGRESS KILL_AFTER``

Every event is appended to ``PROGRESS`` as one canonical JSON line and flushed to disk before the
job goes on, so the parent can read what the job did before it died. After the ``KILL_AFTER``-th
newly committed chunk the process kills itself with SIGKILL: no handler runs, no cleanup, the way
a crashed machine or an OOM killer ends a job. With ``KILL_AFTER`` 0 the job runs to the end.
"""

import importlib.util
import os
import signal
import sys
from pathlib import Path
from types import ModuleType

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.registry import AdapterRegistry
from neptune.identity import canonical_json
from neptune.runtime import IngestJob, JobEvent
from neptune.store.workspace import Workspace

ADAPTERS = Path(__file__).parents[1] / "adapters"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ADAPTERS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def registry() -> AdapterRegistry:
    tally, framelog = load("tally_adapter"), load("framelog_adapter")
    return AdapterRegistry(
        [
            *builtin_adapters(),
            tally.TallyAdapter(rows_per_chunk=1),
            framelog.FrameLogAdapter(frames_per_chunk=8),
        ]
    )


def main(argv: list[str]) -> int:
    root, home, destination, progress = (Path(arg) for arg in argv[:4])
    kill_after = int(argv[4])
    committed = 0
    with progress.open("ab") as log:

        def on_event(event: JobEvent) -> None:
            nonlocal committed
            log.write(canonical_json.dumps(event.to_json()) + b"\n")
            log.flush()
            os.fsync(log.fileno())
            if event.kind == "chunk_committed" and event.details.get("new") is True:
                committed += 1
                if committed == kill_after:
                    os.kill(os.getpid(), signal.SIGKILL)

        job = IngestJob(root, destination, Workspace(home), registry(), on_event=on_event)
        outcome = job.run()
    return 0 if outcome.package is not None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
