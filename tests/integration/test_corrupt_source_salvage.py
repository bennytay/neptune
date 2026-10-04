"""MVL-42 acceptance: one malformed chunk or PDF does not destroy a multi-source run, and the
receipt states exactly what was lost (ADR 0069).

Real adapters over real recordings from several kinds of robot (a mobile base's MCAP, a ROS 1 bag,
a PX4 ULog and an ArduPilot DataFlash log, a datasheet PDF), each beside sources that must land
untouched. A decoder bug is injected per chunk by wrapping the real adapter (same descriptor, so
the same transform and chunk ids); corrupt bytes are generated from the fixtures in the test
(truncated at boundaries, a garbage tail, a zero-filled region) and run through the sandbox.
"""

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.contract import (
    Adapter,
    AdapterConfig,
    Chunk,
    ChunkOutput,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeResult,
    SourceReader,
    configure,
)
from neptune.adapters.flightlog import FlightLogAdapter
from neptune.adapters.mcap import McapAdapter
from neptune.adapters.pdf import PdfAdapter
from neptune.adapters.registry import AdapterRegistry
from neptune.adapters.rosbag1 import Rosbag1Adapter
from neptune.discovery.reader import BytesReader
from neptune.identity.hashing import content_id
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.runtime import (
    IngestJob,
    Isolation,
    JobEvent,
    JobOptions,
    JobOutcome,
    JobState,
    lineage,
)
from neptune.store.package import read_package
from neptune.store.series import count_rows
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
PARTIAL: Final = lineage.SOURCE_PARTIAL
REFUSED: Final = lineage.SALVAGE_REFUSED


class FailOn:
    """The real ``inner`` adapter, except that ``ingest`` raises on the chunks ``fails`` picks: a
    decoder bug that a sandboxed call reports as ``chunk_failed``. Same descriptor, so the same
    transform and chunk ids as ``inner``: a later job with ``inner`` reuses what this committed."""

    def __init__(self, inner: Adapter, fails: Callable[[Chunk], bool]) -> None:
        self.inner, self.fails = inner, fails
        self.descriptor = inner.descriptor

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        return self.inner.probe(head, hints)

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        return self.inner.inspect(source, config)

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        return self.inner.plan(source, config)

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        if self.fails(chunk):
            raise RuntimeError("a decoder bug")
        return self.inner.ingest(source, chunk, config)


def chunks_of(adapter: Adapter, data: bytes) -> tuple[Chunk, ...]:
    return adapter.plan(BytesReader(data), configure(adapter.descriptor, None)).chunks


def registry(*adapters: Adapter) -> AdapterRegistry:
    """The built-in adapters, with ``adapters`` in place of those of the same id."""
    mine = {adapter.descriptor.id: adapter for adapter in adapters}
    return AdapterRegistry(
        [mine.pop(a.descriptor.id, a) for a in builtin_adapters()] + list(mine.values())
    )


class Run:
    def __init__(
        self,
        root: Path,
        tmp_path: Path,
        adapters: AdapterRegistry,
        *,
        name: str = "package",
        home: str = "home",
        options: JobOptions | None = None,
    ) -> None:
        self.events: list[JobEvent] = []
        self.workspace = Workspace(tmp_path / home)
        job = IngestJob(
            root, tmp_path / name, self.workspace, adapters, options, on_event=self.events.append
        )
        self.outcome: JobOutcome = job.run()
        assert self.outcome.state is JobState.COMMITTED
        self.package: Any = read_package(tmp_path / name)

    def only(self, code: str) -> Any:
        (found,) = [f for f in self.outcome.findings if f.code == code]
        return found

    def of(self, kind: str) -> list[JobEvent]:
        return [e for e in self.events if e.kind == kind]

    def ingested(self) -> set[str]:
        return {source for source, _ in self.outcome.ingested}

    def rows(self) -> int:
        return sum(count_rows(path) for path in self.package.series.values())


def corpus(tmp_path: Path, *recordings: tuple[str, str]) -> Path:
    """A root with the ``(fixture, name)`` recordings beside a text note and a datasheet PDF."""
    root = tmp_path / "site"
    root.mkdir(exist_ok=True)
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    shutil.copy(FIXTURES / "pdf" / "gripper_datasheet.pdf", root / "gripper_datasheet.pdf")
    for fixture, name in recordings:
        shutil.copy(FIXTURES / fixture, root / name)
    return root


