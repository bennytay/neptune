"""MVL-57, the M2 gate: the runtime and the adapter ABI under the scenarios the gate names.

Each test is one scenario of ``docs/reviews/m2-stress-test.md``, run through the real job with the
real sandbox (ADR 0030) unless it says otherwise: a kill in the middle of a sandboxed parse, a
parser crash, an adapter whose chunks half carry findings, two adapters tied on an extensionless
file, cache hits and misses after a rename, a version bump and a config change, and the hostile
fixture suite end to end. The large-source measurements are
``tests/fixtures/runtime/stress_large_source.py``, run at a small size here.
"""

import ctypes
import importlib.util
import io
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pyarrow.parquet as pq
import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.contract import (
    ABI_VERSION,
    SIGNATURE,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    FormatSpec,
    InspectResult,
    Magic,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
    scratch_directory,
)
from neptune.adapters.registry import AdapterRegistry
from neptune.discovery.archive import inspect_archive
from neptune.discovery.probe import ProbeEngine
from neptune.discovery.reader import BytesReader
from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import content_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.runtime import IngestJob, JobEvent, JobOptions, JobOutcome, JobState, Limits, confine
from neptune.runtime.sandbox import Codec, Returned, Subprocess
from neptune.store.package import read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
TALLY_MAGIC: Final = b"TALLY1\n"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TALLY: Final = _load("tally_adapter", FIXTURES / "adapters" / "tally_adapter.py")
HOSTILE: Final = _load("hostile_adapter", FIXTURES / "adapters" / "hostile_adapter.py")


class Rival:
    """A second adapter for tally files, exactly as sure of them as the tally adapter: a tie."""

    descriptor = AdapterDescriptor(
        id="rival",
        version="1.0.0",
        abi=ABI_VERSION,
        summary="Claims tally files as strongly as the tally adapter, for the tie scenario.",
        formats=(FormatSpec("Tally", magic=(Magic(0, TALLY_MAGIC),)),),
        record_kinds=("document_record",),
        config=(),
        libraries=(),
        finding_codes=(),
        locator_steps=(),
        conventions=(),
        resources=Resources(0, True),
        security=(),
    )

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if head.startswith(TALLY_MAGIC):
            return ProbeResult(SIGNATURE, (ProbeReason("rival.magic", "starts TALLY1"),))
        return ProbeResult(0.0, ())

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        raise NotImplementedError

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        raise NotImplementedError

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        raise NotImplementedError


class EchoingTally(TALLY.TallyAdapter):  # type: ignore[misc, name-defined]
    """Reports every bad row as one finding about the whole source: the same finding from every
    chunk that holds one, which law 9 (one chunk per output) forbids."""

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        output: ChunkOutput = super().ingest(source, chunk, config)
        if not output.findings:
            return output
        echo = ingest_finding(
            code="tally.bad_row",
            category=FindingCategory.CORRUPT,
            severity=Severity.ERROR,
            subject=EvidenceRef(source.content_id, (ByteRange(0, source.size),)),
            transform=config.transform,
            message="the source holds a line that is not two integers",
        )
        return ChunkOutput(output.records, output.series, (echo,))


class BumpedTally(TALLY.TallyAdapter):  # type: ignore[misc, name-defined]
    """The same tally code released as 1.0.1."""

    descriptor = replace(TALLY.DESCRIPTOR, version="1.0.1")


def registry(*extra: Any, tally: Any = None) -> AdapterRegistry:
    chosen = tally if tally is not None else TALLY.TallyAdapter(rows_per_chunk=2)
    return AdapterRegistry([*builtin_adapters(), chosen, HOSTILE.HostileAdapter(), *extra])


