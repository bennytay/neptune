"""The ingest job's state machine: phases, events, options, cancellation, determinism (ADR 0029)."""

import os
import shutil
import threading
from pathlib import Path
from typing import Final

import pytest

from neptune.adapters.builtin import default_registry
from neptune.adapters.registry import AdapterRegistry
from neptune.runtime import (
    PHASES,
    IngestJob,
    JobError,
    JobEvent,
    JobOptions,
    JobState,
    Phase,
)
from neptune.store.package import read_envelope, read_package
from neptune.store.workspace import Workspace

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "root"
    folder.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", folder / "notes.txt")
    shutil.copy(FIXTURES / "text" / "operator_log", folder / "operator_log")
    (folder / "blob.bin").write_bytes(b"\x00\x01binary\x00")
    return folder


def run(root: Path, tmp_path: Path, name: str = "package", **kwargs: object) -> list[JobEvent]:
    seen: list[JobEvent] = []
    job = IngestJob(
        root,
        tmp_path / name,
        Workspace(tmp_path / "home"),
        default_registry(),
        on_event=seen.append,
    )
    outcome = job.run()
    assert outcome.state is JobState.COMMITTED
    return seen


# --- Phases and events -------------------------------------------------------------------------


def test_phases_are_nine_in_a_fixed_order() -> None:
    assert [str(phase) for phase in PHASES] == [
        "discover",
        "fingerprint",
        "inspect",
        "plan",
        "parse",
        "normalize",
        "assemble",
        "validate",
        "commit",
    ]


def test_every_phase_starts_once_and_finishes_once_in_order(root: Path, tmp_path: Path) -> None:
    seen = run(root, tmp_path)
    started = [e.phase for e in seen if e.kind == "phase_started"]
    finished = [e.phase for e in seen if e.kind == "phase_finished"]
    assert started == list(PHASES) and finished == list(PHASES)
    for phase in PHASES:  # a phase starts before it finishes, and finishes before the next starts
        start = next(
            i for i, e in enumerate(seen) if e.kind == "phase_started" and e.phase is phase
        )
        end = next(i for i, e in enumerate(seen) if e.kind == "phase_finished" and e.phase is phase)
        assert start < end
    assert seen[-1].kind == "phase_finished" and seen[-1].phase is Phase.COMMIT
    assert all(isinstance(e.phase, Phase) for e in seen)


def test_phase_summaries_count_what_each_phase_did(root: Path, tmp_path: Path) -> None:
    seen = run(root, tmp_path)
    summaries = {e.phase: e.details for e in seen if e.kind == "phase_finished"}
    assert summaries[Phase.DISCOVER] == {"files": 3, "skipped": 0, "symlinks": 0}
    assert summaries[Phase.FINGERPRINT] == {
        "absences": 0,
        "locations": 3,
        "new_artifacts": 3,
        "new_revisions": 3,
        "sources": 3,
    }
    assert summaries[Phase.INSPECT] == {
        "ambiguous": 0,
        "selected": 2,
        "unreadable": 0,
        "unsupported": 1,
    }
    assert summaries[Phase.PLAN] == {"chunks": 4, "committed": 0, "failed": 0, "sources": 2}
    assert summaries[Phase.PARSE] == {"chunks": 4, "failed": 0, "skipped": 0}
    assert summaries[Phase.NORMALIZE] == {"committed": 4}
    assert summaries[Phase.ASSEMBLE] == {"quarantined": 0, "sources": 2}
    assert summaries[Phase.VALIDATE]["series"] == 0
    assert summaries[Phase.COMMIT] == {"package": read_package(tmp_path / "package").id}


def test_events_are_canonical_and_carry_no_clock(root: Path, tmp_path: Path) -> None:
    first = run(root, tmp_path, "a")
    second = run(root, tmp_path, "b")  # the second run skips every chunk
    for event in first:
        event.to_json()
        assert not any(key in event.details for key in ("time", "started", "host", "duration"))
    kinds = {e.kind for e in first}
    assert kinds >= {
        "phase_started",
        "phase_finished",
        "source_hashed",
        "source_selected",
        "source_unsupported",
        "source_planned",
        "chunk_parsed",
        "chunk_committed",
        "source_admitted",
        "package_staged",
        "package_verified",
        "job_committed",
    }
    assert {e.kind for e in second if e.kind.startswith("chunk")} == {"chunk_skipped"}


def test_an_event_is_a_token_kind_a_phase_and_canonical_details() -> None:
    event = JobEvent("chunk_committed", Phase.NORMALIZE, {"chunk": "x", "new": True})
    assert event.to_json() == {
        "details": {"chunk": "x", "new": True},
        "kind": "chunk_committed",
        "phase": "normalize",
    }
    with pytest.raises(ValueError, match="event kind"):
        JobEvent("Chunk Committed", Phase.PARSE, {})
    with pytest.raises(TypeError, match="phase"):
        JobEvent("chunk_committed", "parse", {})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        JobEvent("chunk_committed", Phase.PARSE, {"nan": float("nan")})


