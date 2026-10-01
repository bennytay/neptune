"""MVL-12 acceptance, end to end: the SDK drives the real job and the real sandbox.

The text fixtures (``tests/fixtures/text``: valid, renamed, truncated, corrupted, empty) are
ingested through ``Neptune`` and ``AsyncNeptune`` with the shipped adapters and the default
isolation, a confined child process per adapter call (ADR 0030). Nothing is mocked: sockets are
disabled where a test claims local-only, a cancellation is a real cancel at a real checkpoint,
and the kill is a SIGKILL of a real process running the SDK.
"""

import asyncio
import shutil
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.identity import canonical_json
from neptune.model.finding import Severity
from neptune.model.jsonvalue import JsonObject
from neptune.sdk import AsyncNeptune, IngestResult, JobEvent, Neptune, Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
VICTIM: Final = FIXTURES / "sdk" / "sdk_victim.py"
TEXT_FILES: Final = 6  # README.md, corrupted.txt, empty.txt, notes.txt, operator_log, truncated.txt


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """The text fixtures as one folder of site notes."""
    return Path(shutil.copytree(FIXTURES / "text", tmp_path / "site-notes"))


@pytest.fixture
def long_corpus(corpus: Path) -> Path:
    """The text fixtures and forty more notes: long enough that a job is mid-parse when its
    first commit is seen and cancelled, and short enough to run twice."""
    for n in range(40):
        lines = "".join(f"dock {n}, check {k}: torque nominal\n\n" for k in range(3))
        (corpus / f"note-{n:02d}.txt").write_text(lines, encoding="utf-8")
    return corpus


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> Any:
        raise AssertionError("an SDK ingest reached for the network")

    for target, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    ):
        monkeypatch.setattr(target, name, refuse)


def chunks_of(events: list[JobEvent], kind: str) -> set[str]:
    return {str(event.details["chunk"]) for event in events if event.kind == kind}


def clean_package(corpus: Path, tmp_path: Path) -> IngestResult:
    """An uninterrupted ingest in a workspace of its own: what every resume must equal."""
    return Neptune(tmp_path / "clean-home").ingest(corpus, tmp_path / "clean")


def no_job_threads() -> bool:
    return not any(t.name == "neptune-ingest" and t.is_alive() for t in threading.enumerate())


@pytest.mark.usefixtures("no_network")
def test_a_sync_ingest_of_the_text_fixtures_runs_every_call_in_the_sandbox(
    corpus: Path, tmp_path: Path
) -> None:
    seen: list[JobEvent] = []
    client = Neptune(tmp_path / "home")
    assert client.workspace.local_only  # the default, and nothing below needs more
    result = client.ingest(corpus, tmp_path / "package", on_event=seen.append)

    assert result.committed
    (ready,) = [event.details for event in seen if event.kind == "sandbox_ready"]
    assert ready["isolation"] == "subprocess" and "degraded" not in ready
    assert len(result.ingested) == TEXT_FILES and result.findings == ()
    assert result.cache.calls.ingest == sum(len(s.chunks) for s in result.cache.sources)
    receipt = result.read_receipt()
    assert receipt.id == result.receipt
    # README.md is Markdown; session grouping's tables are in every package (ADR 0036).
    adapters = {t.adapter_id for t in receipt.transforms} - {"neptune.grouping"}
    assert "text" in adapters and adapters <= {a.descriptor.id for a in builtin_adapters()}
    # The truncated and the corrupted file each lose one block, and say so.
    assert [(f.code, f.severity) for f in receipt.findings] == [
        ("text.invalid_utf8", Severity.WARNING)
    ] * 2
    assert result.read_package().id == result.package


@pytest.mark.usefixtures("no_network")
def test_an_async_ingest_of_the_text_fixtures_writes_the_sync_package(
    corpus: Path, tmp_path: Path
) -> None:
    sync = Neptune(tmp_path / "sync-home").ingest(corpus, tmp_path / "sync")

    async def main() -> tuple[list[JobEvent], IngestResult]:
        run = AsyncNeptune(tmp_path / "async-home").start(corpus, tmp_path / "async")
        streamed = [event async for event in run]
        return streamed, await run.result()

    streamed, result = asyncio.run(main())
    assert result.committed
    assert result.package == sync.package and result.receipt == sync.receipt
    for name in ("receipt.json", "manifest.json"):
        assert (tmp_path / "async" / name).read_bytes() == (tmp_path / "sync" / name).read_bytes()
    assert "sandbox_ready" in [event.kind for event in streamed]
    assert streamed[-1].kind == "phase_finished" and no_job_threads()