class Job:
    """One sandboxed job's outcome, every event, and the package read back."""

    def __init__(
        self,
        root: Path,
        home: Path,
        destination: Path,
        adapters: AdapterRegistry,
        options: JobOptions | None = None,
    ) -> None:
        self.events: list[JobEvent] = []
        self.workspace = Workspace(home)
        job = IngestJob(
            root, destination, self.workspace, adapters, options, on_event=self.events.append
        )
        self.outcome: JobOutcome = job.run()
        assert self.outcome.state is JobState.COMMITTED
        self.package: Any = read_package(destination)

    def of(self, kind: str) -> list[JobEvent]:
        return [event for event in self.events if event.kind == kind]

    def codes(self) -> list[str]:
        return sorted(finding.code for finding in self.outcome.findings)

    def findings(self, code: str) -> list[IngestFinding]:
        return [finding for finding in self.outcome.findings if finding.code == code]

    def chunks(self, kind: str) -> set[str]:
        return {str(event.details["chunk"]) for event in self.of(kind)}

    def source(self, path: str) -> str:
        return str(
            next(
                e.details["source"]
                for e in self.of("source_hashed")
                if e.details["location"] == {"kind": "local", "path": path}
            )
        )

    def evidence(self) -> set[str]:
        """The ids of every record an adapter made: what a cache hit must reproduce."""
        return {r.id for r in self.package.records if getattr(r, "kind", "") not in _LEDGER}


_LEDGER: Final = frozenset({"source_artifact", "source_revision", "source_absence"})


def children_of(pid: int) -> list[int]:
    found: list[int] = []
    tasks = Path(f"/proc/{pid}/task")
    for task in tasks.iterdir() if tasks.exists() else ():
        try:
            found += [int(child) for child in (task / "children").read_text().split()]
        except OSError:
            continue
    return found


# --- Kill and resume in the middle of a sandboxed parse ------------------------------------------


@pytest.mark.slow
def test_a_job_killed_mid_parse_resumes_to_the_clean_package(tmp_path: Path) -> None:
    """SIGKILL the job while a sandboxed ``ingest`` of a multi-chunk source is running: the call
    dies with it, nothing of that chunk is kept, the call's scratch directory is swept by the next
    job, which reuses every committed chunk and builds exactly the package a clean run builds."""
    root = tmp_path / "root"
    root.mkdir()
    lines = ("one", "two", "three", "nap", "five", "six")
    (root / "long.hostile").write_bytes(HOSTILE.hostile(*lines))
    (root / "lift.tally").write_bytes(TALLY_MAGIC + b"".join(b"%d %d\n" % (t, t) for t in range(9)))
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    home, script = tmp_path / "home", FIXTURES / "runtime" / "sandboxed_job.py"
    victim = subprocess.Popen([sys.executable, str(script), str(root), str(home), "x", "600"])
    napping = None
    try:
        seen: dict[int, float] = {}
        deadline = time.monotonic() + 60
        while napping is None and time.monotonic() < deadline:
            now = time.monotonic()
            for pid in children_of(victim.pid):
                seen.setdefault(pid, now)
                if now - seen[pid] > 0.5:  # every other call lasts milliseconds
                    napping = pid
            time.sleep(0.02)
        assert napping is not None, "the job never reached the slow chunk"
    finally:
        victim.kill()
        victim.wait()
    deadline = time.monotonic() + 10
    while Path(f"/proc/{napping}").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not Path(f"/proc/{napping}").exists()  # the call died with the job
    assert not (tmp_path / "x").exists()
    kept = set(Workspace(home).chunks())
    assert kept  # chunks before the slow one were committed
    left = [p for p in (home / "scratch").iterdir()]
    assert len(left) == 1  # the killed call's scratch directory, its lock released by the kill

    options = JobOptions(attempts=1, limits=Limits(wall_seconds=600))
    resumed = Job(root, home, tmp_path / "resumed", registry(), options)
    (swept,) = resumed.of("workspace_swept")
    assert swept.details["scratch"] == 1
    assert kept <= resumed.chunks("chunk_skipped")
    assert not kept & resumed.chunks("chunk_committed")
    napped = resumed.chunks("chunk_committed") - kept
    assert napped  # the slow chunk, and every chunk after it, parsed by the resuming job
    clean = Job(root, tmp_path / "clean-home", tmp_path / "clean", registry(), options)
    assert resumed.outcome.package == clean.outcome.package
    assert resumed.outcome.findings == () and len(resumed.outcome.ingested) == 3
    assert list((home / "scratch").iterdir()) == [] and list((home / "staging").iterdir()) == []


# --- A parser crash inside the sandbox -----------------------------------------------------------


