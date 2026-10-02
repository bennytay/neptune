"""MVL-6 acceptance: one corrupt source does not invalidate unrelated successfully ingested sources.

Corrupt comes in degrees. A source whose adapter copes (bad rows, invalid UTF-8) lands with the
adapter's findings. A source whose adapter crashes, cannot plan, or breaks the contract is
quarantined with the runtime's finding and nothing else is touched. A file that changes under the
job, or cannot be opened, is reported the same way. The ``brittle`` fixture adapter supplies the
crashes on demand; ``tally`` and ``text`` supply ordinary corruption.
"""

import importlib.util
import os
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.contract import (
    AdapterConfig,
    Chunk,
    ChunkOutput,
    Plan,
    SourceReader,
    make_chunk,
)
from neptune.adapters.registry import AdapterRegistry
from neptune.discovery.verify import verify_artifact
from neptune.identity.hashing import content_id
from neptune.model.finding import FindingCategory, Severity, subject_to_json
from neptune.model.knowledge import Known
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.series import ColumnType, SeriesBatch, SeriesColumn
from neptune.model.world import DocumentBlock, DocumentRecord
from neptune.runtime import IngestJob, Isolation, JobError, JobEvent, JobOptions, JobState, Phase
from neptune.runtime import job as job_module
from neptune.runtime import lineage as runtime_lineage
from neptune.store.package import read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / "adapters" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TALLY: Final = _load("tally_adapter")
BRITTLE: Final = _load("brittle_adapter")


def registry() -> AdapterRegistry:
    return AdapterRegistry(
        [*builtin_adapters(), TALLY.TallyAdapter(rows_per_chunk=2), BRITTLE.BrittleAdapter()]
    )


def run(
    root: Path,
    tmp_path: Path,
    adapters: AdapterRegistry | None = None,
    *,
    options: JobOptions | None = None,
    on_event: Callable[[JobEvent], None] | None = None,
    name: str = "package",
) -> tuple[Any, Any, list[JobEvent]]:
    """Run a job over ``root``; return its outcome, the package read back, and every event."""
    seen: list[JobEvent] = []

    def record(event: JobEvent) -> None:
        seen.append(event)
        if on_event is not None:
            on_event(event)

    job = IngestJob(
        root,
        tmp_path / name,
        Workspace(tmp_path / "home"),
        adapters or registry(),
        options,
        on_event=record,
    )
    outcome = job.run()
    assert outcome.state is JobState.COMMITTED
    return outcome, read_package(tmp_path / name), seen


def codes(package: Any) -> list[str]:
    """The receipt's codes but validation's (ADR 0054): these tests pin what the runtime says."""
    return sorted(
        finding.code
        for finding in package.receipt.findings
        if not finding.code.startswith("neptune.validate.")
    )


def path_of(details: Any) -> str:
    location = details["location"]
    assert isinstance(location, dict)
    return str(location["path"])


def codes_of(details: Any) -> tuple[str, ...]:
    found = details["codes"]
    assert isinstance(found, list)
    return tuple(str(code) for code in found)


def read_by(package: Any) -> dict[str, int]:
    return {str(s.location.to_json()["path"]): len(s.read_by) for s in package.receipt.sources}


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    shutil.copy(FIXTURES / "text" / "corrupted.txt", root / "corrupted.txt")
    (root / "lift.tally").write_bytes(b"TALLY1\n10 1\n20 2\noops\n40 4\n50 5\n")
    (root / "crash.brittle").write_bytes(BRITTLE.brittle("first", "crash", "last"))
    (root / "unplannable.brittle").write_bytes(BRITTLE.brittle("plan-crash", "x"))
    (root / "blob.bin").write_bytes(b"\x00\x01binary\x00")
    return root


