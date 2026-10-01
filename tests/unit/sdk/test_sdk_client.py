"""The sync SDK: every public call, its checks before anything runs, and its result (ADR 0035).

Jobs here run the real runtime; they run in process only where a test needs an adapter that
holds still (``gated_adapter``), and through the sandbox otherwise.
"""

import errno
import importlib.util
import os
import shutil
import threading
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

import neptune.store.assemble
from neptune.adapters.builtin import builtin_adapters, default_registry
from neptune.adapters.registry import AdapterRegistry
from neptune.adapters.text import TextAdapter
from neptune.runtime import PHASES, Isolation, JobError, JobEvent, JobOptions, JobState, Phase
from neptune.sdk import (
    ConfigurationError,
    DestinationExistsError,
    IngestResult,
    InvalidDestinationError,
    InvalidRequestError,
    InvalidSourceError,
    Neptune,
    NetworkRefusedError,
    PackageInvalidError,
    PublishIncompleteError,
    UnsupportedError,
    Workspace,
    WorkspaceUnusableError,
    committed_result,
    dry_run,
    ingest,
    read_package,
)
from neptune.store.assemble import NotDurableError
from neptune.store.package import read_package as store_read_package
from neptune.store.workspace import LocalOnlyError, WorkspaceError

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"
IN_PROCESS: Final = JobOptions(isolation=Isolation.IN_PROCESS)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / "adapters" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATED: Final = _load("gated_adapter")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "run"
    folder.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", folder / "notes.txt")
    shutil.copy(FIXTURES / "text" / "operator_log", folder / "operator_log")
    return folder


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def kinds(events: list[JobEvent]) -> list[str]:
    return [event.kind for event in events]


# --- ingest ------------------------------------------------------------------------------------


def test_ingest_writes_the_package_and_returns_the_runtime_outcome(
    root: Path, home: Path, tmp_path: Path
) -> None:
    result = Neptune(home).ingest(root, tmp_path / "package")
    assert isinstance(result, IngestResult)
    assert result.committed and result.state is JobState.COMMITTED
    assert not result.cancelled and not result.planned
    package = store_read_package(tmp_path / "package")
    assert result.destination == tmp_path / "package"
    assert result.package == package.id
    assert result.receipt == package.manifest.receipt == package.receipt.id
    assert len(result.ingested) == 2 and result.findings == ()
    assert {phase for phase, _ in result.durations} == {str(phase) for phase in PHASES}
    assert result.cache is result.outcome.cache and result.job == result.outcome.job
    assert result.read_package().id == result.package
    assert result.read_receipt() == package.receipt


def test_ingest_delivers_every_runtime_event_on_the_calling_thread(
    root: Path, home: Path, tmp_path: Path
) -> None:
    threads: set[int] = set()
    seen: list[JobEvent] = []

    def on_event(event: JobEvent) -> None:
        threads.add(threading.get_ident())
        seen.append(event)

    Neptune(home).ingest(root, tmp_path / "package", on_event=on_event)
    assert threads == {threading.get_ident()}
    assert kinds(seen)[0] == "workspace_swept"
    assert "job_committed" in kinds(seen)
    assert [e.phase for e in seen if e.kind == "phase_started"] == list(PHASES)


def test_an_exception_from_on_event_stops_the_job_and_the_next_ingest_resumes(
    root: Path, home: Path, tmp_path: Path
) -> None:
    def stop(event: JobEvent) -> None:
        if event.kind == "chunk_committed":
            raise KeyError("the consumer gave up")

    with pytest.raises(KeyError, match="gave up") as caught:
        Neptune(home).ingest(root, tmp_path / "package", on_event=stop)
    assert committed_result(caught.value) is None
    assert not (tmp_path / "package").exists()
    seen: list[JobEvent] = []
    result = Neptune(home).ingest(root, tmp_path / "package", on_event=seen.append)
    assert result.committed and "chunk_skipped" in kinds(seen)


