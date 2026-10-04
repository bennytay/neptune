"""The streaming package writer at scale: peak memory capped whatever the record count, time
linear in it (ADR 0065).

Each write runs in a child process (``tests/fixtures/store/make_scale_package.py``), so its peak
resident memory is its own: the records are generated lazily inside it, so the peak is the
writer's, not the input's. The cap is ADR 0065's bound with room for allocator slack.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.store.package import package_files, package_id

ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = ROOT / "tests" / "fixtures" / "store" / "make_scale_package.py"
PEAK_CAP_MIB: Final = 256  # ADR 0065: interpreter + spill budget + buffers, independent of rows


def write(rows: int, tmp_path: Path, *extra: str) -> dict[str, Any]:
    scratch = tmp_path / f"scratch-{rows}"
    scratch.mkdir()
    out = tmp_path / f"package-{rows}"
    done = subprocess.run(
        [sys.executable, str(SCRIPT), str(rows), str(out), str(scratch), *extra],
        check=True,
        capture_output=True,
        text=True,
    )
    assert list(scratch.iterdir()) == []  # every spilled run was removed
    result: dict[str, Any] = json.loads(done.stdout)
    return result


def test_twenty_thousand_rows_stream_under_the_cap_to_the_in_memory_bytes(tmp_path: Path) -> None:
    streamed = write(20_000, tmp_path)
    assert streamed["peak_mib"] < PEAK_CAP_MIB
    spec = importlib.util.spec_from_file_location("make_scale_package", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert streamed["id"] == package_id(package_files(module.scale_records(20_000)))


@pytest.mark.slow
def test_a_hundred_thousand_and_a_million_rows_stay_under_the_cap_in_linear_time(
    tmp_path: Path,
) -> None:
    small = write(100_000, tmp_path)
    large = write(1_000_000, tmp_path)
    assert small["peak_mib"] < PEAK_CAP_MIB
    assert large["peak_mib"] < PEAK_CAP_MIB
    # Roughly linear: ten times the rows takes at most twice as long per row (a loaded machine and
    # one more merge level are the slack); an in-memory sort or a quadratic step would not pass.
    per_row = (small["seconds"] / 100_000, large["seconds"] / 1_000_000)
    assert per_row[1] < 2 * per_row[0], per_row