# One data chunk of each recording (chunk_bytes small enough for several): the middle one, or for
# DataFlash the last, since its middle chunk declares a parameter table the last one's records
# name (losing it refuses the salvage: ``reference_lost``).
RECORDINGS: Final = [
    pytest.param(McapAdapter(chunk_bytes=512), "mcap/robot.mcap", "base.mcap", 2, id="mcap"),
    pytest.param(
        Rosbag1Adapter(chunk_bytes=2048), "rosbag1/robot_none.bag", "arm.bag", 1, id="rosbag1"
    ),
    pytest.param(FlightLogAdapter(chunk_bytes=256), "ulog/copter.ulg", "copter.ulg", 3, id="ulog"),
    pytest.param(
        FlightLogAdapter(chunk_bytes=1024), "ardupilot/copter.bin", "rover.bin", 2, id="dataflash"
    ),
]


@pytest.mark.parametrize(("adapter", "fixture", "name", "which"), RECORDINGS)
def test_a_decoder_bug_in_one_data_chunk_loses_exactly_its_bytes(
    tmp_path: Path, adapter: Adapter, fixture: str, name: str, which: int
) -> None:
    data = (FIXTURES / fixture).read_bytes()
    windows = [c for c in chunks_of(adapter, data) if isinstance(c.context.get("start"), int)]
    assert len(windows) >= 3, "the fixture needs several data chunks"
    bad = windows[which]
    start, end = bad.context["start"], bad.context["end"]
    assert isinstance(start, int) and isinstance(end, int) and start < end
    root = corpus(tmp_path, (fixture, name))
    clean = Run(root, tmp_path, registry(adapter), name="clean", home="clean-home")
    run = Run(root, tmp_path, registry(FailOn(adapter, lambda c: c.id == bad.id)))

    # Every source landed: the note, the PDF and the recording without its bad chunk.
    assert run.ingested() == clean.ingested() and len(run.ingested()) == 3
    source = content_id(data)
    failed = run.only(lineage.CHUNK_FAILED)
    assert failed.subject == EvidenceRef(source, (ByteRange(start, end - start),))
    assert failed.details["extent"] == {"length": end - start, "offset": start}
    assert failed.details["chunk"] == bad.id and failed.details["attempts"] == 2
    partial = run.only(PARTIAL)
    assert partial.subject == EvidenceRef(source, (ByteRange(0, len(data)),))
    planned = len(chunks_of(adapter, data))
    assert partial.details["chunks"] == planned and partial.details["committed"] == planned - 1
    assert partial.details["lost"] == [
        {
            "chunk": bad.id,
            "code": lineage.CHUNK_FAILED,
            "extent": {"length": end - start, "offset": start},
        }
    ]
    assert partial.details["not_covered"] == [{"length": end - start, "offset": start}]
    assert partial.details["not_covered_bytes"] == end - start
    assert partial.related == (EvidenceRef(source, (ByteRange(start, end - start),)),)
    assert f"[{start}, {end})" in partial.message
    # The receipt says it: both findings, in the package's own list.
    receipt = {(f.code, f.id) for f in run.package.receipt.findings}
    assert {(lineage.CHUNK_FAILED, failed.id), (PARTIAL, partial.id)} <= receipt
    (salvaged,) = run.of("source_salvaged")
    assert salvaged.details["source"] == source and salvaged.details["lost"] == 1
    assert not run.of("source_quarantined")
    # What the other chunks decoded is in the package; what the lost one held is not.
    assert 0 < run.rows() < clean.rows()
    held = {getattr(r, "id", None) for r in clean.package.records}
    added = {r.kind for r in run.package.records if getattr(r, "id", None) not in held}
    assert added <= {"ingest_finding", "transform_record"}  # the runtime's findings and itself


