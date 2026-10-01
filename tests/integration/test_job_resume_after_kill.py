"""MVL-6 acceptance: a job killed mid-ingest resumes safely without duplicating work.

The victim is a real process (``tests/fixtures/runtime/kill_mid_job.py``) that SIGKILLs itself at
a chosen point (after a chunk commits, halfway through writing one, with one staged but not
renamed, while the package is staged, before it is published), so no cleanup runs. The resumed
job, in this process, must skip every chunk the victim committed, reuse its plans, and build the
package a clean run builds; the workspace must hold no partial chunk.
"""

import importlib.util
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.registry import AdapterRegistry
from neptune.identity import canonical_json
from neptune.model.jsonvalue import JsonObject
from neptune.model.source import SourceRevision
from neptune.runtime import IngestJob, JobError, JobEvent, JobState, Phase
from neptune.store.package import read_envelope, read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
VICTIM: Final = FIXTURES / "runtime" / "kill_mid_job.py"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / "adapters" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TALLY: Final = _load("tally_adapter")
FRAMELOG: Final = _load("framelog_adapter")


def registry() -> AdapterRegistry:
    """The victim's registry (``kill_mid_job.registry``): the same adapters, same chunking."""
    return AdapterRegistry(
        [
            *builtin_adapters(),
            TALLY.TallyAdapter(rows_per_chunk=1),
            FRAMELOG.FrameLogAdapter(frames_per_chunk=8),
        ]
    )


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "survey"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    shutil.copy(FIXTURES / "text" / "operator_log", root / "operator_log")
    rows = b"".join(f"{t * 10} {t}\n".encode() for t in range(40))
    (root / "lift.tally").write_bytes(b"TALLY1\n" + rows)
    frames = [(t * 1000, bytes([t % 256]) * 50) for t in range(50)]
    (root / "cam.framelog").write_bytes(FRAMELOG.frame_log(frames))
    (root / "blob.bin").write_bytes(b"\x00\x01binary\x00")
    return root


def kill_at(
    root: Path, home: Path, destination: Path, progress: Path, point: str, count: int
) -> int:
    """Run the victim until it kills itself at the ``count``-th ``point``; its return code."""
    paths = [str(path) for path in (root, home, destination, progress)]
    command = [sys.executable, str(VICTIM), *paths, point, str(count)]
    return subprocess.run(command, check=False, timeout=180).returncode


def whole_chunks(workspace: Workspace) -> set[str]:
    """Every chunk committed in ``workspace``, each checked to be complete: no partial chunk."""
    found = set()
    for chunk in workspace.chunks():
        directory = workspace.chunk_path(chunk)
        assert sorted(p.name for p in directory.iterdir()) == [
            "admitted.json",
            "chunk.json",
            "findings.jsonl",
            "records.jsonl",
            "runs",
        ]
        workspace.load(chunk)  # every file reads back
        assert workspace.admitted(chunk) is not None
        found.add(chunk)
    return found


def logged(progress: Path) -> list[JsonObject]:
    events: list[JsonObject] = []
    for line in progress.read_bytes().splitlines():
        event = canonical_json.loads(line)
        assert isinstance(event, dict)
        events.append(event)
    return events


def path_of(details: JsonObject) -> str:
    location = details["location"]
    assert isinstance(location, dict)
    return str(location["path"])


def logged_chunk(event: JsonObject) -> str:
    details = event["details"]
    assert isinstance(details, dict)
    return str(details["chunk"])


def chunks_of(events: list[JobEvent], kind: str) -> set[str]:
    return {str(event.details["chunk"]) for event in events if event.kind == kind}


def test_a_job_killed_mid_ingest_resumes_without_redoing_committed_chunks(
    corpus: Path, tmp_path: Path
) -> None:
    home, destination, progress = tmp_path / "home", tmp_path / "package", tmp_path / "events"
    assert kill_at(corpus, home, destination, progress, "committed", 5) == -signal.SIGKILL
    assert not destination.exists()
    before = logged(progress)
    killed = [logged_chunk(e) for e in before if e["kind"] == "chunk_committed"]
    assert len(killed) == 5
    assert all(
        e["phase"] in ("parse", "normalize") or e["kind"] != "chunk_committed" for e in before
    )
    assert not any(e["phase"] == "assemble" for e in before)

    seen: list[JobEvent] = []
    outcome = IngestJob(
        corpus, destination, Workspace(home), registry(), on_event=seen.append
    ).run()
    assert outcome.state is JobState.COMMITTED
    committed, skipped = chunks_of(seen, "chunk_committed"), chunks_of(seen, "chunk_skipped")
    assert set(killed) <= skipped
    assert not set(killed) & committed
    assert committed  # the rest was still to do
    planned = [e for e in seen if e.kind == "source_planned"]
    assert planned and all(e.details["reused"] for e in planned)  # plans saved before the kill

    clean = IngestJob(corpus, tmp_path / "clean", Workspace(tmp_path / "other"), registry()).run()
    assert outcome.package == clean.package
    assert read_package(destination).id == outcome.package
    envelope = read_envelope(destination)
    assert envelope.job == outcome.job
    assert dict(envelope.durations).keys() == {str(phase) for phase in Phase}
    assert not envelope.root.endswith("/")  # the root as the host names it
    assert Workspace(home).clear_staging() >= 0  # whatever the kill left in staging is removable