def test_one_crashing_source_does_not_invalidate_the_others(corpus: Path, tmp_path: Path) -> None:
    outcome, package, seen = run(corpus, tmp_path)
    assert codes(package) == [
        "neptune.probe.unsupported",
        "neptune.runtime.chunk_failed",
        "neptune.runtime.plan_failed",
        "tally.bad_row",
        "text.invalid_utf8",
    ]
    assert read_by(package) == {
        "blob.bin": 1,  # by the probe engine, which says no adapter claims it
        "corrupted.txt": 1,
        "crash.brittle": 1,  # read by the runtime, which says why nothing came of it
        "lift.tally": 1,
        "notes.txt": 1,
        "unplannable.brittle": 1,
    }
    transforms = {t.id: t.adapter_id for t in package.receipt.transforms}
    for source in package.receipt.sources:
        if source.location.to_json()["path"].endswith(".brittle"):
            assert [transforms[t] for t in source.read_by] == [runtime_lineage.RUNTIME_ID]
    assert len(outcome.ingested) == 3
    documents = [r for r in package.records if isinstance(r, DocumentRecord)]
    assert len(documents) == 2  # notes and corrupted; nothing of either brittle file
    assert not any(
        isinstance(r, DocumentBlock)
        and isinstance(r.text, Known)
        and r.text.value in ("first", "last")
        for r in package.records
    )
    failed = [f for f in package.receipt.findings if f.code == "neptune.runtime.chunk_failed"]
    assert [(f.category, f.severity) for f in failed] == [(FindingCategory.FAILED, Severity.ERROR)]
    assert "RuntimeError" in failed[0].message and "crash" not in failed[0].message

    quarantined = {codes_of(e.details) for e in seen if e.kind == "source_quarantined"}
    assert quarantined == {("neptune.runtime.chunk_failed",), ("neptune.runtime.plan_failed",)}
    assert len([e for e in seen if e.kind == "source_admitted"]) == 3
    # The crashing source's other chunks were committed: a fixed adapter version redoes one chunk.
    crashed = next(e.details["source"] for e in seen if e.kind == "chunk_failed")
    committed = [e for e in seen if e.kind == "chunk_committed" and e.details["source"] == crashed]
    assert len(committed) == 3  # the document, "first" and "last"
    retried = [e for e in seen if e.kind == "chunk_retried"]
    assert len(retried) == 1 and retried[0].details["error"] == "RuntimeError"


def test_a_transient_fault_is_retried_and_the_source_lands(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "wobbly.brittle").write_bytes(BRITTLE.brittle("x", "flaky", "y"))
    # The fault counter lives on the adapter instance, which only an in-process call updates:
    # a sandboxed call runs in a fresh child each attempt and never sees the first one.
    trusted = JobOptions(attempts=2, isolation=Isolation.IN_PROCESS)
    outcome, package, seen = run(root, tmp_path, options=trusted)
    assert codes(package) == []
    assert len(outcome.ingested) == 1
    retried = [e for e in seen if e.kind == "chunk_retried"]
    assert [(e.details["attempt"], e.details["error"]) for e in retried] == [(1, "OSError")]
    parsed = [e for e in seen if e.kind == "chunk_parsed" and e.details["attempt"] == 2]
    assert len(parsed) == 1
    texts = sorted(
        r.text.value
        for r in package.records
        if isinstance(r, DocumentBlock) and isinstance(r.text, Known)
    )
    assert texts == ["flaky", "x", "y"]


def test_without_a_retry_the_same_fault_quarantines_the_source(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "wobbly.brittle").write_bytes(BRITTLE.brittle("x", "flaky", "y"))
    outcome, package, seen = run(root, tmp_path, options=JobOptions(attempts=1))
    assert codes(package) == ["neptune.runtime.chunk_failed"]
    assert outcome.ingested == ()
    (finding,) = package.receipt.findings
    assert "after 1 attempt (OSError at ingest)" in finding.message
    assert not [e for e in seen if e.kind == "chunk_retried"]


