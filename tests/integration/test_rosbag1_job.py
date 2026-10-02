"""MVL-18 end to end: ROS 1 bags and the same recording as MCAP through the SDK and the sandbox.

A folder holding the bags (damaged ones included) and ``same_recording.mcap`` is ingested by
``Neptune`` with every adapter call in a confined child (ADR 0030). The package must commit with
each bag read by ``rosbag1`` and the MCAP file by ``mcap``, every series row must resolve to
exactly its message in the source, the same recording must give the same Run and Stream facts from
either container, two fresh ingests must write the same package, and a rerun must reuse every
chunk.
"""

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.model.finding import IngestFinding
from neptune.model.knowledge import Known
from neptune.model.run import Run, Stream
from neptune.sdk import IngestResult, Neptune
from neptune.store.package import read_package
from neptune.store.series import read_rows

pytestmark = pytest.mark.integration

TESTS: Final = Path(__file__).parents[1]
FIXTURES: Final = TESTS / "fixtures" / "rosbag1"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


READING: Final = _load("rosbag_reading", FIXTURES / "rosbag_reading.py")
MCAP_READING: Final = _load("mcap_reading", TESTS / "fixtures" / "mcap" / "mcap_reading.py")
SOURCES: Final = (
    "robot_bz2.bag",
    "robot_lz4.bag",
    "unclosed.bag",
    "truncated.bag",
    "lying_chunk_info.bag",
    "unknown_compression.bag",
    "connection_collision.bag",
    "empty.bag",
    "same_recording.mcap",
)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("rosbag1_corpus") / "mobile-manipulator"
    root.mkdir()
    for name in SOURCES:
        shutil.copyfile(FIXTURES / name, root / name)
    shutil.copyfile(FIXTURES / "robot_none.bag", root / "renamed_without_extension")
    return root


@pytest.fixture(scope="module")
def ingested(corpus: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, IngestResult]:
    work = tmp_path_factory.mktemp("rosbag1_job")
    return work, Neptune(work / "home").ingest(corpus, work / "package")


def package_files(package: Path) -> dict[str, bytes]:
    return {
        path.relative_to(package).as_posix(): path.read_bytes()
        for path in sorted(package.rglob("*"))
        if path.is_file() and "volatile" not in path.parts
    }


def test_every_bag_lands_read_by_rosbag1_and_the_recording_by_mcap(
    ingested: tuple[Path, IngestResult],
) -> None:
    work, result = ingested
    assert result.committed
    package = read_package(work / "package")
    transforms = {t.id: t.adapter_id for t in package.receipt.transforms}
    readers = {}
    for source in package.receipt.sources:
        derived = {
            "neptune.grouping",
            "neptune.introspection",
            "neptune.clocks",
        }  # interpretation, not readers
        names = [transforms[t] for t in source.read_by if transforms[t] not in derived]
        readers[getattr(source.location, "path", "")] = names
    assert readers["same_recording.mcap"] == ["mcap"]
    for name in readers:
        if name != "same_recording.mcap":
            assert readers[name] == ["rosbag1"], name  # renamed, extensionless included
    codes = {r.code for r in package.records if isinstance(r, IngestFinding)}
    assert {
        "rosbag1.chunk_truncated",
        "rosbag1.index_invalid",
        "rosbag1.message_count_mismatch",
        "rosbag1.unknown_compression",
        "rosbag1.conflicting_declaration",
    } <= codes


def test_every_row_in_the_package_resolves_to_its_message(
    corpus: Path, ingested: tuple[Path, IngestResult]
) -> None:
    work, _ = ingested
    package = read_package(work / "package")
    location = {s.content_id: getattr(s.location, "path", "") for s in package.receipt.sources}
    rows = 0
    for stream in (r for r in package.records if isinstance(r, Stream)):
        path = location[stream.series.source]
        data = (corpus / path).read_bytes()
        for row in read_rows(package.series[stream.id]):
            stream.check_row(row)
            if path.endswith(".mcap"):
                _, record = MCAP_READING.record_at(data, stream.row_evidence(row))
                assert row["time/0"] == MCAP_READING.message(record).log_time
            else:
                _, record = READING.record_at(data, stream.row_evidence(row))
                assert row["time/0"] == READING.message(record).time
            rows += 1
    assert rows > 100


def test_a_ros1_run_and_an_mcap_run_are_the_same_kind_of_thing_in_one_package(
    ingested: tuple[Path, IngestResult],
) -> None:
    work, _ = ingested
    package = read_package(work / "package")
    location = {s.content_id: getattr(s.location, "path", "") for s in package.receipt.sources}
    by_source: dict[str, dict[str, Stream]] = {}
    for stream in (r for r in package.records if isinstance(r, Stream)):
        topic = stream.topic.known_or_raise() if isinstance(stream.topic, Known) else ""
        by_source.setdefault(location[stream.series.source], {})[topic] = stream
    bag, mcap = by_source["robot_bz2.bag"], by_source["same_recording.mcap"]
    assert set(bag) == set(mcap) == {"/joint_states", "/odom", "/tf", "/tf_static"}
    for topic in bag:
        for field in ("schema_name", "schema_encoding", "message_encoding"):
            assert getattr(bag[topic], field).known_or_raise() == (
                getattr(mcap[topic], field).known_or_raise()
            ), (topic, field)
        assert bag[topic].metadata == mcap[topic].metadata
        assert (
            bag[topic].message_count.known_or_raise() == mcap[topic].message_count.known_or_raise()
        )
    runs = [r for r in package.records if isinstance(r, Run)]
    assert len(runs) == len(SOURCES) + 1  # every source has its run, the renamed one too


def test_two_fresh_ingests_write_the_same_package_and_a_rerun_reuses_every_chunk(
    corpus: Path, ingested: tuple[Path, IngestResult], tmp_path: Path
) -> None:
    work, first = ingested
    fresh = Neptune(tmp_path / "home").ingest(corpus, tmp_path / "again")
    assert fresh.committed and fresh.receipt == first.receipt
    assert package_files(tmp_path / "again") == package_files(work / "package")
    rerun = Neptune(work / "home").ingest(corpus, tmp_path / "rerun")
    assert (rerun.cache.calls.plan, rerun.cache.calls.ingest) == (0, 0)
    assert package_files(tmp_path / "rerun") == package_files(work / "package")