def test_losing_the_declarations_refuses_the_salvage_and_the_rest_land(tmp_path: Path) -> None:
    """Rows of streams declared in a lost chunk cannot stand alone: the MCAP is quarantined with
    ``salvage_refused`` naming the laws, and every other source still lands."""
    adapter = McapAdapter(chunk_bytes=512)
    root = corpus(tmp_path, ("mcap/robot.mcap", "base.mcap"))
    declarations = lambda c: c.context.get("part") == "declarations"  # noqa: E731
    run = Run(root, tmp_path, registry(FailOn(adapter, declarations)))
    refused = run.only(REFUSED)
    laws = {p["law"] for p in refused.details["problems"]}
    assert laws == {"stream_undeclared"}
    assert refused.details["lost"] == 1 and refused.details["chunks"] == 5
    assert "(stream_undeclared)" in refused.message
    assert not [f for f in run.outcome.findings if f.code == PARTIAL]
    (quarantined,) = run.of("source_quarantined")
    assert quarantined.details["codes"] == [lineage.CHUNK_FAILED, REFUSED]
    assert len(run.ingested()) == 2 and not run.package.series  # the note and the PDF


def test_a_lost_table_that_kept_records_name_refuses_the_salvage(tmp_path: Path) -> None:
    """DataFlash's middle chunk declares a parameter table the last chunk's records name: kept,
    they would dangle, so the salvage is refused (``reference_lost``)."""
    adapter = FlightLogAdapter(chunk_bytes=1024)
    data = (FIXTURES / "ardupilot" / "copter.bin").read_bytes()
    middle = chunks_of(adapter, data)[1]
    root = corpus(tmp_path, ("ardupilot/copter.bin", "rover.bin"))
    run = Run(root, tmp_path, registry(FailOn(adapter, lambda c: c.id == middle.id)))
    problems = run.only(REFUSED).details["problems"]
    assert [(p["law"], p["target_kind"]) for p in problems] == [
        ("reference_lost", "structured_table")
    ]
    assert len(run.ingested()) == 2


def test_a_source_that_loses_every_chunk_is_quarantined(tmp_path: Path) -> None:
    root = corpus(tmp_path, ("ardupilot/copter.bin", "rover.bin"))
    run = Run(root, tmp_path, registry(FailOn(FlightLogAdapter(chunk_bytes=1024), bool)))
    refused = run.only(REFUSED)
    assert refused.details == {
        "adapter": "flightlog",
        "chunks": 3,
        "lost": 3,
        "problems": [],
        "version": refused.details["version"],
    }
    assert "every chunk was lost (3 chunks)" in refused.message
    assert len(run.ingested()) == 2


def test_a_malformed_pdf_page_is_lost_without_an_extent_and_said_so(tmp_path: Path) -> None:
    """The PDF adapter names no byte extent (a page is not a byte window), so its lost chunk is
    counted, not ranged; the document and its other page still land."""
    adapter = PdfAdapter(pages_per_chunk=1)
    data = (FIXTURES / "pdf" / "gripper_datasheet.pdf").read_bytes()
    pages = [c for c in chunks_of(adapter, data) if c.context.get("part") == "pages"]
    assert len(pages) == 2
    root = corpus(tmp_path, ("mcap/robot.mcap", "base.mcap"))
    run = Run(root, tmp_path, registry(FailOn(adapter, lambda c: c.id == pages[1].id)))
    failed = run.only(lineage.CHUNK_FAILED)
    assert failed.subject == EvidenceRef(content_id(data), (ByteRange(0, len(data)),))
    assert "extent" not in failed.details
    partial = run.only(PARTIAL)
    assert partial.details["undeclared"] == 1 and partial.details["not_covered"] == []
    assert partial.related == ()
    assert "1 lost chunk without a declared extent" in partial.message
    assert len(run.ingested()) == 3


def test_the_next_job_retries_only_the_lost_chunk_and_writes_the_fresh_package(
    tmp_path: Path,
) -> None:
    """Lost chunks are never committed: after the fix (here, the same adapter without the bug)
    the next job over the same workspace ingests exactly them, and its package is the one a
    fresh workspace writes (ADR 0028 §2)."""
    adapter = McapAdapter(chunk_bytes=512)
    data = (FIXTURES / "mcap" / "robot.mcap").read_bytes()
    bad = [c for c in chunks_of(adapter, data) if c.context.get("part") == "data"][2]
    root = corpus(tmp_path, ("mcap/robot.mcap", "base.mcap"))
    broken = Run(root, tmp_path, registry(FailOn(adapter, lambda c: c.id == bad.id)), name="a")
    assert broken.only(PARTIAL).details["lost"][0]["chunk"] == bad.id
    fixed = Run(root, tmp_path, registry(adapter), name="b")
    fresh = Run(root, tmp_path, registry(adapter), name="c", home="fresh-home")
    assert fixed.outcome.cache.calls.ingest == 1
    assert fixed.outcome.package == fresh.outcome.package != broken.outcome.package
    assert not [f for f in fixed.outcome.findings if f.code.startswith("neptune.runtime.")]