# --- The envelope and the outcome --------------------------------------------------------------


def test_the_envelope_names_the_job_and_times_every_phase(root: Path, tmp_path: Path) -> None:
    options = JobOptions(job="nightly-7")
    job = IngestJob(root, tmp_path / "p", Workspace(tmp_path / "home"), default_registry(), options)
    outcome = job.run()
    envelope = read_envelope(tmp_path / "p")
    assert envelope.job == outcome.job == "nightly-7"
    assert envelope.root == str(root.absolute())
    assert dict(envelope.durations).keys() == {str(phase) for phase in PHASES}
    assert all(seconds >= 0.0 for _, seconds in envelope.durations)
    assert envelope.receipt == read_package(tmp_path / "p").manifest.receipt
    assert envelope.started <= envelope.finished
    assert dict(outcome.durations).keys() == dict(envelope.durations).keys()
    assert outcome.package == read_package(tmp_path / "p").id
    assert len(outcome.ingested) == 2 and outcome.findings == ()


def test_a_job_name_is_random_unless_given(root: Path, tmp_path: Path) -> None:
    a = IngestJob(root, tmp_path / "a", Workspace(tmp_path / "home"), default_registry()).job
    b = IngestJob(root, tmp_path / "b", Workspace(tmp_path / "home"), default_registry()).job
    assert a != b and len(a) == 32


# --- Options and refusals ----------------------------------------------------------------------


def test_options_are_checked_before_any_work(root: Path, tmp_path: Path) -> None:
    home, registry = Workspace(tmp_path / "home"), default_registry()
    with pytest.raises(JobError, match="attempts must be at least 1"):
        JobOptions(attempts=0)
    with pytest.raises(JobError, match="attempts must be an integer"):
        JobOptions(attempts=True)
    with pytest.raises(JobError, match="non-empty text"):
        JobOptions(job="")
    with pytest.raises(JobError, match="not registered"):
        IngestJob(root, tmp_path / "p", home, registry, JobOptions(config={"mcap": {}}))
    with pytest.raises(JobError, match="no options"):
        IngestJob(root, tmp_path / "p", home, registry, JobOptions(config={"text": {"x": 1}}))
    with pytest.raises(JobError, match="block_rule"):
        options = JobOptions(config={"text": {"block_rule": "stanza"}})
        IngestJob(root, tmp_path / "p", home, registry, options)
    assert not (tmp_path / "p").exists()


def test_the_root_must_be_a_directory_and_the_destination_free(root: Path, tmp_path: Path) -> None:
    home, registry = Workspace(tmp_path / "home"), default_registry()
    with pytest.raises(JobError, match="not a directory"):
        IngestJob(root / "notes.txt", tmp_path / "p", home, registry)
    with pytest.raises(JobError, match="not a directory"):
        IngestJob(tmp_path / "missing", tmp_path / "p", home, registry)
    (tmp_path / "taken").mkdir()
    with pytest.raises(JobError, match="written once"):
        IngestJob(root, tmp_path / "taken", home, registry)


def test_a_job_runs_once(root: Path, tmp_path: Path) -> None:
    job = IngestJob(root, tmp_path / "p", Workspace(tmp_path / "home"), default_registry())
    job.run()
    assert job.state is JobState.COMMITTED
    with pytest.raises(JobError, match="runs once"):
        job.run()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_an_unreadable_root_fails_the_job(root: Path, tmp_path: Path) -> None:
    root.chmod(0o000)
    try:
        job = IngestJob(root, tmp_path / "p", Workspace(tmp_path / "home"), default_registry())
        with pytest.raises(JobError, match="cannot be read"):
            job.run()
    finally:
        root.chmod(0o755)
    assert job.state is JobState.FAILED
    assert not (tmp_path / "p").exists()


# --- Cancellation ------------------------------------------------------------------------------


def test_cancelling_stops_at_the_next_chunk_and_the_work_so_far_resumes(
    root: Path, tmp_path: Path
) -> None:
    cancel = threading.Event()
    seen: list[JobEvent] = []

    def stop_after_two_chunks(event: JobEvent) -> None:
        seen.append(event)
        if sum(1 for e in seen if e.kind == "chunk_committed") == 2:
            cancel.set()

    home = Workspace(tmp_path / "home")
    job = IngestJob(
        root,
        tmp_path / "p",
        home,
        default_registry(),
        on_event=stop_after_two_chunks,
        cancel=cancel,
    )
    outcome = job.run()
    assert outcome.state is JobState.CANCELLED and job.state is JobState.CANCELLED
    assert outcome.package is None and not (tmp_path / "p").exists()
    assert seen[-1].kind == "job_cancelled"
    assert not any(e.kind == "phase_started" and e.phase is Phase.ASSEMBLE for e in seen)
    committed = {str(e.details["chunk"]) for e in seen if e.kind == "chunk_committed"}
    assert len(committed) == 2 and all(home.committed(chunk) for chunk in committed)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".p.")]  # no staged debris

    resumed = run(root, tmp_path, "p")
    assert committed <= {str(e.details["chunk"]) for e in resumed if e.kind == "chunk_skipped"}
    assert len([e for e in resumed if e.kind == "chunk_committed"]) == 2


