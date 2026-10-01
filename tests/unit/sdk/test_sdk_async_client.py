"""The async SDK: the sync surface, awaited; events on the loop; cancellation that waits (ADR 0035).

Each test drives its own loop with ``asyncio.run``. Jobs run the real runtime: through the
sandbox, or in process where a test needs an adapter that holds still (``gated_adapter``).
"""

import asyncio
import importlib.util
import inspect
import shutil
import threading
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.runtime import Isolation, JobEvent, JobOptions, Phase
from neptune.sdk import (
    AsyncIngestion,
    AsyncNeptune,
    DestinationExistsError,
    IngestResult,
    InvalidSourceError,
    Neptune,
    NetworkRefusedError,
    committed_result,
    read_package,
)
from neptune.store.assemble import StagedPackage, publish

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


def no_job_threads() -> bool:
    return not any(t.name == "neptune-ingest" and t.is_alive() for t in threading.enumerate())


def test_the_async_client_has_the_sync_surface() -> None:
    for name in ("ingest", "dry_run", "start", "start_dry_run", "workspace", "registry", "options"):
        assert hasattr(Neptune, name) and hasattr(AsyncNeptune, name)
    for name in ("ingest", "dry_run", "start", "start_dry_run"):
        sync, awaited = getattr(Neptune, name), getattr(AsyncNeptune, name)
        assert inspect.signature(sync).parameters == inspect.signature(awaited).parameters
    assert inspect.signature(Neptune.__init__) == inspect.signature(AsyncNeptune.__init__)
    assert inspect.signature(Neptune.ingest).return_annotation is IngestResult
    assert inspect.signature(AsyncNeptune.ingest).return_annotation is IngestResult
    assert inspect.iscoroutinefunction(AsyncNeptune.ingest)
    assert inspect.iscoroutinefunction(AsyncNeptune.dry_run)


def test_await_ingest_is_the_sync_ingest(root: Path, home: Path, tmp_path: Path) -> None:
    loop_thread: list[int] = []
    on_loop: set[int] = set()
    seen: list[JobEvent] = []

    def on_event(event: JobEvent) -> None:
        on_loop.add(threading.get_ident())
        seen.append(event)

    async def main() -> IngestResult:
        loop_thread.append(threading.get_ident())
        client = AsyncNeptune(home)
        return await client.ingest(root, tmp_path / "async", on_event=on_event)

    result = asyncio.run(main())
    called: list[JobEvent] = []
    sync = Neptune(tmp_path / "other").ingest(root, tmp_path / "sync", on_event=called.append)
    assert result.committed and result.package == sync.package and result.receipt == sync.receipt
    assert [e.kind for e in seen] == [e.kind for e in called]
    assert on_loop == set(loop_thread)  # on_event runs on the loop's thread
    assert no_job_threads()


def test_await_dry_run_plans_without_parsing(root: Path, home: Path) -> None:
    async def main() -> IngestResult:
        return await AsyncNeptune(home).dry_run(root)

    plan = asyncio.run(main())
    assert plan.planned and plan.cache.calls.ingest == 0 and plan.package is None


def test_async_for_streams_events_then_the_result(root: Path, home: Path, tmp_path: Path) -> None:
    async def main() -> tuple[list[JobEvent], IngestResult, list[JobEvent]]:
        run = AsyncNeptune(home).start(root, tmp_path / "package")
        assert isinstance(run, AsyncIngestion)
        streamed = [event async for event in run]
        result = await run.result()
        again = [event async for event in run]  # the end stays the end
        assert run.done()
        return streamed, result, again

    streamed, result, again = asyncio.run(main())
    assert streamed[0].kind == "workspace_swept" and streamed[-1].phase is Phase.COMMIT
    assert result.committed and again == []


def test_start_dry_run_streams_a_dry_run(root: Path, home: Path) -> None:
    async def main() -> tuple[list[str], IngestResult]:
        run = AsyncNeptune(home).start_dry_run(root)
        kinds = [event.kind async for event in run]
        return kinds, await run.result()

    kinds, plan = asyncio.run(main())
    assert kinds[-1] == "job_planned" and plan.planned


def test_a_result_awaited_without_iterating_still_arrives(
    root: Path, home: Path, tmp_path: Path
) -> None:
    async def main() -> IngestResult:
        return await AsyncNeptune(home).start(root, tmp_path / "package").result()

    assert asyncio.run(main()).committed


def test_a_bad_call_raises_before_anything_runs(root: Path, home: Path) -> None:
    async def main() -> None:
        client = AsyncNeptune(home)
        with pytest.raises(DestinationExistsError):
            client.start(root, root)
        with pytest.raises(DestinationExistsError):
            await client.ingest(root, root)
        with pytest.raises(InvalidSourceError):
            await client.dry_run(root / "missing")
        with pytest.raises(NetworkRefusedError):
            await client.dry_run("s3://fleet/run-7")

    asyncio.run(main())
    assert no_job_threads()