def test_output_that_breaks_the_contract_is_a_finding_not_a_crash(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "wrong-id.brittle").write_bytes(BRITTLE.brittle("ok", "bad-output"))
    (root / "twice.brittle").write_bytes(BRITTLE.brittle("ok", "dup"))
    (root / "fine.brittle").write_bytes(BRITTLE.brittle("ok", "fine"))
    outcome, package, seen = run(root, tmp_path)
    assert codes(package) == ["neptune.runtime.chunk_failed", "neptune.runtime.output_invalid"]
    assert len(outcome.ingested) == 1
    by_code = {f.code: f for f in outcome.findings}
    wrong = by_code["neptune.runtime.chunk_failed"]
    assert wrong.details["error"] == "ContractError"
    assert wrong.details["step"] == "check_chunk_output"
    assert wrong.details["attempts"] == 1  # a contract violation is a bug: never retried
    twice = by_code["neptune.runtime.output_invalid"]
    problems = twice.details["problems"]
    assert isinstance(problems, list) and len(problems) == 1
    assert isinstance(problems[0], dict) and problems[0]["law"] == "record_repeated"
    assert problems[0]["kind"] == "document_block"
    assert "(record_repeated)" in twice.message
    assert not [e for e in seen if e.kind == "chunk_retried"]


class OverlappingTally(TALLY.TallyAdapter):  # type: ignore[misc, name-defined]
    """A tally adapter with a planning bug: every chunk numbers its rows from 0."""

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        planned = super().plan(source, config)
        chunks = [planned.chunks[0]]
        for chunk in planned.chunks[1:]:
            context = {**chunk.context, "first": 0}
            chunks.append(make_chunk(source, config, context, chunk.cost))
        return Plan(tuple(chunks), planned.findings)