def test_cancelling_after_assembly_discards_the_staged_package(root: Path, tmp_path: Path) -> None:
    cancel = threading.Event()

    def stop_when_staged(event: JobEvent) -> None:
        if event.kind == "package_staged":
            cancel.set()

    job = IngestJob(
        root,
        tmp_path / "p",
        Workspace(tmp_path / "home"),
        default_registry(),
        on_event=stop_when_staged,
        cancel=cancel,
    )
    outcome = job.run()
    assert outcome.state is JobState.CANCELLED
    assert sorted(p.name for p in tmp_path.iterdir()) == ["home", "root"]  # nothing left beside


def test_a_cancellation_requested_before_the_run_does_no_work(root: Path, tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    seen: list[JobEvent] = []
    job = IngestJob(
        root,
        tmp_path / "p",
        Workspace(tmp_path / "home"),
        default_registry(),
        on_event=seen.append,
        cancel=cancel,
    )
    outcome = job.run()
    assert outcome.state is JobState.CANCELLED
    # Discovery and fingerprinting ran (the ledger is worth keeping); inspection stopped at once.
    assert [e.phase for e in seen if e.kind == "phase_finished"] == [
        Phase.DISCOVER,
        Phase.FINGERPRINT,
    ]
    assert not any(e.kind == "source_selected" for e in seen)


# --- Determinism, lineage and boundaries -------------------------------------------------------


def test_the_same_root_gives_the_same_package_from_any_workspace(
    root: Path, tmp_path: Path
) -> None:
    registry = default_registry()
    a = IngestJob(root, tmp_path / "a", Workspace(tmp_path / "w1"), registry).run()
    b = IngestJob(root, tmp_path / "b", Workspace(tmp_path / "w2"), registry).run()
    assert a.package == b.package
    assert read_package(tmp_path / "a").files() == read_package(tmp_path / "b").files()
    assert read_envelope(tmp_path / "a").job != read_envelope(tmp_path / "b").job


def test_adapter_config_re_lineages_the_package(root: Path, tmp_path: Path) -> None:
    registry = default_registry()
    lines = JobOptions(config={"text": {"block_rule": "line"}})
    a = IngestJob(root, tmp_path / "a", Workspace(tmp_path / "home"), registry).run()
    b = IngestJob(root, tmp_path / "b", Workspace(tmp_path / "home"), registry, lines).run()
    assert a.package != b.package
    assert {t[1] for t in a.ingested}.isdisjoint({t[1] for t in b.ingested})
    blocks = {
        name: dict(read_package(tmp_path / name).receipt.records)["document_block"]
        for name in ("a", "b")
    }
    assert blocks["b"] > blocks["a"]  # a block per line, not per paragraph


def test_an_empty_root_gives_an_empty_package(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    seen = run(tmp_path / "empty", tmp_path)
    package = read_package(tmp_path / "package")
    assert package.receipt.sources == () and package.receipt.transforms == ()
    summaries = {e.phase: e.details for e in seen if e.kind == "phase_finished"}
    assert summaries[Phase.PARSE] == {"chunks": 0, "failed": 0, "skipped": 0}
    assert [e.phase for e in seen if e.kind == "phase_started"] == list(PHASES)


def test_the_same_bytes_at_two_locations_are_ingested_once(root: Path, tmp_path: Path) -> None:
    shutil.copy(root / "notes.txt", root / "copy-of-notes.txt")
    seen = run(root, tmp_path)
    summaries = {e.phase: e.details for e in seen if e.kind == "phase_finished"}
    assert summaries[Phase.FINGERPRINT]["locations"] == 4
    assert summaries[Phase.FINGERPRINT]["sources"] == 3
    assert summaries[Phase.INSPECT]["selected"] == 2
    package = read_package(tmp_path / "package")
    assert {s.location.to_json()["path"] for s in package.receipt.sources} == {
        "blob.bin",
        "copy-of-notes.txt",
        "notes.txt",
        "operator_log",
    }
    assert dict(package.receipt.records)["document_record"] == 2


def test_a_registry_with_no_adapters_reads_nothing_and_still_packages(
    root: Path, tmp_path: Path
) -> None:
    seen: list[JobEvent] = []
    job = IngestJob(
        root, tmp_path / "p", Workspace(tmp_path / "home"), AdapterRegistry(), on_event=seen.append
    )
    outcome = job.run()
    assert outcome.state is JobState.COMMITTED and outcome.ingested == ()
    assert len([e for e in seen if e.kind == "source_unsupported"]) == 3
    package = read_package(tmp_path / "p")
    assert all(s.read_by == () for s in package.receipt.sources)
