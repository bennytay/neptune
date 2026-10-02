"""MVL-19 end to end: rosbag2 bags through the SDK, the real job and the real sandbox (ADR 0030).

A folder of three bags (sqlite3, MCAP, split sqlite3) and a hostile one is ingested with
``Neptune``. Each file is read by the adapter that claims it: ``metadata.yaml`` and ``.db3`` by
``rosbag2``, ``.mcap`` by ``mcap``, in a confined child per call. The package must hold both
backends' streams, every sqlite3 row must resolve to its message's cell, the bags must be grouped
as recordings, two fresh jobs must write the same package, and a hostile bag (a part path that
leaves the directory, a damaged database, a link) must cost its own findings only.
"""

import shutil
from pathlib import Path
from typing import Final, TypeAlias

import pytest

from neptune.adapters.rosbag2._sqlite import Database, read_schema
from neptune.derived.grouping import Rule
from neptune.derived.sessions import SessionProposal, read_derived
from neptune.discovery.reader import BytesReader
from neptune.model.finding import IngestFinding
from neptune.model.provenance import ByteRange
from neptune.model.run import Run, Stream
from neptune.sdk import IngestResult, Neptune
from neptune.store.series import read_rows

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "rosbag2"
BAGS: Final = ("mobile_base_sqlite3", "mobile_base_mcap", "split_sqlite3")
Ingested: TypeAlias = tuple[Path, Path, IngestResult]


def corpus(root: Path) -> Path:
    root.mkdir()
    for bag in BAGS:
        shutil.copytree(FIXTURES / bag, root / bag)
    hostile = root / "hostile_bag"
    hostile.mkdir()
    meta = (FIXTURES / "mobile_base_sqlite3" / "metadata.yaml").read_text()
    (hostile / "metadata.yaml").write_text(
        meta.replace("mobile_base_sqlite3_0.db3", "../../etc/passwd", 1)
    )
    db = bytearray((FIXTURES / "mobile_base_sqlite3" / "mobile_base_sqlite3_0.db3").read_bytes())
    schema = read_schema(Database(BytesReader(bytes(db))))
    page = next(e.root for e in schema if e.name == "messages")
    for offset in range((page - 1) * 4096 + 5, page * 4096, 3):  # the messages page
        db[offset] ^= 0xFF
    (hostile / "hostile_bag_0.db3").write_bytes(bytes(db))
    (hostile / "hostile_bag_1.db3").symlink_to("/etc/passwd")
    return root


@pytest.fixture(scope="module")
def ingested(tmp_path_factory: pytest.TempPathFactory) -> Ingested:
    work = tmp_path_factory.mktemp("rosbag2_job")
    root = corpus(work / "root")
    result = Neptune(work / "home").ingest(root, work / "package")
    return root, work, result


def test_each_file_is_read_by_the_adapter_that_claims_it(ingested: Ingested) -> None:
    _, _, result = ingested
    assert result.committed
    package = result.read_package()
    transforms = {t.id: t.adapter_id for t in package.receipt.transforms}
    readers = {}
    for source in package.receipt.sources:
        path = str(getattr(source.location, "path", ""))
        readers[path] = sorted(
            {
                transforms[t]
                for t in source.read_by
                if transforms[t] not in {"neptune.grouping", "neptune.clocks"}
            }
        )
    for bag in BAGS:
        assert readers[f"{bag}/metadata.yaml"] == ["rosbag2"]
    assert readers["mobile_base_sqlite3/mobile_base_sqlite3_0.db3"] == ["rosbag2"]
    assert readers["mobile_base_mcap/mobile_base_mcap_0.mcap"] == ["mcap"]
    assert readers["split_sqlite3/split_sqlite3_1.db3"] == ["rosbag2"]


