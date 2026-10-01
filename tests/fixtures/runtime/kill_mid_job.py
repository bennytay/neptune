"""Run an ingest job and SIGKILL the process at a chosen point: the resume tests' victim.

Usage: ``python kill_mid_job.py ROOT HOME DESTINATION PROGRESS POINT COUNT``

Every event is appended to ``PROGRESS`` as one canonical JSON line and flushed to disk before the
job goes on, so the parent can read what the job did before it died. The process kills itself with
SIGKILL at ``POINT``, the ``COUNT``-th time the job gets there: no handler runs, no cleanup, the
way a crashed machine or an OOM killer ends a job. The points:

- ``committed``: just after a chunk's ``chunk_committed`` event;
- ``chunk-writing``: inside ``Workspace.commit``, just after a run file is written into the
  chunk's staging directory, before the rest of it is written or flushed;
- ``chunk-staged``: inside ``Workspace.commit``, the chunk's staging directory written whole and
  flushed, not yet renamed into ``chunks/``;
- ``package-staging``: inside ``assemble.stage``, a merged series written into the workspace's
  staging as a derivative (ADR 0031), the package's hidden staging directory open beside its
  destination;
- ``before-publish``: the envelope written into the staged package, not yet renamed into place;
- ``never``: the job runs to the end.

Each point but ``committed`` is armed by wrapping the function the job calls there, in this
process only: the code under test is the code that ships.
"""

import importlib.util
import os
import signal
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import neptune.runtime.job
import neptune.store.assemble
import neptune.store.workspace
from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.registry import AdapterRegistry
from neptune.identity import canonical_json
from neptune.runtime import IngestJob, JobEvent
from neptune.store.workspace import Workspace

if TYPE_CHECKING:
    from collections.abc import Callable

ADAPTERS = Path(__file__).parents[1] / "adapters"

# point -> (module, the name the job calls there, whether to die before the call or after it)
WRAPPED: dict[str, tuple[ModuleType, str, bool]] = {
    "chunk-writing": (neptune.store.workspace, "write_run", False),
    "chunk-staged": (neptune.store.workspace, "fsync_tree", False),
    "package-staging": (neptune.store.assemble, "merge_runs", False),
    "before-publish": (neptune.runtime.job, "publish", True),
}
POINTS = ("committed", "never", *WRAPPED)


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


def die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def arm(module: ModuleType, name: str, count: int, *, before: bool) -> None:
    """Make ``module.name`` die on its ``count``-th call, before it runs or after it returns."""
    original: Callable[..., Any] = getattr(module, name)
    calls = 0

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if before and calls == count:
            die()
        result = original(*args, **kwargs)
        if not before and calls == count:
            die()
        return result

    setattr(module, name, wrapper)


def main(argv: list[str]) -> int:
    root, home, destination, progress = (Path(arg) for arg in argv[:4])
    point, count = argv[4], int(argv[5])
    if point not in POINTS or count < 1:
        raise SystemExit(f"POINT is one of {POINTS} and COUNT at least 1")
    if point in WRAPPED:
        module, name, before = WRAPPED[point]
        arm(module, name, count, before=before)
    committed = 0
    with progress.open("ab") as log:

        def on_event(event: JobEvent) -> None:
            nonlocal committed
            log.write(canonical_json.dumps(event.to_json()) + b"\n")
            log.flush()
            os.fsync(log.fileno())
            if point == "committed" and event.kind == "chunk_committed" and event.details["new"]:
                committed += 1
                if committed == count:
                    die()

        job = IngestJob(root, destination, Workspace(home), registry(), on_event=on_event)
        outcome = job.run()
    return 0 if outcome.package is not None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