def test_a_crash_inside_the_sandbox_costs_one_chunk_and_the_rerun_retries_only_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "crashy.hostile").write_bytes(HOSTILE.hostile("a", "b", "segfault", "d"))
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    first = Job(root, tmp_path / "home", tmp_path / "first", registry())
    (crash,) = first.findings("neptune.runtime.adapter_crashed")
    assert crash.details["signal"] == "SIGSEGV" and crash.details["attempts"] == 2
    crashy = first.source("crashy.hostile")
    committed = [e for e in first.of("chunk_committed") if e.details["source"] == crashy]
    assert len(committed) == 4  # the document, a, b and d: one chunk lost, not the source's work
    assert len(first.outcome.ingested) == 1  # the notes landed

    again = Job(root, tmp_path / "home", tmp_path / "again", registry())
    assert again.outcome.cache.calls.ingest == 2  # the crashed chunk's two attempts, nothing else
    assert again.outcome.findings == first.outcome.findings
    assert again.outcome.package == first.outcome.package


# --- An adapter whose chunks half carry findings -------------------------------------------------


def test_findings_on_half_the_chunks_land_with_everything_else(tmp_path: Path) -> None:
    """A chunk whose output is findings alone is an output like any other: committed, reused,
    and the source admitted. One finding per affected line, never per chunk or per source."""
    root = tmp_path / "root"
    root.mkdir()
    rows = b"".join(b"%d %d\n" % (t, t) if t % 2 == 0 else b"oops\n" for t in range(10))
    (root / "half.tally").write_bytes(TALLY_MAGIC + rows)
    adapters = registry(tally=TALLY.TallyAdapter(rows_per_chunk=1))
    first = Job(root, tmp_path / "home", tmp_path / "first", adapters)
    found = [r for r in first.package.records if isinstance(r, IngestFinding)]
    assert sorted(f.code for f in found) == ["tally.bad_row"] * 5 and first.codes() == []
    assert len({f.subject for f in found}) == 5  # each cites its own line
    assert len(first.outcome.ingested) == 1
    (series,) = first.package.series.values()
    assert pq.ParquetFile(series).metadata.num_rows == 5  # the five good lines
    findings_only = [
        e for e in first.of("chunk_committed") if e.details["findings"] and not e.details["rows"]
    ]
    assert len(findings_only) == 5
    again = Job(root, tmp_path / "home", tmp_path / "again", adapters)
    assert again.outcome.cache.calls.ingest == 0 and again.outcome.package == first.outcome.package