def test_a_cancel_event_ends_the_job_cancelled_without_a_package(
    root: Path, home: Path, tmp_path: Path
) -> None:
    cancel = threading.Event()
    cancel.set()  # checked from inspect on: the walk and its ledger finish first (ADR 0028 §6)
    seen: list[JobEvent] = []
    result = Neptune(home).ingest(root, tmp_path / "package", on_event=seen.append, cancel=cancel)
    assert result.cancelled and result.state is JobState.CANCELLED
    assert result.package is None and result.receipt is None
    assert not (tmp_path / "package").exists()
    assert kinds(seen)[-1] == "job_cancelled"
    assert "source_hashed" in kinds(seen) and "source_selected" not in kinds(seen)
    with pytest.raises(InvalidRequestError, match="cancelled job wrote no package"):
        result.read_package()
    assert Neptune(home).ingest(root, tmp_path / "package").committed


def test_the_one_call_ingest_is_the_client_call(root: Path, home: Path, tmp_path: Path) -> None:
    seen: list[JobEvent] = []
    result = ingest(root, tmp_path / "a", workspace=home, on_event=seen.append)
    again = Neptune(home).ingest(root, tmp_path / "b")
    assert result.committed and result.package == again.package and seen


# --- dry_run -----------------------------------------------------------------------------------


def test_a_dry_run_plans_every_source_and_parses_nothing(
    root: Path, home: Path, tmp_path: Path
) -> None:
    seen: list[JobEvent] = []
    plan = Neptune(home).dry_run(root, on_event=seen.append)
    assert plan.planned and plan.state is JobState.PLANNED
    assert plan.package is None and plan.receipt is None and plan.destination is None
    assert plan.ingested == ()
    assert plan.cache.calls.ingest == 0 and plan.cache.calls.plan == 2
    assert [source.adapter for source in plan.cache.sources] == ["text", "text"]
    assert {c.rule for s in plan.cache.sources for c in s.chunks} == {"source_new"}
    assert {e.phase for e in seen} <= {Phase.DISCOVER, Phase.FINGERPRINT, Phase.INSPECT, Phase.PLAN}
    assert kinds(seen)[-1] == "job_planned" and seen[-1].details == {"sources": 2}
    with pytest.raises(InvalidRequestError, match="planned job wrote no package"):
        plan.read_receipt()
    with pytest.raises(InvalidRequestError):
        plan.read_package()

    # The plans it saved are the ones ingest uses: no adapter plans twice.
    result = Neptune(home).ingest(root, tmp_path / "package")
    assert result.cache.calls.plan == 0 and result.cache.calls.ingest > 0
    assert all(source.plan.rule == "planned" for source in result.cache.sources)


def test_the_one_call_dry_run_is_the_client_call(root: Path, home: Path, tmp_path: Path) -> None:
    plan = dry_run(root, workspace=home)
    assert plan.planned and plan.cache == Neptune(tmp_path / "other").dry_run(root).cache
    again = Neptune(home).dry_run(root)  # the plans the first saved are reused
    assert again.cache.calls.plan == 0 and again.cache.calls.ingest == 0


def test_a_dry_run_of_a_folder_whose_package_exists_is_still_a_dry_run(
    root: Path, home: Path, tmp_path: Path
) -> None:
    Neptune(home).ingest(root, tmp_path / "package")
    plan = Neptune(home).dry_run(root)
    assert plan.planned
    assert all(c.rule == "committed" for s in plan.cache.sources for c in s.chunks)


# --- The client: workspace, adapters, options, remote ------------------------------------------


def test_the_default_workspace_is_neptune_home_and_local_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEPTUNE_HOME", str(tmp_path / "default"))
    client = Neptune()
    assert client.workspace.home == tmp_path / "default"
    assert client.workspace.local_only
    assert (tmp_path / "default" / "workspace.json").exists()


def test_a_workspace_is_a_path_or_an_open_workspace(home: Path) -> None:
    opened = Workspace(home)
    assert Neptune(opened).workspace is opened
    assert Neptune(str(home)).workspace.home == home