def test_the_same_inputs_give_the_same_receipt_id(corpus: Path, tmp_path: Path) -> None:
    first = Neptune(tmp_path / "w1").ingest(corpus, tmp_path / "p1")
    fresh = Neptune(tmp_path / "w2").ingest(corpus, tmp_path / "p2")  # nothing cached
    cached = Neptune(tmp_path / "w1").ingest(corpus, tmp_path / "p3")  # everything cached
    moved = tmp_path / "elsewhere" / "renamed-notes"
    shutil.copytree(corpus, moved)  # the same bytes under another root
    elsewhere = Neptune(tmp_path / "w1").ingest(moved, tmp_path / "p4")

    assert cached.cache.calls.plan == 0 and cached.cache.calls.ingest == 0
    results = (first, fresh, cached, elsewhere)
    assert len({r.receipt for r in results}) == 1 and first.receipt is not None
    assert len({r.package for r in results}) == 1
    assert len({(tmp_path / f"p{n}" / "receipt.json").read_bytes() for n in (1, 2, 3, 4)}) == 1
    assert len({r.job for r in results}) == 4  # job ids are the runtime's, in the envelope only


def earlier(kind: str, corpus: Path, home: Path, tmp_path: Path) -> None:
    """A job over ``corpus`` that leaves its history in ``home``'s ledger, and no package."""
    client = Neptune(home)
    if kind == "dry_run":
        assert client.dry_run(corpus).planned
    elif kind == "cancelled":
        cancel = threading.Event()

        def on_event(event: JobEvent) -> None:
            if event.kind == "chunk_committed":
                cancel.set()

        done = client.ingest(corpus, tmp_path / "cancelled", on_event=on_event, cancel=cancel)
        assert done.cancelled
    elif kind == "killed":
        paths = [str(p) for p in (corpus, tmp_path / "killed", home, tmp_path / "killed-events")]
        killed = subprocess.run(
            [sys.executable, str(VICTIM), *paths, "2"], check=False, timeout=180
        )
        assert killed.returncode == -signal.SIGKILL
    else:
        assert client.ingest(corpus, tmp_path / "earlier").committed


CHANGES: Final[dict[str, Callable[[Path], object]]] = {
    "unchanged": lambda root: None,
    "edited": lambda root: (root / "notes.txt").write_bytes(b"Dock 5: revised.\n"),
    "deleted": lambda root: (root / "notes.txt").unlink(),
    "renamed": lambda root: (root / "notes.txt").rename(root / "notes-2026-10-02.txt"),
}


@pytest.mark.parametrize("change", sorted(CHANGES))
@pytest.mark.parametrize("kind", ["dry_run", "cancelled", "killed", "ingest"])
def test_a_package_never_depends_on_what_earlier_jobs_saw(
    kind: str, change: str, corpus: Path, tmp_path: Path
) -> None:
    """A dry run, a cancelled, killed or finished ingest, then the folder changes (or not): the
    next ingest in that workspace writes the package a fresh workspace writes from the folder as
    it is now. The workspace keeps the history; the package lists only its job's scan (ADR 0035
    §9)."""
    home = tmp_path / "home"
    earlier(kind, corpus, home, tmp_path)
    CHANGES[change](corpus)
    later = Neptune(home).ingest(corpus, tmp_path / "later")
    fresh = Neptune(tmp_path / "fresh-home").ingest(corpus, tmp_path / "fresh")

    assert later.committed and fresh.committed
    assert later.package == fresh.package and later.receipt == fresh.receipt
    for name in ("receipt.json", "manifest.json"):
        assert (tmp_path / "later" / name).read_bytes() == (tmp_path / "fresh" / name).read_bytes()
    chain = ("source_revision", "source_absence")
    listed = [r for r in later.read_package().records if r.kind in chain]
    assert len(listed) == sum(1 for path in corpus.iterdir() if path.is_file())
    assert all(not r.supersedes for r in listed)  # one revision per file, each its chain's first
    kept = Workspace(home).load_ledger(corpus)
    history = len(kept.revisions()) + len(kept.absences())
    assert history == len(listed) if change == "unchanged" else history > len(listed)