def test_start_needs_a_running_loop(root: Path, home: Path) -> None:
    with pytest.raises(RuntimeError, match="no running event loop"):
        AsyncNeptune(home).start_dry_run(root)


def test_cancelling_the_task_cancels_the_job_and_waits_for_it(
    root: Path, home: Path, tmp_path: Path
) -> None:
    gate = threading.Event()
    adapter = GATED.GatedAdapter(gate)
    seen: list[JobEvent] = []

    async def main() -> None:
        client = AsyncNeptune(home, adapters=[adapter], options=IN_PROCESS)
        task = asyncio.create_task(client.ingest(root, tmp_path / "package", on_event=seen.append))
        while not adapter.reached.is_set():  # the job is inside plan, holding still
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()  # the task waits for the job to stop, not just for the cancel
        gate.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert committed_result(caught.value) is None  # stopped at a checkpoint: no package

    asyncio.run(main())
    assert no_job_threads()  # nothing is left running behind the caller's back
    assert not (tmp_path / "package").exists()
    # The job stopped at a checkpoint: the plan it was making is saved; the next job reuses it.
    result = Neptune(home).ingest(root, tmp_path / "package")
    assert result.committed and result.cache.calls.plan < 2


def test_an_exception_from_on_event_cancels_the_job_and_propagates(
    root: Path, home: Path, tmp_path: Path
) -> None:
    def stop(event: JobEvent) -> None:
        if event.kind == "source_selected":
            raise LookupError("the consumer gave up")

    async def main() -> None:
        with pytest.raises(LookupError, match="gave up"):
            await AsyncNeptune(home).ingest(root, tmp_path / "package", on_event=stop)

    asyncio.run(main())
    assert no_job_threads() and not (tmp_path / "package").exists()


def test_cancel_on_the_handle_ends_the_job_cancelled(
    root: Path, home: Path, tmp_path: Path
) -> None:
    gate = threading.Event()
    adapter = GATED.GatedAdapter(gate)

    async def main() -> IngestResult:
        client = AsyncNeptune(home, adapters=[adapter], options=IN_PROCESS)
        run = client.start(root, tmp_path / "package")
        while not adapter.reached.is_set():
            await asyncio.sleep(0.01)
        run.cancel()
        gate.set()
        await run.wait()
        return await run.result()

    assert asyncio.run(main()).cancelled


def test_a_shared_cancel_event_reaches_the_async_job(
    root: Path, home: Path, tmp_path: Path
) -> None:
    cancel = threading.Event()
    cancel.set()

    async def main() -> IngestResult:
        return await AsyncNeptune(home).ingest(root, tmp_path / "package", cancel=cancel)

    assert asyncio.run(main()).cancelled


def test_a_task_cancelled_after_the_last_checkpoint_carries_the_committed_package(
    root: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job holds still just after its rename (the seam: nothing else stops a job between its
    last checkpoint and its last event). The cancel is acknowledged once the package is in
    place: ``CancelledError`` still ends the task, and carries the committed result (ADR 0035
    §3), so the caller knows the destination now holds the package."""
    published, gate = threading.Event(), threading.Event()

    def held(staged: StagedPackage) -> str:
        package = publish(staged)
        published.set()
        gate.wait(30)
        return package

    monkeypatch.setattr("neptune.runtime.job.publish", held)

    async def main() -> BaseException:
        client = AsyncNeptune(home)
        task = asyncio.create_task(client.ingest(root, tmp_path / "package"))
        while not published.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()  # acknowledged after the publish, not before
        gate.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        return caught.value

    error = asyncio.run(main())
    assert no_job_threads()
    result = committed_result(error)
    assert result is not None and result.committed
    assert result.destination == tmp_path / "package"
    assert result.package == read_package(tmp_path / "package").id
    assert any("committed package" in note for note in getattr(error, "__notes__", []))


def test_an_on_event_exception_after_the_last_checkpoint_carries_the_committed_package(
    root: Path, home: Path, tmp_path: Path
) -> None:
    def stop(event: JobEvent) -> None:
        if event.kind == "job_committed":  # the package is in place by now
            raise LookupError("the consumer gave up")

    async def main() -> BaseException:
        with pytest.raises(LookupError, match="gave up") as caught:
            await AsyncNeptune(home).ingest(root, tmp_path / "package", on_event=stop)
        return caught.value

    error = asyncio.run(main())
    assert no_job_threads()
    result = committed_result(error)
    assert result is not None and result.committed
    assert result.package == read_package(tmp_path / "package").id