def test_both_backends_and_every_part_land_as_streams_with_runs(ingested: Ingested) -> None:
    _, _, result = ingested
    package = result.read_package()
    location = {s.content_id: str(getattr(s.location, "path", "")) for s in package.receipt.sources}
    streams: dict[str, int] = {}
    for stream in (r for r in package.records if isinstance(r, Stream)):
        folder = location[stream.series.source].split("/")[0]
        streams[folder] = streams.get(folder, 0) + 1
    assert streams["mobile_base_sqlite3"] == 3
    assert streams["mobile_base_mcap"] == 3
    assert streams["split_sqlite3"] == 6  # a topic split over two files: a stream per file
    # A run per data file and per metadata file of the three clean bags.
    runs = [r for r in package.records if isinstance(r, Run)]
    assert len(runs) >= 3 * 1 + 4


def test_every_sqlite3_row_in_the_package_resolves_to_its_message(ingested: Ingested) -> None:
    root, _, result = ingested
    package = result.read_package()
    location = {s.content_id: str(getattr(s.location, "path", "")) for s in package.receipt.sources}
    rows = 0
    for stream in (r for r in package.records if isinstance(r, Stream)):
        path = location[stream.series.source]
        if not path.endswith(".db3") or path.startswith("hostile_bag"):
            continue
        data = (root / path).read_bytes()
        for row in read_rows(package.series[stream.id]):
            stream.check_row(row)
            (step,) = stream.row_evidence(row).locator
            assert isinstance(step, ByteRange)
            cell = data[step.offset : step.offset + step.length]
            assert len(cell) == step.length > row["value/data_bytes"]  # type: ignore[operator]
            rows += 1
    assert rows == 18 + 18


def test_the_bags_are_grouped_as_recordings_of_their_parts(ingested: Ingested) -> None:
    _, _, result = ingested
    package = result.read_package()
    found = {
        str(getattr(p.members[0].location, "path", "")).rsplit("/", 1)[0]: p
        for p in read_derived(package.derived)
        if isinstance(p, SessionProposal) and p.rule == Rule.ROSBAG2_DIRECTORY
    }
    assert {"mobile_base_sqlite3", "mobile_base_mcap", "split_sqlite3"} <= set(found)
    assert len(found["split_sqlite3"].members) == 3  # metadata.yaml and both parts
    assert len(found["mobile_base_mcap"].members) == 2


def test_a_hostile_bag_costs_its_own_findings_and_nothing_else(ingested: Ingested) -> None:
    _, _, result = ingested
    package = result.read_package()
    codes = {r.code for r in package.records if isinstance(r, IngestFinding)}
    assert "rosbag2.unsafe_part_path" in codes
    assert codes & {"rosbag2.damaged_page", "rosbag2.bad_row", "rosbag2.truncated"}
    assert "neptune.discovery.symlink_not_followed" in codes
    # The clean bags are whole.
    assert not any(c.startswith("neptune.sandbox") for c in codes)


def test_two_fresh_jobs_write_the_same_package_and_a_rerun_reuses_every_chunk(
    ingested: Ingested, tmp_path: Path
) -> None:
    root, work, first = ingested
    again = Neptune(tmp_path / "home").ingest(root, tmp_path / "again")
    assert again.package == first.package and again.receipt == first.receipt
    rerun = Neptune(work / "home").ingest(root, tmp_path / "rerun")
    assert (rerun.cache.calls.plan, rerun.cache.calls.ingest) == (0, 0)
    assert rerun.package == first.package


def test_a_split_bag_missing_a_part_is_reported_by_its_own_metadata(tmp_path: Path) -> None:
    root = tmp_path / "root"
    shutil.copytree(FIXTURES / "split_sqlite3", root / "split")
    meta = root / "split" / "metadata.yaml"
    meta.write_text(meta.read_text().replace("_1.db3", "_3.db3"))
    (root / "split" / "split_sqlite3_1.db3").unlink()
    result = Neptune(tmp_path / "home").ingest(root, tmp_path / "package")
    assert result.committed
    findings = [r for r in result.read_package().records if isinstance(r, IngestFinding)]
    gap = next(f for f in findings if f.code == "rosbag2.part_gap")
    assert gap.details["missing"] == [1, 2]
