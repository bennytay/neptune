"""Measure one large source through the real job: the M2 gate's large-source scenario (MVL-57).

Usage: ``python stress_large_source.py WORKDIR GIB``

Generates a sparse frame log of ``GIB`` GiB under ``WORKDIR/run`` (headers written, payloads
holes, so it costs no disk), ingests it with the sandbox into a workspace under ``WORKDIR``,
ingests it again, and prints one JSON object of measurements:

- ``inspect_seconds``: the job's inspect phase, its head read and the probe engine's one
  sandboxed call; ``adapter_inspect_seconds``: the adapter's ``inspect`` through the sandbox;
- ``fingerprint_seconds``, ``plan_seconds``, ``parse_seconds``, ``assemble_seconds``,
  ``validate_seconds``: the job's phases; ``first_receipt_seconds``: the whole first job, from
  start to a published package; ``rerun_seconds``: the second job, every chunk a cache hit;
- ``chunks`` and ``plan_bytes``: the plan, and the size of the plan the workspace keeps;
- ``parent_peak_rss_mib`` and ``child_peak_rss_mib``: the job's peak resident memory and the
  largest of its sandboxed calls';
- ``rerun``: the second job's adapter calls.

``GIB`` 0 measures a small log, for tests. The generated run directory is removed at the end;
the workspace and packages are left in ``WORKDIR``. Peak memory is the process's, so run it in a
process of its own (as ``main`` does) for a clean number.
"""

import importlib.util
import json
import resource
import shutil
import struct
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.contract import configure
from neptune.adapters.registry import AdapterRegistry
from neptune.discovery.reader import LocalReader
from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource
from neptune.identity.revisions import SourceLedger
from neptune.model.source import LocalPath, SourceRevision
from neptune.runtime import IngestJob, wire
from neptune.runtime.sandbox import Returned, Subprocess
from neptune.store.workspace import Workspace

ADAPTERS = Path(__file__).parents[1] / "adapters"
FRAME = 4 * 1024 * 1024  # bytes of payload per frame: 256 frames per GiB
FRAMES_PER_CHUNK = 16


def _framelog() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "framelog_adapter", ADAPTERS / "framelog_adapter.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _generate(path: Path, frames: int, frame: int, magic: bytes) -> int:
    with path.open("wb") as stream:
        stream.write(magic)
        for index in range(frames):
            stream.seek(len(magic) + index * (12 + frame))
            stream.write(struct.pack("<QI", 1_000_000 * index, frame))
        stream.truncate(len(magic) + frames * (12 + frame))
    return path.stat().st_size


def _mib(kib: int) -> float:
    return round(kib / 1024, 1)


def measure(workdir: Path, gib: int) -> dict[str, Any]:
    framelog = _framelog()
    root = workdir / "run"
    root.mkdir(parents=True)
    frames, frame = (gib * 256, FRAME) if gib > 0 else (8, 64 * 1024)
    size = _generate(root / "camera.framelog", frames, frame, framelog.MAGIC)

    def registry() -> AdapterRegistry:
        adapter = framelog.FrameLogAdapter(frames_per_chunk=FRAMES_PER_CHUNK)
        return AdapterRegistry([*builtin_adapters(), adapter])

    workspace = Workspace(workdir / "home")
    started = time.perf_counter()
    first = IngestJob(root, workdir / "first", workspace, registry()).run()
    first_seconds = time.perf_counter() - started
    durations = dict(first.durations)
    (source,) = first.cache.sources
    plan_path = workspace.home / "plans" / source.source[7:9] / source.source[9:]
    plan_bytes = sum(p.stat().st_size for p in plan_path.iterdir())

    started = time.perf_counter()
    again = IngestJob(root, workdir / "again", workspace, registry()).run()
    rerun_seconds = time.perf_counter() - started

    # The adapter's own ``inspect``, through the sandbox, as a dry run (MVL-15) would call it.
    local = LocalSource(root)
    ledger = SourceLedger()
    scan(local, ledger)
    head = ledger.head(LocalPath("camera.framelog"))
    assert isinstance(head, SourceRevision)
    artifact = ledger.artifact(head.content_id)
    assert artifact is not None
    adapter = registry().get("framelog")
    config = configure(adapter.descriptor)
    box = Subprocess()
    with LocalReader(local, LocalPath("camera.framelog"), artifact) as reader:
        started = time.perf_counter()
        inspected = box.call(
            lambda: adapter.inspect(reader, config), wire.INSPECT, (reader.fileno(),)
        )
        inspect_seconds = time.perf_counter() - started
    assert isinstance(inspected, Returned)

    shutil.rmtree(root)
    rounded = {phase: round(seconds, 3) for phase, seconds in durations.items()}
    return {
        "adapter_inspect_seconds": round(inspect_seconds, 3),
        "assemble_seconds": rounded["assemble"],
        "child_peak_rss_mib": _mib(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss),
        "chunks": len(source.chunks),
        "fingerprint_seconds": rounded["fingerprint"],
        "first_receipt_seconds": round(first_seconds, 3),
        "inspect_seconds": rounded["inspect"],
        "parent_peak_rss_mib": _mib(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        "parse_seconds": round(rounded["parse"] + rounded["normalize"], 3),
        "plan_bytes": plan_bytes,
        "plan_seconds": rounded["plan"],
        "rerun": {
            "calls": {
                "ingest": again.cache.calls.ingest,
                "plan": again.cache.calls.plan,
                "probe": again.cache.calls.probe,
            },
            "chunk_hits": again.cache.totals()["chunks"]["hit"],
            "same_package": again.package == first.package,
        },
        "rerun_seconds": round(rerun_seconds, 3),
        "size_bytes": size,
        "sources": len(first.ingested),
        "state": str(first.state),
        "validate_seconds": rounded["validate"],
    }


def main(argv: list[str]) -> int:
    workdir, gib = Path(argv[0]), int(argv[1])
    sys.stdout.write(json.dumps(measure(workdir, gib), sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