def test_the_same_finding_from_two_chunks_quarantines_its_source_and_says_which(
    tmp_path: Path,
) -> None:
    """Law 9 at work: a finding about the whole source emitted by every chunk with a bad row is
    one output emitted twice. The source is quarantined with the chunks named; the rest lands."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "echo.tally").write_bytes(TALLY_MAGIC + b"1 1\noops\n3 3\noops\n")
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    run = Job(root, tmp_path / "home", tmp_path / "p", registry(tally=EchoingTally(1)))
    (invalid,) = run.findings("neptune.runtime.output_invalid")
    problems: Any = invalid.details["problems"]
    (problem,) = problems
    assert problem["law"] == "finding_repeated" and str(problem["chunk"]).startswith("chunk:")
    assert len(run.outcome.ingested) == 1  # the notes


# --- Two adapters, one extensionless file, equal confidence --------------------------------------


def test_two_adapters_tied_on_an_extensionless_file_are_a_finding_never_a_guess(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "renamed").write_bytes(TALLY_MAGIC + b"7 70\n")
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    first = Job(root, tmp_path / "home", tmp_path / "first", registry(Rival()))
    (tie,) = first.findings("neptune.probe.ambiguous")
    assert tie.details["adapters"] == ["rival", "tally"]
    assert tie.details["confidence"] == SIGNATURE
    assert tie.details["reasons"] == {"rival": ["rival.magic"], "tally": ["tally.magic"]}
    (event,) = first.of("source_ambiguous")
    assert event.details["adapters"] == ["rival", "tally"]
    assert not [e for e in first.of("source_planned") if e.details["source"] == tie.subject.source]  # type: ignore[union-attr]
    reader = {s.location.to_json()["path"]: s.read_by for s in first.package.receipt.sources}
    (engine,) = [t for t in first.package.receipt.transforms if t.adapter_id == "neptune.probe"]
    assert reader["renamed"] == (engine.id,)  # looked at by the probe engine, read by nobody
    again = Job(root, tmp_path / "fresh", tmp_path / "again", registry(Rival()))
    assert again.outcome.package == first.outcome.package  # the tie is reported the same way

    alone = Job(root, tmp_path / "alone", tmp_path / "alone-p", registry())
    assert alone.codes() == [] and len(alone.outcome.ingested) == 2  # the bytes decide


# --- The cache: a rename, a version bump, a config change ----------------------------------------


@pytest.fixture
def site(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    (root / "lift.tally").write_bytes(TALLY_MAGIC + b"".join(b"%d %d\n" % (t, t) for t in range(9)))
    return root


def test_a_renamed_source_is_a_cache_hit(site: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = Job(site, home, tmp_path / "first", registry())
    (site / "archive").mkdir()
    (site / "lift.tally").rename(site / "archive" / "2026-10-01-lift.dat")
    moved = Job(site, home, tmp_path / "moved", registry())
    calls = moved.outcome.cache.calls
    assert (calls.plan, calls.ingest) == (0, 0)  # content identity: the name changed nothing
    assert {s.plan.rule for s in moved.outcome.cache.sources} == {"planned"}
    assert moved.outcome.cache.totals()["chunks"]["miss"] == 0
    assert moved.outcome.cache.totals()["derivatives"]["miss"] == 0
    assert moved.evidence() == first.evidence()  # the same records, cited to the same bytes
    assert len(moved.of("source_absent")) == 1  # the old location is recorded as gone


def test_an_adapter_version_bump_misses_exactly_its_own_chunks(site: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = Job(site, home, tmp_path / "first", registry())
    bumped = Job(site, home, tmp_path / "bumped", registry(tally=BumpedTally(rows_per_chunk=2)))
    plans = {s.adapter: s.plan for s in bumped.outcome.cache.sources}
    assert plans["tally"].rule == "transform_changed" and plans["tally"].changed == (
        "adapter_version",
    )
    assert plans["text"].rule == "planned"
    tally_chunks = next(s for s in bumped.outcome.cache.sources if s.adapter == "tally").chunks
    assert bumped.outcome.cache.calls.ingest == len(tally_chunks)  # tally's, and only tally's
    missed = {d.recipe for d in bumped.outcome.cache.derivatives if d.cache == "miss"}
    held = {d.recipe for d in bumped.outcome.cache.derivatives if d.cache == "hit"}
    assert missed == {"neptune.runtime.admission/1", "neptune.store.series/1"}  # tally's
    assert held == {"neptune.runtime.admission/1"}  # the text's verdict
    assert bumped.evidence() != first.evidence()  # new lineage: new record ids (ADR 0003)


def test_a_config_change_invalidates_exactly_one_derivative(site: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    Job(site, home, tmp_path / "first", registry())
    config = {"text": {"block_rule": "line"}}
    changed = Job(site, home, tmp_path / "changed", registry(), JobOptions(config=config))
    plans = {s.adapter: s.plan for s in changed.outcome.cache.sources}
    assert (plans["text"].rule, plans["text"].changed) == ("transform_changed", ("config",))
    assert plans["tally"].rule == "planned"
    derivatives = changed.outcome.cache.derivatives
    missed = [d for d in derivatives if d.cache == "miss"]
    assert len(missed) == 1 and missed[0].recipe == "neptune.runtime.admission/1"
    (owner,) = missed[0].owners
    assert owner[0] == changed.source("notes.txt")
    assert {d.recipe for d in derivatives if d.cache == "hit"} == {
        "neptune.runtime.admission/1",
        "neptune.store.series/1",
    }  # the tally's verdict and series file, untouched


# --- The hostile fixture suite through a real job ------------------------------------------------


class OpenWatch:
    """Every open and read, by any process, of ``directory`` or a file in it (Linux inotify).

    The kernel reports the job's own opens and its sandboxed calls' alike, whatever path or
    descriptor they went through: a symlink followed into ``directory`` is an open there. Opens
    with ``O_PATH`` and ``stat`` read nothing and are not reported.
    """

    IN_ACCESS: Final = 0x001
    IN_OPEN: Final = 0x020
    _EVENT: Final = struct.Struct("iIII")  # wd, mask, cookie, len; then len bytes of name

    def __init__(self, directory: Path) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        self._fd: int = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self._fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        mask = self.IN_OPEN | self.IN_ACCESS
        if libc.inotify_add_watch(self._fd, os.fsencode(directory), mask) < 0:
            os.close(self._fd)
            raise OSError(ctypes.get_errno(), "inotify_add_watch")

    def seen(self) -> list[tuple[str, str]]:
        """``(event, name)`` for each open or read since the last call; ``name`` is ``""`` for the
        directory itself."""
        found: list[tuple[str, str]] = []
        while True:
            try:
                data = os.read(self._fd, 64 * 1024)
            except BlockingIOError:
                return found
            offset = 0
            while offset < len(data):
                _, mask, _, length = self._EVENT.unpack_from(data, offset)
                start = offset + self._EVENT.size
                name = data[start : start + length].rstrip(b"\0").decode()
                found.append(("open" if mask & self.IN_OPEN else "read", name))
                offset = start + length

    def close(self) -> None:
        os.close(self._fd)


def listing(root: Path) -> list[tuple[str, int, bytes]]:
    rows = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        data = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        rows.append((path.relative_to(root).as_posix(), info.st_mode, data))
    return rows


def test_the_hostile_suite_through_a_real_job(hostile: ModuleType, tmp_path: Path) -> None:
    """Every hostile fixture (escaping and looping links, a FIFO, odd names, a deep tree, archive
    bombs, traversal names, truncated and corrupt archives) through the job with the sandbox: the
    job commits, the tree is untouched, nothing outside it is opened or read by the job or any of
    its calls (watched by the kernel), the canary is in no package, every refusal is a finding,
    benign files land, and a second job in another workspace writes the same package."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    hostile.build_tree(root, outside)
    before = listing(root)
    watch = OpenWatch(outside)
    try:
        first = Job(root, tmp_path / "home", tmp_path / "first", registry())
        assert watch.seen() == []  # the canary and its directory were never opened
        second = Job(root, tmp_path / "other", tmp_path / "second", registry())
        assert watch.seen() == []
        assert (outside / "canary.txt").read_bytes() == hostile.CANARY
        assert ("open", "canary.txt") in watch.seen()  # the watch does see an open there
    finally:
        watch.close()
    assert listing(root) == before
    sources = {s.content_id for s in first.package.receipt.sources}
    assert content_id(hostile.CANARY) not in sources
    codes = first.codes()
    assert codes.count("neptune.discovery.symlink_not_followed") == 11
    assert codes.count("neptune.discovery.special_file") == 1
    unsupported = first.findings("neptune.probe.unsupported")
    archives = {first.source(f"archives/{name}") for name in hostile.FIXTURES}
    assert archives <= {f.subject.source for f in unsupported}  # type: ignore[union-attr]
    assert "neptune.probe.container_limit" in codes  # the bombs and the many members
    assert not [c for c in codes if c.startswith("neptune.runtime.")]  # no call failed
    read = {s.location.to_json()["path"] for s in first.package.receipt.sources if s.read_by}
    assert {"benign.txt", "deep/" + "d/" * hostile.DEEP + "leaf.txt"} <= read
    home = tmp_path / "home"
    assert list((home / "scratch").iterdir()) == [] and list((home / "staging").iterdir()) == []
    assert second.outcome.package == first.outcome.package