@pytest.mark.parametrize(
    ("point", "count", "workspace_debris", "beside_debris"),
    [
        ("chunk-writing", 3, 1, 0),  # a run written into a chunk's staging; the rest not yet
        ("chunk-staged", 4, 1, 0),  # a chunk staged whole and flushed, not renamed into chunks/
        # a series merged into a derivative's staging (ADR 0031), the package staged beside
        ("package-staging", 1, 1, 1),
        ("before-publish", 1, 0, 1),  # the staged package with its envelope, not renamed
    ],
)
def test_a_job_killed_inside_a_write_resumes_to_the_clean_package(
    corpus: Path,
    tmp_path: Path,
    point: str,
    count: int,
    workspace_debris: int,
    beside_debris: int,
) -> None:
    home, destination, progress = tmp_path / "home", tmp_path / "package", tmp_path / "events"
    assert kill_at(corpus, home, destination, progress, point, count) == -signal.SIGKILL
    assert not destination.exists()
    killed = {logged_chunk(e) for e in logged(progress) if e["kind"] == "chunk_committed"}
    workspace = Workspace(home)
    assert whole_chunks(workspace) == killed  # what was committed is whole; nothing else is there
    assert len(list((home / "staging").iterdir())) == workspace_debris
    staged_beside = [p for p in tmp_path.iterdir() if p.name.startswith(".package.")]
    assert len(staged_beside) == beside_debris  # a hidden sibling, never the destination
    if point == "before-publish":  # killed between write_envelope and publish
        staged = staged_beside[0]
        assert read_envelope(staged).receipt == read_package(staged).manifest.receipt

    seen: list[JobEvent] = []
    outcome = IngestJob(corpus, destination, workspace, registry(), on_event=seen.append).run()
    assert outcome.state is JobState.COMMITTED
    assert killed <= chunks_of(seen, "chunk_skipped")
    assert not killed & chunks_of(seen, "chunk_committed")
    clean = IngestJob(corpus, tmp_path / "clean", Workspace(tmp_path / "other"), registry()).run()
    assert outcome.package == clean.package == read_package(destination).id
    assert whole_chunks(workspace) == whole_chunks(Workspace(tmp_path / "other"))
    # The resuming job swept the dead writer's staging as it started (ADR 0033 §2).
    (swept,) = [e.details for e in seen if e.kind == "workspace_swept"]
    assert swept == {"scratch": 0, "staging": workspace_debris}
    assert workspace.clear_staging() == 0


def test_a_job_interrupted_in_process_resumes_the_same_way(corpus: Path, tmp_path: Path) -> None:
    home, destination = tmp_path / "home", tmp_path / "package"
    first: list[JobEvent] = []

    def interrupt_after_three(event: JobEvent) -> None:
        first.append(event)
        if len(chunks_of(first, "chunk_committed")) == 3:
            raise KeyboardInterrupt("the operator pressed ^C")

    job = IngestJob(
        corpus, destination, Workspace(home), registry(), on_event=interrupt_after_three
    )
    with pytest.raises(KeyboardInterrupt):
        job.run()
    assert job.state is JobState.FAILED
    assert not destination.exists()
    interrupted = chunks_of(first, "chunk_committed")

    second: list[JobEvent] = []
    outcome = IngestJob(
        corpus, destination, Workspace(home), registry(), on_event=second.append
    ).run()
    assert outcome.state is JobState.COMMITTED
    assert interrupted <= chunks_of(second, "chunk_skipped")
    assert not interrupted & chunks_of(second, "chunk_committed")
    fresh = IngestJob(corpus, tmp_path / "fresh", Workspace(tmp_path / "w2"), registry()).run()
    assert outcome.package == fresh.package


def test_a_finished_job_run_again_is_all_cache_and_a_package_is_written_once(
    corpus: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    first = IngestJob(corpus, tmp_path / "a", Workspace(home), registry()).run()
    again: list[JobEvent] = []
    second = IngestJob(corpus, tmp_path / "b", Workspace(home), registry(), on_event=again.append)
    outcome = second.run()
    assert outcome.package == first.package
    assert not chunks_of(again, "chunk_committed") and chunks_of(again, "chunk_skipped")
    assert [e.details for e in again if e.kind == "phase_finished" and e.phase is Phase.PARSE] == [
        {"chunks": 0, "failed": 0, "skipped": len(chunks_of(again, "chunk_skipped"))}
    ]
    with pytest.raises(JobError, match="written once"):
        IngestJob(corpus, tmp_path / "a", Workspace(home), registry())


def test_a_source_that_changes_between_jobs_gets_a_new_revision_and_keeps_the_old_work(
    corpus: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    IngestJob(corpus, tmp_path / "a", Workspace(home), registry()).run()
    (corpus / "notes.txt").write_bytes(b"Site visit - dock 5, revised after the first ingest.\n")
    seen: list[JobEvent] = []
    outcome = IngestJob(
        corpus, tmp_path / "b", Workspace(home), registry(), on_event=seen.append
    ).run()
    package = read_package(tmp_path / "b")
    revisions = [
        r
        for r in package.records
        if isinstance(r, SourceRevision) and r.location.to_json()["path"] == "notes.txt"
    ]
    assert len(revisions) == 2 and sum(1 for r in revisions if r.supersedes) == 1
    hashed = {path_of(e.details): e.details for e in seen if e.kind == "source_hashed"}
    assert hashed["notes.txt"]["new_revision"] and not hashed["lift.tally"]["new_revision"]
    # Only the changed source's chunks were new work; the rest were committed by the first job.
    new_work = {e.details["source"] for e in seen if e.kind == "chunk_committed"}
    assert new_work == {hashed["notes.txt"]["source"]}
    assert outcome.state is JobState.COMMITTED