def test_a_workspace_that_cannot_be_opened_is_unusable(tmp_path: Path) -> None:
    (tmp_path / "file").write_bytes(b"not a directory")
    with pytest.raises(WorkspaceUnusableError) as caught:
        Neptune(tmp_path / "file")
    assert isinstance(caught.value.__cause__, OSError)
    (tmp_path / "future").mkdir()
    (tmp_path / "future" / "workspace.json").write_bytes(b'{"format":99,"kind":"x"}')
    with pytest.raises(WorkspaceUnusableError):
        Neptune(tmp_path / "future")


def test_adapters_default_to_the_shipped_ones(home: Path) -> None:
    assert Neptune(home).registry.descriptors() == default_registry().descriptors()


def test_adapters_are_a_registry_or_any_iterable_of_adapters(home: Path) -> None:
    registry = AdapterRegistry(builtin_adapters())
    assert Neptune(home, adapters=registry).registry is registry
    chunky = TextAdapter(chunk_bytes=8)
    assert Neptune(home, adapters=[chunky]).registry.get("text") is chunky
    assert Neptune(home, adapters=()).registry.descriptors() == {}


def test_adapters_that_conflict_or_are_not_adapters_are_a_configuration_error(home: Path) -> None:
    with pytest.raises(ConfigurationError, match="cannot be registered"):
        Neptune(home, adapters=[TextAdapter(), TextAdapter()])
    with pytest.raises(ConfigurationError):
        Neptune(home, adapters=[object()])  # type: ignore[list-item]
    with pytest.raises(ConfigurationError):
        Neptune(home, adapters=3)  # type: ignore[arg-type]


def test_options_are_the_runtimes_job_options(home: Path) -> None:
    options = JobOptions(attempts=3, config={"text": {"block_rule": "line"}})
    assert Neptune(home, options=options).options is options
    assert Neptune(home).options == JobOptions()
    with pytest.raises(ConfigurationError, match="options must be JobOptions"):
        Neptune(home, options={"attempts": 3})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"nonesuch": {}}, "not registered"),
        ({"text": {"nope": 1}}, "nope"),
        ({"text": {"block_rule": "sentence"}}, "sentence"),
        ({"text": {"max_block_bytes": "big"}}, "max_block_bytes"),
    ],
)
def test_config_naming_an_unknown_adapter_or_bad_value_fails_before_any_job(
    home: Path, config: dict[str, dict[str, object]], message: str
) -> None:
    options = JobOptions(config=config)  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match=message):
        Neptune(home, options=options)


def test_adapter_config_reaches_the_adapter(root: Path, home: Path, tmp_path: Path) -> None:
    line = Neptune(home, options=JobOptions(config={"text": {"block_rule": "line"}}))
    paragraph = Neptune(home)
    by_line = line.ingest(root, tmp_path / "line")
    by_paragraph = paragraph.ingest(root, tmp_path / "paragraph")
    assert by_line.package != by_paragraph.package  # another config: another lineage
    hashes = {s.config_hash for r in (by_line, by_paragraph) for s in r.cache.sources}
    assert len(hashes) == 2


@pytest.mark.parametrize("remote", ["ftp://neptune.example", "neptune.example", "https://"])
def test_a_remote_that_is_not_an_http_url_is_a_configuration_error(home: Path, remote: str) -> None:
    with pytest.raises(ConfigurationError, match="http"):
        Neptune(home, remote=remote)


def test_remote_execution_is_refused_while_the_workspace_is_local_only(home: Path) -> None:
    with pytest.raises(NetworkRefusedError) as caught:
        Neptune(home, remote="https://neptune.example")
    assert isinstance(caught.value.__cause__, LocalOnlyError)
    assert "remote execution" in str(caught.value)


def test_remote_execution_with_the_network_allowed_is_unsupported_in_this_version(
    home: Path,
) -> None:
    workspace = Workspace(home)
    workspace.allow_network(True)
    with pytest.raises(UnsupportedError, match="MVL-46"):
        Neptune(workspace, remote="https://neptune.example")


