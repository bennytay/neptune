"""MVL-17 end to end: MCAP recordings through the real job and the real sandbox (ADR 0030).

A folder of the fixtures, damaged ones included, is ingested by ``IngestJob`` with every adapter
call in a confined child. The package must commit with every recording read by ``mcap``, every
series row in it must resolve to exactly its message's bytes in the source, two fresh jobs must
write the same package, a rerun must reuse every chunk, and ``robot.mcap`` alone must give the
golden package (``tests/golden/mcap``).
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.adapters.builtin import default_registry
from neptune.model.finding import IngestFinding
from neptune.model.run import Stream
from neptune.runtime import IngestJob, JobOptions, JobOutcome, JobState
from neptune.store.package import read_package
from neptune.store.series import read_rows
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

TESTS: Final = Path(__file__).parents[1]
FIXTURES: Final = TESTS / "fixtures" / "mcap"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


READING: Final = _load("mcap_reading", FIXTURES / "mcap_reading.py")
GOLDEN: Final = _load("make_mcap_golden", TESTS / "golden" / "mcap" / "make_mcap_golden.py")
SOURCES: Final = (
    "robot.mcap",
    "robot_lz4.mcap",
    "unchunked.mcap",
    "no_summary.mcap",
    "truncated.mcap",
    "bad_crc.mcap",
    "lying_index.mcap",
)


def corpus(root: Path) -> Path:
    root.mkdir()
    for name in SOURCES:
        shutil.copyfile(FIXTURES / name, root / name)
    shutil.copyfile(FIXTURES / "robot.mcap", root / "renamed_without_extension")
    return root


def package_files(package: Path) -> dict[str, bytes]:
    return {
        path.relative_to(package).as_posix(): path.read_bytes()
        for path in sorted(package.rglob("*"))
        if path.is_file() and "volatile" not in path.parts
    }


@pytest.fixture(scope="module")
def ingested(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, JobOutcome]:
    work = tmp_path_factory.mktemp("mcap_job")
    root = corpus(work / "root")
    job = IngestJob(
        root, work / "package", Workspace(work / "home"), default_registry(), JobOptions()
    )
    outcome = job.run()
    return root, work, outcome


def test_every_recording_lands_read_by_mcap_inside_the_sandbox(
    ingested: tuple[Path, Path, JobOutcome],
) -> None:
    _, work, outcome = ingested
    assert outcome.state is JobState.COMMITTED
    package = read_package(work / "package")
    transforms = {t.id: t.adapter_id for t in package.receipt.transforms}
    for source in package.receipt.sources:
        readers = [transforms[t] for t in source.read_by if transforms[t] != "neptune.grouping"]
        assert readers == ["mcap"], source.location
    codes = {r.code for r in package.records if isinstance(r, IngestFinding)}
    quarantined = {
        c
        for c in codes
        if c.startswith("neptune.") and not c.startswith(("neptune.grouping.", "neptune.validate."))
    }
    assert not quarantined  # session grouping may say a recording's session is ambiguous
    assert {"mcap.truncated", "mcap.crc_mismatch", "mcap.message_count_mismatch"} <= codes


def test_every_row_in_the_package_resolves_to_its_message(
    ingested: tuple[Path, Path, JobOutcome],
) -> None:
    root, work, _ = ingested
    package = read_package(work / "package")
    location = {s.content_id: getattr(s.location, "path", "") for s in package.receipt.sources}
    rows = 0
    for stream in (r for r in package.records if isinstance(r, Stream)):
        data = (root / location[stream.series.source]).read_bytes()
        for row in read_rows(package.series[stream.id]):
            stream.check_row(row)
            _, record = READING.record_at(data, stream.row_evidence(row))
            message = READING.message(record)
            assert (row["time/0"], row["time/1"]) == (message.log_time, message.publish_time)
            rows += 1
    assert rows > 100


def test_two_fresh_jobs_write_the_same_package_and_a_rerun_reuses_every_chunk(
    ingested: tuple[Path, Path, JobOutcome], tmp_path: Path
) -> None:
    root, work, _ = ingested
    again = IngestJob(
        root, tmp_path / "again", Workspace(tmp_path / "home"), default_registry(), JobOptions()
    )
    assert again.run().state is JobState.COMMITTED
    assert package_files(tmp_path / "again") == package_files(work / "package")
    rerun = IngestJob(
        root, tmp_path / "rerun", Workspace(work / "home"), default_registry(), JobOptions()
    )
    outcome = rerun.run()
    assert (outcome.cache.calls.plan, outcome.cache.calls.ingest) == (0, 0)
    assert package_files(tmp_path / "rerun") == package_files(work / "package")


def test_a_recording_alone_gives_the_golden_package(tmp_path: Path) -> None:
    built = GOLDEN.package_documents(GOLDEN.ingest(tmp_path))
    kept = {
        path.relative_to(GOLDEN.HERE / "package").as_posix(): path.read_bytes()
        for path in sorted((GOLDEN.HERE / "package").rglob("*"))
        if path.is_file()
    }
    # On a deliberate change: `uv run python tests/golden/mcap/make_mcap_golden.py`, and say why.
    assert built == kept
    topics = {
        json.loads(line)["topic"]["value"]
        for line in kept["records/stream.jsonl"].decode().splitlines()
    }
    assert topics == {"/imu", "/battery", "/diagnostics", "/imu_rear"}
