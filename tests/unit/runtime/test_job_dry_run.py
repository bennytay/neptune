"""``IngestJob.dry_run``: the job's first four phases, then a stop in the ``planned`` state."""

import shutil
from pathlib import Path
from typing import Final

import pytest

from neptune.adapters.builtin import default_registry
from neptune.runtime import IngestJob, JobError, JobEvent, JobState, Phase, Rule
from neptune.store.workspace import Workspace

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"
PLANNING: Final = (Phase.DISCOVER, Phase.FINGERPRINT, Phase.INSPECT, Phase.PLAN)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "root"
    folder.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", folder / "notes.txt")
    (folder / "blob.bin").write_bytes(b"\x00\x01binary\x00")
    return folder


def test_a_dry_run_runs_four_phases_and_ends_planned(root: Path, tmp_path: Path) -> None:
    seen: list[JobEvent] = []
    job = IngestJob(
        root, None, Workspace(tmp_path / "home"), default_registry(), on_event=seen.append
    )
    outcome = job.dry_run()
    assert job.state is JobState.PLANNED and outcome.state is JobState.PLANNED
    assert outcome.package is None and outcome.destination is None and outcome.ingested == ()
    assert [e.phase for e in seen if e.kind == "phase_started"] == list(PLANNING)
    assert [e.phase for e in seen if e.kind == "phase_finished"] == list(PLANNING)
    assert seen[-1].kind == "job_planned" and seen[-1].phase is Phase.PLAN
    assert seen[-1].details == {"sources": 1}
    assert not any(e.kind.startswith("chunk_") for e in seen)
    assert outcome.cache.calls.ingest == 0 and outcome.cache.receipt is None
    (planned,) = outcome.cache.sources
    assert planned.adapter == "text" and planned.plan.rule is Rule.SOURCE_NEW
    assert {c.rule for c in planned.chunks} == {Rule.SOURCE_NEW}
    # The unclaimed file is the probe engine's finding, as in a full run.
    assert [f.code for f in outcome.findings] == ["neptune.probe.unsupported"]
    assert dict(outcome.durations)["parse"] == 0.0


def test_a_dry_run_given_a_destination_never_writes_it(root: Path, tmp_path: Path) -> None:
    job = IngestJob(root, tmp_path / "package", Workspace(tmp_path / "home"), default_registry())
    outcome = job.dry_run()
    assert outcome.destination == tmp_path / "package" and not (tmp_path / "package").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".package")]


def test_a_job_without_a_destination_can_only_dry_run(root: Path, tmp_path: Path) -> None:
    job = IngestJob(root, None, Workspace(tmp_path / "home"), default_registry())
    with pytest.raises(JobError, match="needs a destination"):
        job.run()
    assert job.state is JobState.PENDING  # refused before it started: it can still dry run
    assert job.dry_run().state is JobState.PLANNED


def test_a_job_runs_once_whether_dry_or_not(root: Path, tmp_path: Path) -> None:
    job = IngestJob(root, tmp_path / "package", Workspace(tmp_path / "home"), default_registry())
    job.dry_run()
    with pytest.raises(JobError, match="runs once"):
        job.run()
    with pytest.raises(JobError, match="runs once"):
        job.dry_run()


def test_a_dry_run_after_an_ingest_finds_every_chunk_committed(root: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    IngestJob(root, tmp_path / "package", Workspace(home), default_registry()).run()
    outcome = IngestJob(root, None, Workspace(home), default_registry()).dry_run()
    (planned,) = outcome.cache.sources
    assert planned.plan.rule is Rule.PLANNED
    assert {c.rule for c in planned.chunks} == {Rule.COMMITTED}
    assert outcome.cache.calls.plan == 0 and outcome.cache.calls.ingest == 0


def test_the_plans_a_dry_run_saves_are_the_ones_a_run_reuses(root: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    IngestJob(root, None, Workspace(home), default_registry()).dry_run()
    seen: list[JobEvent] = []
    outcome = IngestJob(
        root, tmp_path / "package", Workspace(home), default_registry(), on_event=seen.append
    ).run()
    assert outcome.state is JobState.COMMITTED and outcome.cache.calls.plan == 0
    assert all(e.details["reused"] for e in seen if e.kind == "source_planned")
    fresh = IngestJob(root, tmp_path / "fresh", Workspace(tmp_path / "w2"), default_registry())
    assert fresh.run().package == outcome.package
