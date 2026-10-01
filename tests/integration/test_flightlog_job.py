"""MVL-20 end to end: PX4 ULog and ArduPilot DataFlash logs through the SDK, the real job and the
real sandbox (ADR 0030).

A folder of the fixtures (a multicopter, a ground rover, a boat-frame ArduRover, an old
millisecond-clock log, damaged ones, and one log without an extension) is ingested by ``Neptune``
with the shipped adapters. Every log must be read by ``flightlog``, every series row in the package
must resolve to exactly the message it was read from, two fresh workspaces must write the same
package, and a rerun must reuse every chunk.
"""

import shutil
import struct
from pathlib import Path
from typing import Final

import pytest

from neptune.model.finding import IngestFinding
from neptune.model.run import Run, Stream
from neptune.sdk import Neptune
from neptune.store.series import read_rows

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
LOGS: Final = (
    ("ulog", "copter.ulg"),
    ("ulog", "copter_appended.ulg"),
    ("ulog", "rover.ulg"),
    ("ulog", "copter_truncated.ulg"),
    ("ardupilot", "copter.bin"),
    ("ardupilot", "rover.bin"),
    ("ardupilot", "legacy_timems.bin"),
    ("ardupilot", "copter_truncated.bin"),
)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("flight_logs") / "logs"
    root.mkdir()
    for folder, name in LOGS:
        shutil.copyfile(FIXTURES / folder / name, root / name)
    shutil.copyfile(FIXTURES / "ulog" / "rover.ulg", root / "renamed_without_extension")
    return root


@pytest.fixture(scope="module")
def ingested(corpus: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, object]:
    work = tmp_path_factory.mktemp("flightlog_job")
    result = Neptune(work / "home").ingest(corpus, work / "package")
    return corpus, work, result


def package_files(package: Path) -> dict[str, bytes]:
    return {
        path.relative_to(package).as_posix(): path.read_bytes()
        for path in sorted(package.rglob("*"))
        if path.is_file() and "volatile" not in path.parts
    }


def test_every_log_lands_read_by_flightlog_inside_the_sandbox(
    ingested: tuple[Path, Path, object],
) -> None:
    _, work, result = ingested
    assert result.committed  # type: ignore[attr-defined]
    package = result.read_package()  # type: ignore[attr-defined]
    transforms = {t.id: t.adapter_id for t in package.receipt.transforms}
    readers = {
        source.location.path: [
            transforms[t] for t in source.read_by if transforms[t] != "neptune.grouping"
        ]  # type: ignore[union-attr]
        for source in package.receipt.sources
    }
    assert len(readers) == len(LOGS) + 1
    assert all(found == ["flightlog"] for found in readers.values()), readers
    codes = {r.code for r in package.records if isinstance(r, IngestFinding)}
    quarantined = {
        c for c in codes if c.startswith("neptune.") and not c.startswith("neptune.grouping.")
    }
    assert not quarantined
    assert {"flightlog.truncated", "flightlog.dropout", "flightlog.units_not_declared"} <= codes
    runs = [r for r in package.records if isinstance(r, Run)]
    assert len(runs) == len(LOGS)  # the renamed copy is the same bytes, so the same run


def test_every_row_in_the_package_resolves_to_its_message(
    ingested: tuple[Path, Path, object],
) -> None:
    root, _, result = ingested
    package = result.read_package()  # type: ignore[attr-defined]
    location = {s.content_id: s.location.path for s in package.receipt.sources}  # type: ignore[union-attr]
    rows = 0
    for stream in (r for r in package.records if isinstance(r, Stream)):
        data = (root / location[stream.series.source]).read_bytes()
        for row in read_rows(package.series[stream.id]):
            stream.check_row(row)
            (step,) = stream.row_evidence(row).locator
            offset, length = step.offset, step.length  # type: ignore[attr-defined]
            if data.startswith(b"ULog"):
                size, _ = struct.unpack_from("<HB", data, offset)
                assert length == 3 + size
            else:
                assert data[offset : offset + 2] == b"\xa3\x95"
            rows += 1
    assert rows > 100


def test_two_fresh_workspaces_write_the_same_package_and_a_rerun_reuses_every_chunk(
    ingested: tuple[Path, Path, object], tmp_path: Path
) -> None:
    root, work, _ = ingested
    again = Neptune(tmp_path / "home").ingest(root, tmp_path / "again")
    assert again.committed
    assert package_files(tmp_path / "again") == package_files(work / "package")
    rerun = Neptune(work / "home").ingest(root, tmp_path / "rerun")
    assert (rerun.cache.calls.plan, rerun.cache.calls.ingest) == (0, 0)
    assert package_files(tmp_path / "rerun") == package_files(work / "package")
