"""Deploy's D1 package write at scale, reproducible: the numbers ADR 0065 and ADR 0070 quote.

    uv run --all-packages python scripts/bench_d1_package_write.py WORK [ROWS ...]

Grows the warehouse fleet archetype's CMMS export to ROWS work orders (default 100,000) with
Deploy's own stress fixture (``stress_lifecycle_mapper.work_orders``: the committed package's rows
cycled at new row numbers; synthetic, nothing fetched), maps it with the ``cmms_generic`` preset,
and measures each case in a process of its own, so each peak is its own:

- ``memory``: map, then ``package_files`` and ``write_package`` (the whole package in memory);
- ``stream``: map, then ``write_package_stream``, spilling to a scratch directory (ADR 0065);
- ``read``: read the streamed package back as the job does, every record once, and validate it
  (ADR 0070).

``WORK`` is a directory of the caller's on a real disk (not a RAM-backed temp directory, which
would count spill as memory); each case writes under it and removes what it wrote. One JSON line
per case: rows, the case, seconds mapping and writing (or reading), peak resident memory in MiB
(``VmHWM``), the package id, its size on disk and its finding count.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Final

ROOT: Final = Path(__file__).resolve().parents[1]
FIXTURE: Final = (
    ROOT
    / "packages"
    / "neptune-deploy"
    / "tests"
    / "fixtures"
    / "archetypes"
    / "stress_lifecycle_mapper.py"
)
CASES: Final = ("memory", "stream", "read")


def _fixture() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stress_lifecycle_mapper", FIXTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _peak_mib() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024
    raise OSError("no VmHWM in /proc/self/status: this benchmark measures on Linux")


def _mapped(rows: int) -> list[Any]:
    from neptune_deploy.lifecycle import preset
    from neptune_deploy.lifecycle.run import map_records

    return list(map_records(_fixture().work_orders(rows), [preset("cmms_generic")], ()))


def _size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def case(name: str, rows: int, work: Path) -> dict[str, Any]:
    from neptune.store.package import package_files, read_package, write_package
    from neptune.store.writer import write_package_stream
    from neptune.validate import validate_package

    out, scratch = work / f"package-{rows}", work / f"scratch-{rows}"
    scratch.mkdir(exist_ok=True)
    result: dict[str, Any] = {"rows": rows, "case": name}
    start = time.perf_counter()
    if name == "read":
        package = read_package(out, scratch=scratch)
        result["records"] = sum(1 for _ in package.records)
        result["findings"] = dict(package.manifest.tables).get("ingest_finding", 0)
        result["validation_findings"] = len(validate_package(package, spill=scratch).findings)
        result["read_s"] = round(time.perf_counter() - start, 2)
        result["id"] = package.id
    else:
        shutil.rmtree(out, ignore_errors=True)
        records = _mapped(rows)
        mapped = time.perf_counter()
        if name == "memory":
            identity = write_package(out, package_files(records))
        else:
            identity = write_package_stream(out, records, scratch=scratch)
        result["map_s"] = round(mapped - start, 2)
        result["write_s"] = round(time.perf_counter() - mapped, 2)
        result["id"] = identity
        result["mib_on_disk"] = round(_size(out) / 2**20)
        result["findings"] = (out / "records" / "ingest_finding.jsonl").read_bytes().count(b"\n")
    result["peak_mib"] = round(_peak_mib())
    if name == "memory":
        shutil.rmtree(out)  # the streamed package is the one read back
    if name == "read":
        shutil.rmtree(out)
        scratch.rmdir()  # every spilled run was removed: it is empty
    return result


def main(argv: list[str]) -> int:
    """Each case in a process of its own, in order: memory, stream, then read what stream wrote."""
    if argv[:1] == ["--case"]:
        sys.stdout.write(json.dumps(case(argv[1], int(argv[2]), Path(argv[3]))) + "\n")
        return 0
    if not argv:
        sys.stderr.write(__doc__ or "")
        return 2
    work = Path(argv[0])
    work.mkdir(parents=True, exist_ok=True)
    for rows in [int(arg) for arg in argv[1:]] or [100_000]:
        for name in CASES:
            command = [sys.executable, __file__, "--case", name, str(rows), str(work)]
            done = subprocess.run(command, check=True, capture_output=True, text=True)
            sys.stdout.write(done.stdout)
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