# --- Sources and destinations ------------------------------------------------------------------


def test_a_source_that_does_not_exist_or_is_not_a_regular_file_is_invalid(
    root: Path, home: Path, tmp_path: Path
) -> None:
    client = Neptune(home)
    with pytest.raises(InvalidSourceError, match="does not exist"):
        client.ingest(tmp_path / "missing", tmp_path / "package")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(InvalidSourceError, match="neither a directory nor a regular file"):
        client.dry_run(fifo)
    assert not any((home / "ledgers").iterdir())  # nothing ran


def test_a_file_uri_names_a_local_folder(root: Path, home: Path, tmp_path: Path) -> None:
    spaced = tmp_path / "run 2 · dock"
    shutil.copytree(root, spaced)
    client = Neptune(home)
    by_path = client.dry_run(spaced).cache.sources
    by_uri = client.dry_run(spaced.as_uri()).cache.sources
    assert [[c.chunk for c in s.chunks] for s in by_uri] == [
        [c.chunk for c in s.chunks] for s in by_path
    ]
    local = "file://localhost" + spaced.as_uri().removeprefix("file://")
    assert client.dry_run(local).planned
    assert client.ingest(spaced.as_uri(), tmp_path / "package").committed


@pytest.mark.parametrize(
    ("uri", "error"),
    [
        ("file://robot-7/data/run", UnsupportedError),
        ("file:///data/run?version=2", InvalidSourceError),
        ("file:///data/run#top", InvalidSourceError),
        ("file://", InvalidSourceError),
    ],
)
def test_a_file_uri_that_names_no_local_folder_is_refused(
    home: Path, uri: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        Neptune(home).dry_run(uri)


@pytest.mark.parametrize("uri", ["s3://fleet/run-7", "https://data.example/run-7", "gs://b/r"])
def test_a_remote_source_needs_the_network_then_a_connector(home: Path, uri: str) -> None:
    workspace = Workspace(home)
    with pytest.raises(NetworkRefusedError, match="needs the network"):
        Neptune(workspace).dry_run(uri)
    workspace.allow_network(True)
    with pytest.raises(UnsupportedError, match="no connector"):
        Neptune(workspace).dry_run(uri)


def test_a_destination_that_exists_is_refused_before_anything_runs(
    root: Path, home: Path, tmp_path: Path
) -> None:
    (tmp_path / "package").mkdir()
    with pytest.raises(DestinationExistsError, match="written once"):
        Neptune(home).ingest(root, tmp_path / "package")
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    with pytest.raises(DestinationExistsError):
        Neptune(home).ingest(root, tmp_path / "dangling")
    assert not any((home / "ledgers").iterdir())


def test_a_destination_inside_the_source_is_refused(root: Path, home: Path, tmp_path: Path) -> None:
    with pytest.raises(InvalidDestinationError, match="inside the source"):
        Neptune(home).ingest(root, root / "package")
    (tmp_path / "alias").symlink_to(root)
    with pytest.raises(InvalidDestinationError, match="inside the source"):
        Neptune(home).ingest(root, tmp_path / "alias" / "package")
    assert not any(root.glob("package*"))


# --- Results and packages ----------------------------------------------------------------------


def test_a_receipt_that_is_not_the_one_the_job_wrote_is_refused(
    root: Path, home: Path, tmp_path: Path
) -> None:
    result = Neptune(home).ingest(root, tmp_path / "package")
    receipt = tmp_path / "package" / "receipt.json"
    other = Neptune(home, options=JobOptions(config={"text": {"block_rule": "line"}}))
    other.ingest(root, tmp_path / "other")
    receipt.write_bytes((tmp_path / "other" / "receipt.json").read_bytes())
    with pytest.raises(PackageInvalidError, match="not the receipt this job wrote"):
        result.read_receipt()
    with pytest.raises(PackageInvalidError):
        result.read_package()
    receipt.write_bytes(b"{")
    with pytest.raises(PackageInvalidError, match="cannot be read"):
        result.read_receipt()


def test_read_package_verifies_and_refuses_what_is_not_a_package(
    root: Path, home: Path, tmp_path: Path
) -> None:
    result = Neptune(home).ingest(root, tmp_path / "package")
    assert read_package(tmp_path / "package").id == result.package
    with pytest.raises(PackageInvalidError):
        read_package(root)
    with pytest.raises(PackageInvalidError):
        read_package(tmp_path / "missing")


# --- start: a job on its own thread ------------------------------------------------------------


def test_start_yields_the_events_of_ingest_and_the_same_result(
    root: Path, home: Path, tmp_path: Path
) -> None:
    called: list[JobEvent] = []
    direct = Neptune(tmp_path / "other").ingest(root, tmp_path / "a", on_event=called.append)
    with Neptune(home).start(root, tmp_path / "b") as run:
        streamed = list(run)
        result = run.result()
    assert run.done()
    assert kinds(streamed) == kinds(called)
    assert result.committed and result.package == direct.package
    assert list(run) == []  # iterating again after the end: nothing more


def test_start_dry_run_streams_a_dry_run(root: Path, home: Path) -> None:
    run = Neptune(home).start_dry_run(root)
    assert kinds(list(run))[-1] == "job_planned"
    assert run.result().planned


def test_start_checks_the_call_before_any_thread_starts(root: Path, home: Path) -> None:
    before = threading.active_count()
    with pytest.raises(DestinationExistsError):
        Neptune(home).start(root, root)
    with pytest.raises(InvalidSourceError):
        Neptune(home).start_dry_run(root / "missing")
    assert threading.active_count() == before


def test_result_waits_or_times_out(root: Path, home: Path, tmp_path: Path) -> None:
    gate = threading.Event()
    adapter = GATED.GatedAdapter(gate)
    client = Neptune(home, adapters=[adapter], options=IN_PROCESS)
    run = client.start(root, tmp_path / "package")
    assert adapter.reached.wait(10)
    with pytest.raises(TimeoutError):
        run.result(timeout=0.01)
    assert not run.done()
    gate.set()
    assert run.result(timeout=30).committed


def test_cancel_stops_the_job_at_its_next_checkpoint(
    root: Path, home: Path, tmp_path: Path
) -> None:
    gate = threading.Event()
    adapter = GATED.GatedAdapter(gate)
    client = Neptune(home, adapters=[adapter], options=IN_PROCESS)
    run = client.start(root, tmp_path / "package")
    assert adapter.reached.wait(10)
    run.cancel()
    gate.set()
    result = run.result(timeout=30)
    assert result.cancelled and not (tmp_path / "package").exists()
    assert kinds(list(run))[-1] == "job_cancelled"


def test_leaving_a_with_block_by_an_exception_cancels_the_job_and_waits(
    root: Path, home: Path, tmp_path: Path
) -> None:
    gate = threading.Event()
    adapter = GATED.GatedAdapter(gate)
    client = Neptune(home, adapters=[adapter], options=IN_PROCESS)
    run = client.start(root, tmp_path / "package")
    with pytest.raises(RuntimeError, match="the caller failed"), run:
        assert adapter.reached.wait(10)
        gate.set()
        raise RuntimeError("the caller failed")
    assert run.done() and run.result().cancelled


def test_a_destination_taken_while_the_job_runs_is_raised_from_result(
    root: Path, home: Path, tmp_path: Path
) -> None:
    gate = threading.Event()
    adapter = GATED.GatedAdapter(gate)
    client = Neptune(home, adapters=[adapter], options=IN_PROCESS)
    run = client.start(root, tmp_path / "package")
    assert adapter.reached.wait(10)
    (tmp_path / "package").mkdir()  # something takes the destination while the job runs
    gate.set()
    with pytest.raises(DestinationExistsError, match="a package is written once") as caught:
        run.result(timeout=30)
    assert isinstance(caught.value.__cause__, JobError)
    assert kinds(list(run))[-1] == "job_failed"


def test_a_package_renamed_into_place_whose_flush_fails_is_publish_incomplete(
    root: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No disk fails on cue, so the flush after the rename is made to (the only seam): the job
    renamed its package, so it is the job's, whole, and the error says so, not
    ``destination_exists``."""

    def unflushable(directory: Path) -> None:
        raise OSError(errno.EIO, "Input/output error", str(directory))

    monkeypatch.setattr(neptune.store.assemble, "fsync_directory", unflushable)
    seen: list[JobEvent] = []
    with pytest.raises(PublishIncompleteError, match="may not survive a crash") as caught:
        Neptune(home).ingest(root, tmp_path / "package", on_event=seen.append)
    assert caught.value.code == "publish_incomplete"
    assert isinstance(caught.value.__cause__, JobError)
    assert isinstance(caught.value.__cause__.__cause__, NotDurableError)
    assert kinds(seen)[-1] == "job_failed" and "job_committed" not in kinds(seen)
    package = read_package(tmp_path / "package")  # whole, and verifies
    staged = next(e for e in seen if e.kind == "package_staged")
    assert package.id == staged.details["package"]
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".package.")] == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
@pytest.mark.parametrize(
    ("folder", "mode", "message"),
    [
        ("staging", 0o000, "cannot be swept"),
        ("ledgers", 0o500, "ledger of .* cannot be saved"),
        ("plans", 0o500, "plan of .* cannot be saved"),
        ("chunks", 0o500, "cannot be committed"),
    ],
)
def test_a_workspace_that_will_not_read_or_write_is_workspace_unusable(
    root: Path, home: Path, tmp_path: Path, folder: str, mode: int, message: str
) -> None:
    """Every failure to read or write the workspace mid-job is ``workspace_unusable``, never
    ``job_failed``: sweeping staging, saving the ledger, saving a plan, committing a chunk.
    Each is a real ``OSError`` (a folder this user may not write), not a stand-in."""
    client = Neptune(home)
    locked = home / folder
    locked.chmod(mode)
    try:
        with pytest.raises(WorkspaceUnusableError, match=message) as caught:
            client.ingest(root, tmp_path / "package")
    finally:
        locked.chmod(0o755)
    assert caught.value.code == "workspace_unusable"
    job_error = caught.value.__cause__
    assert isinstance(job_error, JobError)
    assert isinstance(job_error.__cause__, WorkspaceError)
    assert isinstance(job_error.__cause__.__cause__, PermissionError)
    assert not (tmp_path / "package").exists()
    assert client.ingest(root, tmp_path / "package").committed  # usable again: the job resumes


def test_an_on_event_exception_after_the_publish_leaves_the_job_committed(
    root: Path, home: Path, tmp_path: Path
) -> None:
    """Only ``on_event`` runs after the rename: what it raises propagates, the job is committed,
    not failed, and the exception carries the committed result (ADR 0035 §3)."""
    seen: list[JobEvent] = []

    def stop(event: JobEvent) -> None:
        seen.append(event)
        if event.kind == "job_committed":
            raise KeyError("the consumer gave up")

    with pytest.raises(KeyError, match="gave up") as caught:
        Neptune(home).ingest(root, tmp_path / "package", on_event=stop)
    result = committed_result(caught.value)
    assert result is not None and result.state is JobState.COMMITTED
    assert result.package == read_package(tmp_path / "package").id
    assert result.receipt == result.read_receipt().id
    assert kinds(seen)[-1] == "job_committed" and "job_failed" not in kinds(seen)


def test_leaving_a_with_block_after_the_job_committed_carries_the_result(
    root: Path, home: Path, tmp_path: Path
) -> None:
    run = Neptune(home).start(root, tmp_path / "package")
    with pytest.raises(RuntimeError, match="the caller failed") as caught, run:
        assert run.result(timeout=60).committed
        raise RuntimeError("the caller failed")
    result = committed_result(caught.value)
    assert result is not None and result.package == read_package(tmp_path / "package").id
