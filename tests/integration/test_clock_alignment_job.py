"""MVL-36 end to end: a drone's PX4 log, an ArduRover boat's DataFlash log and a mobile robot's
MCAP recording through the real job (ADR 0060).

Each log's boot clock is related to the GPS time its fix messages carry, each MCAP channel's
publish clock to the recording's log clock, and nothing relates the three robots: the package says
so in one finding, and aligning across them is refused, never guessed. No stored tick changes.
"""

import shutil
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.derived.clocks import (
    Aligned,
    InferredClockMapping,
    InferredTimestampDomain,
    Reason,
    Unaligned,
)
from neptune.derived.sessions import read_derived
from neptune.derived.temporal import clock_graph
from neptune.model.finding import IngestFinding
from neptune.model.knowledge import Known, Unknown
from neptune.model.reference import TimestampDomain
from neptune.model.run import Stream
from neptune.model.time import Epoch, Timescale, Timestamp
from neptune.sdk import Neptune
from neptune.store.series import read_rows

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
SOURCES: Final = (("ulog", "copter.ulg"), ("ardupilot", "rover.bin"), ("mcap", "robot.mcap"))


def ingest(root: Path, work: Path) -> Any:
    result = Neptune(work / "home").ingest(root, work / "package")
    assert result.committed
    return result.read_package()


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("clocks") / "robots"
    root.mkdir()
    for folder, name in SOURCES:
        shutil.copyfile(FIXTURES / folder / name, root / name)
    return root


@pytest.fixture(scope="module")
def package(corpus: Path, tmp_path_factory: pytest.TempPathFactory) -> Any:
    return ingest(corpus, tmp_path_factory.mktemp("clocks_job"))


def streams(package: Any) -> list[Stream]:
    return [r for r in package.records if isinstance(r, Stream)]


def _value(knowledge: Any) -> Any:
    return knowledge.value if isinstance(knowledge, Known) else None


def test_each_log_boot_clock_maps_onto_its_gps_time(package: Any) -> None:
    derived = read_derived(package.derived)
    found = [d for d in derived if isinstance(d, InferredTimestampDomain)]
    assert sorted((d.field, d.epoch, d.timescale) for d in found) == [
        ("GWk,GMS", Known(Epoch.GPS), Known(Timescale.GPS)),
        ("time_utc_usec", Known(Epoch.UNIX), Known(Timescale.POSIX)),
    ]  # the copter's second GPS instance never had time (all zeros): no clock is made for it
    graph = clock_graph(package.records, derived)
    utc = next(d for d in found if d.field == "time_utc_usec")
    gps = next(
        s
        for s in streams(package)
        if _value(s.schema_name) == "vehicle_gps_position" and package.series.get(s.id)
        if any(row["value/time_utc_usec"] for row in read_rows(package.series[s.id]))
    )
    for row in read_rows(package.series[gps.id]):
        boot, fix = row["time/0"], row["value/time_utc_usec"]
        assert isinstance(boot, int) and isinstance(fix, int)
        aligned = graph.align(Timestamp(boot, gps.clocks[0]), utc.id)
        assert isinstance(aligned, Aligned) and aligned.inferred
        assert aligned.instant == Timestamp(fix, utc.id)
        assert aligned.bound is None  # the fix-to-publication latency is stated nowhere


def test_every_mcap_channel_with_messages_joins_the_log_clock(package: Any) -> None:
    domains = {r.id: r for r in package.records if isinstance(r, TimestampDomain)}
    mappings = [m for m in read_derived(package.derived) if isinstance(m, InferredClockMapping)]
    mcap = [s for s in streams(package) if domains[s.clocks[0]].field == "log_time"]
    mapped = {m.source for m in mappings}
    for stream in mcap:
        rows = list(read_rows(package.series[stream.id])) if stream.id in package.series else []
        assert (stream.clocks[1] in mapped) == bool(rows), stream.topic
    assert all(isinstance(m.residual_bound, Unknown) for m in mappings)


def test_the_robots_stay_unsynchronised_and_aligning_across_them_is_refused(package: Any) -> None:
    findings = [r for r in package.records if isinstance(r, IngestFinding)]
    [unsynced] = [f for f in findings if f.code == "neptune.clocks.unsynchronised"]
    groups = unsynced.details["groups"]
    assert isinstance(groups, list) and len(groups) >= 3  # three robots, never joined
    codes = {f.code for f in findings if f.code.startswith("neptune.clocks.")}
    assert {"neptune.clocks.latency_unbounded", "neptune.clocks.anchors_absent"} <= codes
    domains = {r.field: r for r in package.records if isinstance(r, TimestampDomain)}
    graph = clock_graph(package.records, read_derived(package.derived))
    boot, log_time = domains["timestamp"], domains["log_time"]
    assert graph.align(Timestamp(5_000_000, boot.id), log_time.id) == Unaligned(
        Reason.UNSYNCHRONISED
    )


def test_a_fresh_workspace_writes_the_same_alignment(
    corpus: Path, package: Any, tmp_path: Path
) -> None:
    again = ingest(corpus, tmp_path)
    assert again.id == package.id
    assert again.derived == package.derived