def test_seq_repeated_across_chunks_is_caught_without_holding_every_seq(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.tally").write_bytes(b"TALLY1\n10 1\n20 2\n30 3\n40 4\n")
    (root / "b.tally").write_bytes(b"TALLY1\n10 1\n20 2\n")  # one chunk of rows: nothing overlaps
    adapters = AdapterRegistry([OverlappingTally(rows_per_chunk=2)])
    outcome, package, _ = run(root, tmp_path, adapters)
    assert codes(package) == ["neptune.runtime.output_invalid"]
    assert len(outcome.ingested) == 1
    (finding,) = outcome.findings
    assert "(seq_ranges_overlap)" in finding.message
    (problem,) = finding.details["problems"]
    assert problem["law"] == "seq_ranges_overlap"
    assert problem["seq"] == 0 and len(problem["chunks"]) == 2


def test_a_source_that_changes_during_the_job_is_reported_and_the_rest_proceed(
    corpus: Path, tmp_path: Path
) -> None:
    def rewrite_notes_after_planning(event: JobEvent) -> None:
        if event.kind == "phase_finished" and event.phase is Phase.PLAN:
            data = (corpus / "notes.txt").read_bytes()
            (corpus / "notes.txt").write_bytes(data[:-1] + b"!")  # same size, other bytes

    outcome, package, seen = run(corpus, tmp_path, on_event=rewrite_notes_after_planning)
    assert "neptune.runtime.source_changed" in codes(package)
    # Not read by its adapter: only discovery's verification cites it, naming the changed chunk.
    assert read_by(package)["notes.txt"] == 1
    (verified,) = [f for f in outcome.findings if f.code == "neptune.discovery.chunk_changed"]
    assert verified.details == {"chunk_size": 8 * 1024 * 1024, "first_chunk": 0, "last_chunk": 0}
    assert read_by(package)["corrupted.txt"] == 1 and read_by(package)["lift.tally"] == 1
    changed = [e for e in seen if e.kind == "source_changed"]
    assert [path_of(e.details) for e in changed] == ["notes.txt"]
    finding = next(f for f in outcome.findings if f.code == "neptune.runtime.source_changed")
    assert subject_to_json(finding.subject) == {"kind": "local", "path": "notes.txt"}
    assert finding.category is FindingCategory.INCONSISTENT


def test_entries_that_cannot_be_read_are_findings_by_reason(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    os.mkfifo(root / "pipe")
    (root / "latest").symlink_to("notes.txt")
    outcome, package, seen = run(root, tmp_path)
    # Discovery, which saw both entries, says why it read neither (ADR 0029 §1, ADR 0033 §3).
    assert codes(package) == [
        "neptune.discovery.special_file",
        "neptune.discovery.symlink_not_followed",
    ]
    special, link = sorted(outcome.findings, key=lambda f: f.code)
    assert special.severity is Severity.INFO and special.category is FindingCategory.SKIPPED
    assert subject_to_json(special.subject) == {"kind": "local", "path": "pipe"}
    assert subject_to_json(link.subject) == {"kind": "local", "path": "latest"}
    assert link.details == {"absolute": False, "inside_root": True, "target": "notes.txt"}
    assert [e.details for e in seen if e.kind == "symlink_recorded"] == [
        {"location": {"kind": "local", "path": "latest"}, "target_hex": b"notes.txt".hex()}
    ]
    assert read_by(package) == {"notes.txt": 1}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_an_unreadable_file_is_an_error_finding_and_the_rest_proceed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    secret = root / "secret.txt"
    secret.write_bytes(b"restricted\n")
    secret.chmod(0)
    try:
        outcome, package, _ = run(root, tmp_path)
    finally:
        secret.chmod(0o644)
    assert codes(package) == ["neptune.discovery.unreadable"]
    (finding,) = outcome.findings
    assert finding.severity is Severity.ERROR and finding.category is FindingCategory.SKIPPED
    assert subject_to_json(finding.subject) == {"kind": "local", "path": "secret.txt"}
    assert read_by(package) == {"notes.txt": 1}  # the unreadable file was never fingerprinted


class _RowBugTally(TALLY.TallyAdapter):  # type: ignore[misc, name-defined]
    """A tally adapter that rewrites one column of every non-empty batch: a series bug that no
    single chunk's check can see, since the stream is declared in another chunk."""

    column: str = ""
    kind: ColumnType | None = None

    def convert(self, value: Any) -> Any:
        return value

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        output = super().ingest(source, chunk, config)
        batches = []
        for batch in output.series:
            if batch.length:
                batch = SeriesBatch(
                    batch.stream,
                    tuple(
                        SeriesColumn(
                            c.name,
                            self.kind or c.type,
                            tuple(self.convert(v) for v in c.values),
                        )
                        if c.name == self.column
                        else c
                        for c in batch.columns
                    ),
                )
            batches.append(batch)
        return ChunkOutput(output.records, tuple(batches), output.findings)


class DriftingTally(_RowBugTally):
    """Rows carry ``value/value`` as float64, the header's empty batch as int64."""

    column, kind = "value/value", ColumnType.FLOAT64

    def convert(self, value: Any) -> Any:
        return float(value)


class MisplacedTally(_RowBugTally):
    """Every row's locator offset is negative: its provenance cannot be rebuilt."""

    column = "locator/0/offset"

    def convert(self, value: Any) -> Any:
        return -1 - value


@pytest.mark.parametrize(
    ("adapter", "law"),
    [(DriftingTally, "run_columns_disagree"), (MisplacedTally, "run_breaks_stream")],
)
def test_one_sources_broken_series_is_a_finding_and_the_rest_land(
    tmp_path: Path, adapter: type, law: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    (root / "lift.tally").write_bytes(b"TALLY1\n10 1\n20 2\n30 3\n")
    adapters = AdapterRegistry([*builtin_adapters(), adapter(rows_per_chunk=2)])
    outcome, package, seen = run(root, tmp_path, adapters)
    assert codes(package) == ["neptune.runtime.output_invalid"]
    assert len(outcome.ingested) == 1  # the notes
    assert read_by(package) == {"lift.tally": 1, "notes.txt": 1}
    (finding,) = outcome.findings
    assert f"({law})" in finding.message
    problems = finding.details["problems"]
    assert {p["law"] for p in problems} == {law}
    assert not package.series  # the tally's stream is not in the package
    assert [e.details["codes"] for e in seen if e.kind == "source_quarantined"] == [
        ["neptune.runtime.output_invalid"]
    ]


class SplitTally(TALLY.TallyAdapter):  # type: ignore[misc, name-defined]
    """Each chunk's rows come as two batches of the stream, the second with a column more."""

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        output: ChunkOutput = super().ingest(source, chunk, config)
        if not output.series or output.series[0].length < 2:
            return output
        (batch,) = output.series
        head = SeriesBatch(
            batch.stream, tuple(replace(c, values=c.values[:1]) for c in batch.columns)
        )
        tail = SeriesBatch(
            batch.stream,
            (
                *(replace(c, values=c.values[1:]) for c in batch.columns),
                SeriesColumn("value/extra", ColumnType.INT8, (0,) * (batch.length - 1)),
            ),
        )
        return ChunkOutput(output.records, (head, tail), output.findings)


def test_batches_of_one_chunk_that_disagree_fail_that_chunk_not_the_job(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    (root / "lift.tally").write_bytes(b"TALLY1\n10 1\n20 2\n")
    adapters = AdapterRegistry([*builtin_adapters(), SplitTally(rows_per_chunk=2)])
    outcome, package, _ = run(root, tmp_path, adapters)
    assert codes(package) == ["neptune.runtime.chunk_failed"]
    (finding,) = outcome.findings
    assert finding.details["error"] == "ContractError" and finding.details["attempts"] == 1
    assert finding.details["step"] == "chunk_series"
    assert finding.details["law"] == "batch_columns_disagree"
    assert len(outcome.ingested) == 1  # the notes


class SilentTally(TALLY.TallyAdapter):  # type: ignore[misc, name-defined]
    """Returns nothing at all, not a ``ChunkOutput``, for every chunk that holds rows."""

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> Any:
        output: ChunkOutput = super().ingest(source, chunk, config)
        return None if output.series and output.series[0].length else output


def test_ingest_returning_the_wrong_type_fails_that_chunk_not_the_job(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    (root / "lift.tally").write_bytes(b"TALLY1\n10 1\n20 2\n")
    adapters = AdapterRegistry([*builtin_adapters(), SilentTally(rows_per_chunk=2)])
    outcome, package, seen = run(root, tmp_path, adapters)
    assert codes(package) == ["neptune.runtime.chunk_failed"]
    (finding,) = outcome.findings
    assert finding.details["step"] == "ingest_result"
    assert finding.details["returned"] == "builtins.NoneType"
    assert finding.details["attempts"] == 1  # a contract violation: never retried
    assert len(outcome.ingested) == 1 and not [e for e in seen if e.kind == "chunk_retried"]


def test_a_file_removed_after_planning_is_unreadable_in_parse_and_the_rest_land(
    corpus: Path, tmp_path: Path
) -> None:
    def remove_notes_after_planning(event: JobEvent) -> None:
        if event.kind == "phase_finished" and event.phase is Phase.PLAN:
            (corpus / "notes.txt").unlink()

    outcome, package, seen = run(corpus, tmp_path, on_event=remove_notes_after_planning)
    assert "neptune.runtime.source_unreadable" in codes(package)
    (unreadable,) = [e for e in seen if e.kind == "source_unreadable"]
    assert unreadable.phase is Phase.PARSE and path_of(unreadable.details) == "notes.txt"
    (summary,) = [e for e in seen if e.kind == "phase_finished" and e.phase is Phase.PARSE]
    assert summary.details["failed"] == 2  # the chunk notes.txt was opened for; the crash
    assert read_by(package)["corrupted.txt"] == 1 and read_by(package)["lift.tally"] == 1
    finding = next(f for f in outcome.findings if f.code.endswith("source_unreadable"))
    assert finding.details == {"errno": "ENOENT", "reason": "missing"}  # codes, never the text


# --- Findings are the same every run: no exception text, no repr, no path ----------------------


class GeneratorPlanTally(TALLY.TallyAdapter):  # type: ignore[misc, name-defined]
    """``plan`` returns a generator of chunks, not a ``Plan``: a repr would name its address.

    Every generator it returns is kept, so no two runs can see one at the same address.
    """

    kept: list[Iterator[Chunk]] = []  # noqa: RUF012 - shared on purpose: outlives every job

    def plan(self, source: SourceReader, config: AdapterConfig) -> Any:
        chunks = (chunk for chunk in super().plan(source, config).chunks)
        self.kept.append(chunks)
        return chunks


class AddressTally(TALLY.TallyAdapter):  # type: ignore[misc, name-defined]
    """``ingest`` builds a ``ChunkOutput`` around a bare ``object()``, whose ``ContractError``
    quotes its repr, address and all. Each object is kept, so every run's has its own address."""

    kept: list[object] = []  # noqa: RUF012 - shared on purpose: outlives every job

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        output: ChunkOutput = super().ingest(source, chunk, config)
        if not output.series or not output.series[0].length:
            return output
        stray = object()
        self.kept.append(stray)
        return ChunkOutput(records=(stray,))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("adapter", "code", "step", "facts"),
    [
        (
            GeneratorPlanTally,
            "neptune.runtime.plan_failed",
            "plan_result",
            {"error": "ContractError", "returned": "builtins.generator"},
        ),
        (AddressTally, "neptune.runtime.chunk_failed", "ingest", {"error": "ContractError"}),
    ],
)
def test_the_same_failing_job_twice_writes_the_same_package(
    tmp_path: Path, adapter: type, code: str, step: str, facts: dict[str, str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    (root / "lift.tally").write_bytes(b"TALLY1\n10 1\n20 2\n")  # one chunk of rows
    adapters = AdapterRegistry([*builtin_adapters(), adapter(rows_per_chunk=2)])
    homes = (("a", tmp_path / "w1"), ("b", tmp_path / "elsewhere" / "deep" / "w2"))
    outcomes = [
        IngestJob(root, tmp_path / name, Workspace(home), adapters).run() for name, home in homes
    ]
    assert outcomes[0].package == outcomes[1].package  # fresh workspaces, different paths
    assert outcomes[0].findings == outcomes[1].findings
    (finding,) = outcomes[0].findings
    assert finding.code == code and finding.details["step"] == step
    assert {key: finding.details[key] for key in facts} == facts
    text = finding.message + repr(finding.details)
    assert "0x" not in text and str(tmp_path) not in text
    assert len(outcomes[0].ingested) == 1  # the notes


@dataclass(frozen=True)
class _Impostor:
    """Claims a kind the brittle adapter declares, and has nothing else: no provenance."""

    kind: str = "document_block"
    id: str = "rec:sha256:" + "0" * 64


class ImpostorBrittle(BRITTLE.BrittleAdapter):  # type: ignore[misc, name-defined]
    """For the line ``impostor``, emits an ``_Impostor`` in place of its block."""

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        output: ChunkOutput = super().ingest(source, chunk, config)
        if any(getattr(r, "text", None) == Known("impostor") for r in output.records):
            return ChunkOutput(records=(_Impostor(),))  # type: ignore[arg-type]
        return output


@pytest.mark.parametrize(
    ("isolation", "phase"),
    [
        (Isolation.IN_PROCESS, Phase.NORMALIZE),  # the check meets the impostor
        (Isolation.SUBPROCESS, Phase.PARSE),  # the impostor has no JSON form to cross back in
    ],
)
def test_a_check_that_raises_on_odd_output_fails_that_chunk_not_the_job(
    tmp_path: Path, isolation: Isolation, phase: Phase
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    (root / "odd.brittle").write_bytes(BRITTLE.brittle("fine", "impostor"))
    adapters = AdapterRegistry([*builtin_adapters(), ImpostorBrittle()])
    outcome, package, seen = run(root, tmp_path, adapters, options=JobOptions(isolation=isolation))
    assert codes(package) == ["neptune.runtime.chunk_failed"]
    (finding,) = outcome.findings
    assert finding.details["step"] == "check_chunk_output"
    assert finding.details["error"] == "AttributeError"  # no provenance, no to_json
    assert finding.details["attempts"] == 1
    assert read_by(package) == {"notes.txt": 1, "odd.brittle": 1}  # read by the runtime only
    assert len(outcome.ingested) == 1
    (failed,) = [e for e in seen if e.kind == "chunk_failed"]
    assert failed.phase is phase and failed.details["step"] == "check_chunk_output"


class DiskFaultBrittle(BRITTLE.BrittleAdapter):  # type: ignore[misc, name-defined]
    """``plan`` raises an ``OSError`` of its own, naming a path, as an adapter's I/O might."""

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        raise OSError(5, "Input/output error", "/home/someone/scratch/plan.tmp")


def test_an_os_error_from_plan_is_the_adapters_failure_not_an_unreadable_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    (root / "a.brittle").write_bytes(BRITTLE.brittle("x"))
    adapters = AdapterRegistry([*builtin_adapters(), DiskFaultBrittle()])
    outcome, package, seen = run(root, tmp_path, adapters)
    assert codes(package) == ["neptune.runtime.plan_failed"]
    (finding,) = outcome.findings
    assert finding.details == {
        "adapter": "brittle",
        "error": "OSError",
        "step": "plan",
        "version": "1.0.0",
    }
    assert "/home/someone" not in finding.message
    assert not [e for e in seen if e.kind == "source_unreadable"]
    assert len(outcome.ingested) == 1


def test_a_committed_run_the_workspace_cannot_read_fails_the_job_not_the_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "lift.tally").write_bytes(b"TALLY1\n10 1\n20 2\n")
    home = Workspace(tmp_path / "home")

    def spoil_a_run(event: JobEvent) -> None:
        if event.kind == "phase_finished" and event.phase is Phase.NORMALIZE:
            run_file = next((home.home / "chunks").glob("*/*/runs/*.parquet"))
            run_file.write_bytes(b"not parquet")  # the workspace's own bytes, damaged

    job = IngestJob(root, tmp_path / "p", home, registry(), on_event=spoil_a_run)
    with pytest.raises(JobError, match="cannot be read"):
        job.run()
    assert job.state is JobState.FAILED and not (tmp_path / "p").exists()


@pytest.mark.parametrize("isolation", [Isolation.SUBPROCESS, Isolation.IN_PROCESS])
def test_a_short_read_over_an_intact_source_is_the_adapters_failure(
    tmp_path: Path, isolation: Isolation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job's reader never reads short, so a ``ShortReadError`` naming a source that still
    matches its artifact came from the adapter's own window over it: ``plan_failed`` or
    ``chunk_failed`` naming ``ShortReadError``, after the usual retries, and no finding blames
    the source; one naming another reader is the adapter's too (ADR 0033 §3). An unchanged file
    found intact is verified once, not once per attempt."""
    verified: list[str] = []

    def counting(stream: Any, artifact: Any) -> Any:
        verified.append(artifact.content_id)
        return verify_artifact(stream, artifact)

    monkeypatch.setattr(job_module, "verify_artifact", counting)
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    short = BRITTLE.brittle("first", "short", "last")
    (root / "short.brittle").write_bytes(short)
    planned = BRITTLE.brittle("plan-short", "x")
    (root / "plan.brittle").write_bytes(planned)
    elsewhere = BRITTLE.brittle("short-elsewhere")
    (root / "elsewhere.brittle").write_bytes(elsewhere)
    outcome, package, seen = run(root, tmp_path, options=JobOptions(isolation=isolation))
    assert codes(package) == [
        "neptune.runtime.chunk_failed",
        "neptune.runtime.chunk_failed",
        "neptune.runtime.plan_failed",
    ]
    failed = {f.subject.source: f.details for f in outcome.findings}
    assert failed[content_id(planned)] == {
        "adapter": "brittle",
        "error": "ShortReadError",
        "step": "plan",
        "version": "1.0.0",
    }
    for data in (short, elsewhere):
        details = failed[content_id(data)]
        assert (details["error"], details["step"], details["attempts"]) == (
            "ShortReadError",
            "ingest",
            2,  # retried as any other raise
        )
    retried = sorted(str(e.details["error"]) for e in seen if e.kind == "chunk_retried")
    assert retried == ["ShortReadError", "ShortReadError"]
    assert sorted(verified) == sorted([content_id(short), content_id(planned)])  # never elsewhere
    assert not [e for e in seen if e.kind == "source_short_read"]
    assert not [c for c in codes(package) if c.startswith("neptune.discovery.")]
    assert len(outcome.ingested) == 1  # the notes


class WindowBrittle(BRITTLE.BrittleAdapter):  # type: ignore[misc, name-defined]
    """Reads each line through a window of its own that serves nothing, before the job's reader
    is touched: a ``ShortReadError`` naming the source and the line's range, whatever the file
    now holds."""

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        if chunk.context.get("part") != "document":
            start, end = chunk.context["start"], chunk.context["end"]
            assert isinstance(start, int) and isinstance(end, int)
            BRITTLE._read_short(source, start, end)
        output: ChunkOutput = super().ingest(source, chunk, config)
        return output


@pytest.mark.parametrize("isolation", [Isolation.SUBPROCESS, Isolation.IN_PROCESS])
@pytest.mark.parametrize(
    ("adapter", "blamed"),
    [
        (BRITTLE.BrittleAdapter, "neptune.runtime.source_changed"),  # its read fails the hash
        (WindowBrittle, "neptune.discovery.short_read"),  # its own window reads short
    ],
)
def test_a_source_cut_under_the_adapter_is_the_sources_finding_never_retried(
    tmp_path: Path, isolation: Isolation, adapter: type, blamed: str
) -> None:
    """A source cut while its chunks are being ingested: an adapter that reads it through the
    job's reader meets ``SourceChangedError`` (``source_changed``); one whose own window raises
    ``ShortReadError`` naming it is the source's ``short_read`` for the unserved range, since the
    job finds the file no longer matches its artifact. Either way ``verify_artifact``'s account
    follows, the source is quarantined and nothing is retried (ADR 0033 §3)."""
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    data = BRITTLE.brittle("first", "second", "third")
    (root / "cut.brittle").write_bytes(data)
    cut = 20

    def cut_after_the_first_chunk(event: JobEvent) -> None:
        mine = event.kind == "chunk_committed" and event.details["source"] == content_id(data)
        if mine and (root / "cut.brittle").stat().st_size == len(data):
            (root / "cut.brittle").write_bytes(data[:cut])

    adapters = AdapterRegistry([*builtin_adapters(), adapter()])
    outcome, package, seen = run(
        root,
        tmp_path,
        adapters,
        options=JobOptions(isolation=isolation),
        on_event=cut_after_the_first_chunk,
    )
    assert codes(package) == sorted([blamed, "neptune.discovery.truncated"])
    (truncated,) = [f for f in outcome.findings if f.code == "neptune.discovery.truncated"]
    assert truncated.subject == EvidenceRef(content_id(data), (ByteRange(cut, len(data) - cut),))
    first = data.index(b"first")
    if blamed == "neptune.discovery.short_read":
        (short,) = [f for f in outcome.findings if f.code == blamed]
        assert short.subject == EvidenceRef(content_id(data), (ByteRange(first, 5),))
        assert short.details == {"offset": first, "unread_bytes": 5}
        (event,) = [e for e in seen if e.kind == "source_short_read"]
        assert (event.details["step"], event.details["offset"]) == ("ingest", first)
    else:
        assert not [e for e in seen if e.kind == "source_short_read"]
    assert not [e for e in seen if e.kind == "chunk_retried"]
    quarantined = [codes_of(e.details) for e in seen if e.kind == "source_quarantined"]
    assert quarantined == [(blamed,)]
    assert len(outcome.ingested) == 1  # the notes


def test_a_source_cut_after_hashing_is_verified_against_its_artifact(tmp_path: Path) -> None:
    """A source that shrinks under the job is ``source_changed`` with ``verify_artifact``'s
    exact account: here, the missing range (ADR 0029 §3)."""
    root = tmp_path / "root"
    root.mkdir()
    data = BRITTLE.brittle("first", "second", "third")
    (root / "cut.brittle").write_bytes(data)

    def cut_after_planning(event: JobEvent) -> None:
        if event.kind == "phase_finished" and event.phase is Phase.PLAN:
            (root / "cut.brittle").write_bytes(data[:20])

    outcome, package, _ = run(root, tmp_path, on_event=cut_after_planning)
    assert codes(package) == ["neptune.discovery.truncated", "neptune.runtime.source_changed"]
    (truncated,) = [f for f in outcome.findings if f.code == "neptune.discovery.truncated"]
    assert truncated.subject == EvidenceRef(content_id(data), (ByteRange(20, len(data) - 20),))
    assert truncated.details == {
        "actual_size": 20,
        "declared_size": len(data),
        "missing_bytes": len(data) - 20,
    }