def test_a_salvaged_package_is_the_same_every_run(tmp_path: Path) -> None:
    adapter = Rosbag1Adapter(chunk_bytes=2048)
    data = (FIXTURES / "rosbag1" / "robot_none.bag").read_bytes()
    bad = next(c for c in chunks_of(adapter, data) if c.context.get("part") == "data")
    root = corpus(tmp_path, ("rosbag1/robot_none.bag", "arm.bag"))
    adapters = registry(FailOn(adapter, lambda c: c.id == bad.id))
    first = Run(root, tmp_path, adapters, name="a", home="w1")
    second = Run(root, tmp_path, adapters, name="b", home="elsewhere/deep/w2")
    assert first.outcome.package == second.outcome.package
    assert first.outcome.findings == second.outcome.findings


# --- Corrupt bytes, generated from the fixtures, through the sandbox ---------------------------


def mutants(data: bytes) -> dict[str, bytes]:
    """Damage a recording might come with: cut at boundaries, a garbage tail, a zeroed middle."""
    size = len(data)
    garbage = bytes((i * 151 + 7) % 256 for i in range(997))
    middle = size // 2
    return {
        "cut-quarter": data[: size // 4],
        "cut-half": data[:middle],
        "cut-last-byte": data[:-1],
        "garbage-tail": data + garbage,
        "zeroed-middle": data[: middle - 64] + bytes(128) + data[middle + 64 :],
    }


BASES: Final = (
    ("mcap/robot.mcap", ".mcap"),
    ("rosbag1/robot_none.bag", ".bag"),
    ("ulog/copter.ulg", ".ulg"),
    ("ardupilot/copter.bin", ".bin"),
    ("pdf/gripper_datasheet.pdf", ".pdf"),
    ("tabular/workorders_amr_fleet.xlsx", ".xlsx"),
)


@pytest.mark.slow
@pytest.mark.parametrize("chunk_bytes", [512, 64 * 1024 * 1024])
def test_corrupt_recordings_are_findings_and_the_same_package_every_run(
    tmp_path: Path, chunk_bytes: int
) -> None:
    """Every mutant of every base, in one root with a clean note, through sandboxed jobs: the job
    commits, the note lands, every mutant is in the receipt (read, salvaged or quarantined with
    a finding saying why), and two runs in fresh workspaces write the same package."""
    root = tmp_path / "site"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    expected: set[str] = set()
    for fixture, suffix in BASES:
        stem = Path(fixture).stem
        for kind, data in mutants((FIXTURES / fixture).read_bytes()).items():
            (root / f"{stem}-{kind}{suffix}").write_bytes(data)
            expected.add(content_id(data))
    adapters = registry(
        McapAdapter(chunk_bytes=chunk_bytes),
        Rosbag1Adapter(chunk_bytes=max(chunk_bytes, 2048)),
        FlightLogAdapter(chunk_bytes=chunk_bytes),
    )
    options = JobOptions(isolation=Isolation.SUBPROCESS)
    first = Run(root, tmp_path, adapters, name="a", home="w1", options=options)
    second = Run(root, tmp_path, adapters, name="b", home="w2", options=options)
    assert first.outcome.package == second.outcome.package
    assert content_id((FIXTURES / "text" / "notes.txt").read_bytes()) in first.ingested()
    read = {
        str(s.location.to_json()["path"]): len(s.read_by) for s in first.package.receipt.sources
    }
    assert all(count >= 1 for count in read.values())  # every file was read by someone
    assert len(read) == 1 + len(expected)
    quarantined = {str(e.details["source"]) for e in first.of("source_quarantined")}
    salvaged = {str(e.details["source"]) for e in first.of("source_salvaged")}
    admitted = {str(e.details["source"]) for e in first.of("source_admitted")}
    assert not (quarantined & admitted) and not (salvaged & admitted)
    findings_by_source: dict[str, set[str]] = {}
    for finding in first.outcome.findings:
        subject = finding.subject
        if isinstance(subject, EvidenceRef):
            findings_by_source.setdefault(str(subject.source), set()).add(finding.code)
    for source in quarantined | salvaged:
        assert findings_by_source.get(source), f"{source} left out without a finding"