def test_a_dry_run_through_the_sandbox_predicts_the_ingest(corpus: Path, tmp_path: Path) -> None:
    client = Neptune(tmp_path / "home")
    plan = client.dry_run(corpus)
    assert plan.planned and plan.package is None and plan.cache.calls.ingest == 0
    predicted = {c.chunk for source in plan.cache.sources for c in source.chunks}
    assert len(plan.cache.sources) == TEXT_FILES

    seen: list[JobEvent] = []
    result = client.ingest(corpus, tmp_path / "package", on_event=seen.append)
    assert result.cache.calls.plan == 0  # the dry run's plans
    assert chunks_of(seen, "chunk_committed") == predicted


def test_cancelling_a_sync_ingest_keeps_the_work_and_the_rerun_finishes_it(
    long_corpus: Path, tmp_path: Path
) -> None:
    cancel = threading.Event()
    committed: list[str] = []

    def on_event(event: JobEvent) -> None:
        if event.kind == "chunk_committed":
            committed.append(str(event.details["chunk"]))
            if len(committed) == 3:
                cancel.set()

    client = Neptune(tmp_path / "home")
    result = client.ingest(long_corpus, tmp_path / "package", on_event=on_event, cancel=cancel)
    assert result.cancelled and result.package is None
    assert len(committed) == 3  # the chunk in hand finished; no other started
    assert not (tmp_path / "package").exists()

    seen: list[JobEvent] = []
    resumed = client.ingest(long_corpus, tmp_path / "package", on_event=seen.append)
    assert resumed.committed
    assert set(committed) <= chunks_of(seen, "chunk_skipped")
    assert not set(committed) & chunks_of(seen, "chunk_committed")
    assert resumed.package == clean_package(long_corpus, tmp_path).package


def test_cancelling_an_async_ingest_waits_for_the_job_and_the_rerun_finishes_it(
    long_corpus: Path, tmp_path: Path
) -> None:
    committed: list[str] = []

    async def main() -> None:
        first = asyncio.Event()

        def on_event(event: JobEvent) -> None:
            if event.kind == "chunk_committed":
                committed.append(str(event.details["chunk"]))
                first.set()

        client = AsyncNeptune(tmp_path / "home")
        task = asyncio.create_task(
            client.ingest(long_corpus, tmp_path / "package", on_event=on_event)
        )
        await first.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert no_job_threads() and not (tmp_path / "package").exists()

    seen: list[JobEvent] = []
    resumed = Neptune(tmp_path / "home").ingest(
        long_corpus, tmp_path / "package", on_event=seen.append
    )
    assert set(committed) <= chunks_of(seen, "chunk_skipped")
    assert chunks_of(seen, "chunk_committed")  # the cancelled job left work to do
    assert resumed.package == clean_package(long_corpus, tmp_path).package


def logged(progress: Path) -> list[JsonObject]:
    events: list[JsonObject] = []
    for line in progress.read_bytes().splitlines():
        event = canonical_json.loads(line)
        assert isinstance(event, dict)
        events.append(event)
    return events


def test_a_job_killed_mid_ingest_resumes_through_the_sdk(corpus: Path, tmp_path: Path) -> None:
    home, destination, progress = tmp_path / "home", tmp_path / "package", tmp_path / "events"
    paths = [str(path) for path in (corpus, destination, home, progress)]
    killed = subprocess.run([sys.executable, str(VICTIM), *paths, "4"], check=False, timeout=180)
    assert killed.returncode == -signal.SIGKILL
    assert not destination.exists()
    before = [event["details"] for event in logged(progress) if event["kind"] == "chunk_committed"]
    done = {str(details["chunk"]) for details in before if isinstance(details, dict)}
    assert len(done) == 4

    seen: list[JobEvent] = []
    resumed = Neptune(home).ingest(corpus, destination, on_event=seen.append)
    assert resumed.committed
    assert done <= chunks_of(seen, "chunk_skipped")
    assert not done & chunks_of(seen, "chunk_committed")
    assert all(e.details["reused"] for e in seen if e.kind == "source_planned")
    assert resumed.package == clean_package(corpus, tmp_path).package
    assert Workspace(home).clear_staging() == 0  # the resuming job swept what the kill left