# --- Archives: the probe's listing and the hardening inspector (ADR 0032) -------------------------


def nested_archive() -> bytes:
    """A zip holding a 2 MiB zip of incompressible bytes: inspecting it spools the inner zip."""
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("frames.bin", random.Random(57).randbytes(2 * 1024 * 1024))
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("inner.zip", inner.getvalue())
    return outer.getvalue()


def test_the_probe_lists_within_its_budget_while_the_inspector_reads_everything() -> None:
    """Why the two archive passes stay apart (ADR 0032): the probe's listing reads a bounded
    budget whatever the archive holds, the hardening inspector inflates every member."""
    data = nested_archive()

    class Counting(BytesReader):
        served = 0

        def read(self, offset: int, length: int) -> bytes:
            piece = super().read(offset, length)
            Counting.served += len(piece)
            return piece

    probed = ProbeEngine(registry()).probe(Counting(data), "bundle.zip")
    assert probed.container is not None and probed.container.complete
    assert Counting.served < 512 * 1024 < len(data)  # heads and directories, not the frames
    counted = Counting(data)
    with tempfile.TemporaryDirectory() as scratch:
        report = inspect_archive(
            io.BytesIO(data), source=counted.content_id, size=len(data), scratch=Path(scratch)
        )
    (member,) = report.members
    assert member.nested is not None and member.nested.members[0].read_bytes == 2 * 1024 * 1024


