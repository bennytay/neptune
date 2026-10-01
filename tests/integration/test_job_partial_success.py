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
from collections.abc import Callable
from dataclasses import replace
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
from neptune.model.finding import FindingCategory, Severity, subject_to_json
from neptune.model.knowledge import Known
from neptune.model.series import ColumnType, SeriesBatch, SeriesColumn
from neptune.model.world import DocumentBlock, DocumentRecord
from neptune.runtime import IngestJob, JobEvent, JobOptions, JobState, Phase
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
    return sorted(finding.code for finding in package.receipt.findings)


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
        "neptune.runtime.chunk_failed",
        "neptune.runtime.plan_failed",
        "tally.bad_row",
        "text.invalid_utf8",
    ]
    assert read_by(package) == {
        "blob.bin": 0,
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
    outcome, package, seen = run(root, tmp_path, options=JobOptions(attempts=2))
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
    assert "after 1 attempt (OSError)" in finding.message
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
    assert wrong.details["error"] == "ContractError" and "problem" in wrong.details
    assert wrong.details["attempts"] == 1  # a contract violation is a bug: never retried
    twice = by_code["neptune.runtime.output_invalid"]
    problems = twice.details["problems"]
    assert isinstance(problems, list) and len(problems) == 1
    assert "emitted by two chunks" in str(problems[0])
    assert "two chunks" in twice.message
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
    assert "seq ranges of chunks" in finding.message and "overlap" in finding.message
    assert finding.details["problems"] == [finding.message.split(": ", 1)[1]]


def test_a_source_that_changes_during_the_job_is_reported_and_the_rest_proceed(
    corpus: Path, tmp_path: Path
) -> None:
    def rewrite_notes_after_planning(event: JobEvent) -> None:
        if event.kind == "phase_finished" and event.phase is Phase.PLAN:
            data = (corpus / "notes.txt").read_bytes()
            (corpus / "notes.txt").write_bytes(data[:-1] + b"!")  # same size, other bytes

    outcome, package, seen = run(corpus, tmp_path, on_event=rewrite_notes_after_planning)
    assert "neptune.runtime.source_changed" in codes(package)
    assert read_by(package)["notes.txt"] == 0  # not read: nothing could cite its bytes
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
    assert codes(package) == ["neptune.runtime.entry_skipped"]
    (finding,) = outcome.findings
    assert finding.severity is Severity.INFO and finding.details == {"reason": "not_regular_file"}
    assert subject_to_json(finding.subject) == {"kind": "local", "path": "pipe"}
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
    assert codes(package) == ["neptune.runtime.entry_skipped"]
    (finding,) = outcome.findings
    assert finding.severity is Severity.ERROR and finding.details == {"reason": "unreadable"}
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
    ("adapter", "problem"),
    [(DriftingTally, "disagree on their columns"), (MisplacedTally, "offset")],
)
def test_one_sources_broken_series_is_a_finding_and_the_rest_land(
    tmp_path: Path, adapter: type, problem: str
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
    assert problem in finding.message
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
    assert "disagree on their columns" in str(finding.details["problem"])
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
    assert finding.details["problem"] == "ingest returned a NoneType"
    assert finding.details["attempts"] == 1  # a contract violation: never retried
    assert len(outcome.ingested) == 1 and not [e for e in seen if e.kind == "chunk_retried"]


def test_a_file_removed_after_planning_is_unreadable_in_parse_and_the_rest_land(
    corpus: Path, tmp_path: Path
) -> None:
    def remove_notes_after_planning(event: JobEvent) -> None:
        if event.kind == "phase_finished" and event.phase is Phase.PLAN:
            (corpus / "notes.txt").unlink()

    _, package, seen = run(corpus, tmp_path, on_event=remove_notes_after_planning)
    assert "neptune.runtime.source_unreadable" in codes(package)
    (unreadable,) = [e for e in seen if e.kind == "source_unreadable"]
    assert unreadable.phase is Phase.PARSE and path_of(unreadable.details) == "notes.txt"
    (summary,) = [e for e in seen if e.kind == "phase_finished" and e.phase is Phase.PARSE]
    assert summary.details["failed"] == 2  # the chunk notes.txt was opened for; the crash
    assert read_by(package)["corrupted.txt"] == 1 and read_by(package)["lift.tally"] == 1