@pytest.mark.skipif(not confine.landlock_abi(), reason="no Landlock: calls get no scratch")
def test_the_archive_inspector_runs_in_a_sandboxed_call_through_its_scratch(
    tmp_path: Path,
) -> None:
    """What an M4 archive adapter will do inside ``plan``: inspect a nested archive in the
    sandbox, spooling the inner archive through the call's scratch directory, and nowhere else."""
    data = nested_archive()
    source = BytesReader(data).content_id
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)

    def inspect() -> str:
        directory = scratch_directory()
        assert directory is not None
        report = inspect_archive(io.BytesIO(data), source=source, size=len(data), scratch=directory)
        (member,) = report.members
        assert member.nested is not None
        spooled = sorted(p.name for p in directory.iterdir())  # the spool is gone by now
        return json.dumps(
            {
                "complete": report.complete,
                "findings": [f.code for f in report.findings],
                "inner": [m.name for m in member.nested.members],
                "left": spooled,
            }
        )

    box = Subprocess(Limits(cpu_seconds=20, wall_seconds=40))
    outcome = box.call(inspect, Codec(str, str.encode, bytes.decode), scratch=scratch)
    assert isinstance(outcome, Returned), outcome
    assert json.loads(outcome.value) == {
        "complete": True,
        "findings": [],
        "inner": ["frames.bin"],
        "left": [],
    }


def test_the_archive_measurement_script_runs_small(tmp_path: Path) -> None:
    """``stress_archive_passes.py``, whose 64 and 256 MiB runs are the gate's recorded figures, at
    4 and 8 MiB: the listing reads the same bytes at either size, the inspector inflates all."""
    script = FIXTURES / "runtime" / "stress_archive_passes.py"
    done = subprocess.run(
        [sys.executable, str(script), str(tmp_path), "4", "8"],
        check=True,
        capture_output=True,
        timeout=300,
    )
    measured = json.loads(done.stdout)
    small, large = measured["4"], measured["8"]
    assert (
        small["listing_bytes"] == large["listing_bytes"] < 2 * 1024 * 1024 < small["archive_bytes"]
    )
    for mib, run in ((4, small), (8, large)):
        assert run["inflated_bytes"] == mib * 1024 * 1024 and run["inspect_complete"]
    assert list(tmp_path.iterdir()) == []  # the generated archives are removed


# --- A large source, measured --------------------------------------------------------------------


def measure(tmp_path: Path, gib: int) -> dict[str, Any]:
    """Run the measurement script in a process of its own, so its peak memory is its own."""
    script = FIXTURES / "runtime" / "stress_large_source.py"
    done = subprocess.run(
        [sys.executable, str(script), str(tmp_path), str(gib)],
        check=True,
        capture_output=True,
        timeout=600,
    )
    measured: dict[str, Any] = json.loads(done.stdout)
    assert not (tmp_path / "run").exists()  # the script removes the source it generated
    return measured


@pytest.mark.slow
def test_a_large_sparse_source_is_inspected_cheaply_and_planned_small(tmp_path: Path) -> None:
    """The measurement script at 1 GiB (the gate records larger runs): probing reads the head and
    nothing else, the plan is kilobytes, memory stays flat, and a rerun calls no adapter."""
    measured = measure(tmp_path, 1)
    assert measured["sources"] == 1 and measured["state"] == "committed"
    assert measured["inspect_seconds"] < 2.0  # the head and the engine's call, not the bytes
    assert measured["adapter_inspect_seconds"] < 2.0
    assert measured["plan_bytes"] < 64 * 1024 and measured["chunks"] == 17
    assert measured["parent_peak_rss_mib"] < 512 and measured["child_peak_rss_mib"] < 512
    rerun = measured["rerun"]
    # A probe call per registered adapter for the one source: the built-ins and the frame log.
    probes = len(builtin_adapters()) + 1
    assert rerun["calls"] == {"ingest": 0, "plan": 0, "probe": probes} and rerun["same_package"]


def test_the_measurement_script_runs_small(tmp_path: Path) -> None:
    measured = measure(tmp_path, 0)
    assert measured["state"] == "committed" and measured["chunks"] == 2
    assert measured["rerun"]["chunk_hits"] == 2
