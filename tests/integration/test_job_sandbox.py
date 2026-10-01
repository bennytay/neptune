"""MVL-10 acceptance: a parser that crashes, hangs or allocates without bound yields a finding,
and the job completes for every other source and chunk; killing the sandboxed process leaves no
partial chunk output in the workspace.

The ``hostile`` fixture adapter supplies the attacks on demand, line by line, beside the text
reference adapter and the ``tally`` series adapter, which must land untouched. Every test runs
the real job with the real sandbox (ADR 0030); nothing is mocked.
"""

import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.registry import AdapterRegistry
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.knowledge import Known
from neptune.model.provenance import TransformRecord
from neptune.model.world import DocumentBlock
from neptune.runtime import (
    IngestJob,
    Isolation,
    JobError,
    JobEvent,
    JobOptions,
    JobOutcome,
    JobState,
    Limits,
    confine,
)
from neptune.store.package import read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
MIB: Final = 1024 * 1024


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / "adapters" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TALLY: Final = _load("tally_adapter")
HOSTILE: Final = _load("hostile_adapter")


def registry() -> AdapterRegistry:
    return AdapterRegistry(
        [*builtin_adapters(), TALLY.TallyAdapter(rows_per_chunk=2), HOSTILE.HostileAdapter()]
    )


def sandboxed(**limits: int) -> JobOptions:
    return JobOptions(attempts=2, limits=Limits(**limits))


def children_of(pid: int) -> list[int]:
    """The processes ``pid`` has started that are still alive (Linux ``/proc``)."""
    tasks = Path(f"/proc/{pid}/task")
    found: list[int] = []
    for task in tasks.iterdir() if tasks.exists() else ():
        try:
            found += [int(child) for child in (task / "children").read_text().split()]
        except OSError:  # the task ended while it was read
            continue
    return found


class Run:
    """One job's outcome, the package read back, every event, and the workspace it used."""

    def __init__(
        self,
        root: Path,
        tmp_path: Path,
        options: JobOptions,
        *,
        name: str = "package",
        home: str = "home",
        on_event: Callable[[JobEvent], None] | None = None,
    ) -> None:
        self.events: list[JobEvent] = []
        self.home = tmp_path / home
        self.workspace = Workspace(self.home)

        def record(event: JobEvent) -> None:
            self.events.append(event)
            if on_event is not None:
                on_event(event)

        job = IngestJob(root, tmp_path / name, self.workspace, registry(), options, on_event=record)
        self.outcome: JobOutcome = job.run()
        assert self.outcome.state is JobState.COMMITTED
        self.package: Any = read_package(tmp_path / name)

    def codes(self) -> list[str]:
        return sorted(finding.code for finding in self.outcome.findings)

    def finding(self, code: str) -> IngestFinding:
        (found,) = [f for f in self.outcome.findings if f.code == code]
        return found

    def of(self, kind: str) -> list[JobEvent]:
        return [event for event in self.events if event.kind == kind]

    def source(self, path: str) -> object:
        """The content id of the file at ``path``, as the job hashed it."""
        return next(e.details["source"] for e in self.of("source_hashed") if path_of(e) == path)

    def committed_for(self, source: object) -> int:
        return len([e for e in self.of("chunk_committed") if e.details["source"] == source])

    def texts(self) -> list[str]:
        return sorted(
            r.text.value
            for r in self.package.records
            if isinstance(r, DocumentBlock) and isinstance(r.text, Known)
        )

    def staging_is_empty(self) -> bool:
        return not any((self.home / "staging").iterdir())


def path_of(event: JobEvent) -> str:
    location = event.details["location"]
    assert isinstance(location, dict)
    return str(location["path"])


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    (root / "lift.tally").write_bytes(b"TALLY1\n10 1\n20 2\n30 3\n")
    (root / "clean.hostile").write_bytes(HOSTILE.hostile("calm", "quiet"))
    (root / "crash.hostile").write_bytes(HOSTILE.hostile("before", "segfault", "after"))
    (root / "hang.hostile").write_bytes(HOSTILE.hostile("before", "hang", "after"))
    (root / "hog.hostile").write_bytes(HOSTILE.hostile("before", "hog", "after"))
    return root


def test_crash_hang_and_hog_are_findings_and_everything_else_lands(
    corpus: Path, tmp_path: Path
) -> None:
    started = time.monotonic()
    run = Run(corpus, tmp_path, sandboxed(cpu_seconds=10, wall_seconds=2, memory_bytes=256 * MIB))
    assert time.monotonic() - started < 60

    assert run.codes() == [
        "neptune.runtime.adapter_crashed",
        "neptune.runtime.limit_exceeded",
        "neptune.runtime.limit_exceeded",
    ]
    for finding in run.outcome.findings:
        assert (finding.category, finding.severity) == (FindingCategory.FAILED, Severity.ERROR)
        assert finding.details["adapter"] == "hostile" and finding.details["version"] == "1.0.0"
        assert finding.details["step"] == "ingest"
        assert str(finding.details["chunk"]).startswith("chunk:sha256:")
    crash = run.finding("neptune.runtime.adapter_crashed")
    assert crash.details["signal"] == "SIGSEGV" and crash.details["attempts"] == 2
    limits = {
        f.details["limit"]: f.details["value"]
        for f in run.outcome.findings
        if f.code == "neptune.runtime.limit_exceeded"
    }
    assert limits == {"memory_bytes": 256 * MIB, "wall_seconds": 2}

    # Every other source landed whole: the text, the series, the clean hostile file.
    assert len(run.outcome.ingested) == 3
    assert {"calm", "quiet"} <= set(run.texts())
    assert not {"before", "after"} & set(run.texts())  # the attacked sources are quarantined
    assert run.package.series  # the tally's stream
    # Every other chunk of the attacked sources ran and committed, so a fixed adapter redoes one.
    for path in ("crash.hostile", "hang.hostile", "hog.hostile"):
        assert run.committed_for(run.source(path)) == 3  # the document, before, after
    # Nothing of a stopped call reached the workspace, not even half a chunk.
    for finding in run.outcome.findings:
        assert not run.workspace.committed(str(finding.details["chunk"]))
    assert run.staging_is_empty()

    # A crash may be transient and is retried; a limit would be hit again and is not.
    assert [e.details.get("signal") for e in run.of("chunk_retried")] == ["SIGSEGV"]
    quarantined = sorted(str(e.details["codes"]) for e in run.of("source_quarantined"))
    assert quarantined == sorted(
        str([code])
        for code in (
            "neptune.runtime.adapter_crashed",
            "neptune.runtime.limit_exceeded",
            "neptune.runtime.limit_exceeded",
        )
    )
    (ready,) = run.of("sandbox_ready")
    assert ready.details == {
        "isolation": "subprocess",
        "landlock": confine.landlock_abi(),
        "limits": {
            "cpu_seconds": 10,
            "memory_bytes": 256 * MIB,
            "reply_bytes": 64 * MIB,
            "wall_seconds": 2,
        },
    }
    # The receipt names the policy the findings were made under.
    (runtime,) = [
        r
        for r in run.package.records
        if isinstance(r, TransformRecord) and r.adapter_id == "neptune.runtime"
    ]
    assert dict(runtime.config) == {
        "attempts": 2,
        "cpu_seconds": 10,
        "isolation": "subprocess",
        "memory_bytes": 256 * MIB,
        "reply_bytes": 64 * MIB,
        "wall_seconds": 2,
    }
    assert {f.transform for f in run.outcome.findings} == {runtime.id}


def test_a_spinning_parser_is_stopped_at_its_cpu_limit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "spin.hostile").write_bytes(HOSTILE.hostile("ok", "spin"))
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    run = Run(root, tmp_path, sandboxed(cpu_seconds=1, wall_seconds=60))
    finding = run.finding("neptune.runtime.limit_exceeded")
    assert (finding.details["limit"], finding.details["value"]) == ("cpu_seconds", 1)
    assert len(run.outcome.ingested) == 1 and run.staging_is_empty()


def test_a_plan_that_hangs_or_crashes_is_a_finding(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "slow.hostile").write_bytes(HOSTILE.hostile("plan-hang", "x"))
    (root / "broken.hostile").write_bytes(HOSTILE.hostile("plan-segfault", "x"))
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    run = Run(root, tmp_path, sandboxed(wall_seconds=1))
    assert run.codes() == ["neptune.runtime.adapter_crashed", "neptune.runtime.limit_exceeded"]
    stopped = run.finding("neptune.runtime.limit_exceeded")
    assert stopped.details["step"] == "plan" and "chunk" not in stopped.details
    assert stopped.details["limit"] == "wall_seconds"
    crashed = run.finding("neptune.runtime.adapter_crashed")
    assert crashed.details["step"] == "plan" and crashed.details["signal"] == "SIGSEGV"
    assert crashed.details["attempts"] == 1  # a plan is not retried; the next job plans again
    causes = sorted(
        str(e.details.get("limit") or e.details.get("signal")) for e in run.of("plan_failed")
    )
    assert causes == ["SIGSEGV", "wall_seconds"]
    assert len(run.outcome.ingested) == 1
    assert len(list((run.home / "plans").rglob("*.json"))) == 1  # only the notes' plan is saved


@pytest.mark.parametrize(
    ("attack", "code", "cause"),
    [
        ("die", "neptune.runtime.adapter_crashed", {"signal": "SIGKILL"}),
        ("abort", "neptune.runtime.adapter_crashed", {"signal": "SIGABRT"}),
        ("exit", "neptune.runtime.adapter_crashed", {"exit_status": 7}),
        ("quit", "neptune.runtime.chunk_failed", {"error": "SystemExit", "step": "ingest"}),
    ],
)
def test_every_way_of_dying_is_a_finding(
    tmp_path: Path, attack: str, code: str, cause: dict[str, object]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "victim.hostile").write_bytes(HOSTILE.hostile("ok", attack))
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    run = Run(root, tmp_path, sandboxed())
    finding = run.finding(code)
    assert {key: finding.details[key] for key in cause} == cause
    assert finding.details["attempts"] == 2  # retried, then given up
    (retried,) = run.of("chunk_retried")
    assert all(retried.details[key] == value for key, value in cause.items() if key != "step")
    assert not run.workspace.committed(str(finding.details["chunk"]))
    assert len(run.outcome.ingested) == 1 and run.staging_is_empty()


@pytest.mark.parametrize(
    "attack", ["socket", "fork", "exec", "kill-parent", "write", "setown", "fioasync"]
)
def test_a_parser_cannot_reach_the_network_processes_or_files(tmp_path: Path, attack: str) -> None:
    escaped = tmp_path / "escaped"
    line = f"write {escaped}" if attack == "write" else attack
    root = tmp_path / "root"
    root.mkdir()
    (root / "attack.hostile").write_bytes(HOSTILE.hostile("ok", line))
    run = Run(root, tmp_path, sandboxed())
    finding = run.finding("neptune.runtime.chunk_failed")
    denied = "OSError" if attack == "write" and not confine.landlock_abi() else "PermissionError"
    assert (finding.details["error"], finding.details["step"]) == (denied, "ingest")
    assert not escaped.exists() or escaped.read_bytes() == b""  # not one byte escaped
    assert sorted(p.name for p in root.iterdir()) == ["attack.hostile"]  # the source untouched


def test_a_probe_that_crashes_takes_only_its_adapter_out(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "odd.hostile").write_bytes(HOSTILE.hostile("probe-segfault", "words"))
    run = Run(root, tmp_path, sandboxed())
    (failed,) = run.of("probe_failed")
    assert failed.details["adapter"] == "hostile" and failed.details["signal"] == "SIGSEGV"
    (selected,) = run.of("source_selected")
    assert selected.details["adapter"] == "text"  # the file is ASCII: the text adapter reads it
    assert run.codes() == [] and len(run.outcome.ingested) == 1


def test_the_sandbox_changes_nothing_in_the_package(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    shutil.copy(FIXTURES / "text" / "corrupted.txt", root / "corrupted.txt")
    (root / "lift.tally").write_bytes(b"TALLY1\n10 1\n20 2\noops\n40 4\n")
    (root / "clean.hostile").write_bytes(HOSTILE.hostile("calm", "quiet"))
    boxed = Run(root, tmp_path, sandboxed(), name="boxed", home="boxed-home")
    again = Run(root, tmp_path, sandboxed(), name="again", home="again-home")
    trusted = Run(
        root,
        tmp_path,
        JobOptions(isolation=Isolation.IN_PROCESS),
        name="trusted",
        home="trusted-home",
    )
    assert boxed.outcome.package == again.outcome.package == trusted.outcome.package
    adapter_codes = {f.code for f in boxed.package.receipt.findings}
    assert {"tally.bad_row", "text.invalid_utf8"} <= adapter_codes  # the adapters still spoke


def test_findings_from_the_sandbox_are_deterministic(corpus: Path, tmp_path: Path) -> None:
    for path in ("hang.hostile", "hog.hostile"):
        (corpus / path).unlink()
    first = Run(corpus, tmp_path, sandboxed(), name="first", home="first-home")
    second = Run(corpus, tmp_path, sandboxed(), name="second", home="second-home")
    assert first.outcome.package == second.outcome.package
    assert first.outcome.findings == second.outcome.findings


def test_the_next_job_retries_only_what_was_stopped(corpus: Path, tmp_path: Path) -> None:
    for path in ("hang.hostile", "hog.hostile"):
        (corpus / path).unlink()
    first = Run(corpus, tmp_path, sandboxed(), name="first")
    second = Run(corpus, tmp_path, sandboxed(), name="second")
    crashed = second.source("crash.hostile")
    assert first.outcome.package == second.outcome.package
    skipped = [e for e in second.of("chunk_skipped") if e.details["source"] == crashed]
    assert len(skipped) == 3  # the document, before, after: committed by the first job
    assert [e.details["source"] for e in second.of("chunk_retried")] == [crashed]
    assert second.of("chunk_committed") == []  # nothing new: only the crash was tried again


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # the killer thread is alive at fork
def test_killing_the_sandboxed_process_leaves_no_partial_chunk(
    corpus: Path, tmp_path: Path
) -> None:
    for path in ("crash.hostile", "hog.hostile"):
        (corpus / path).unlink()
    paths: dict[object, str] = {}
    committed: dict[object, int] = {}
    hanging = threading.Event()
    killed: list[int] = []

    def watch(event: JobEvent) -> None:
        """The hang is the call after hang.hostile's second commit (its document, "before")."""
        if event.kind == "source_hashed":
            paths[event.details["source"]] = path_of(event)
        elif event.kind == "chunk_committed":
            source = event.details["source"]
            committed[source] = committed.get(source, 0) + 1
            if paths.get(source) == "hang.hostile" and committed[source] == 2:
                hanging.set()

    def killer() -> None:  # what an OOM killer or an operator does to the parser
        if not hanging.wait(60):
            return
        deadline = time.monotonic() + 60
        while not killed and time.monotonic() < deadline:
            for pid in children_of(os.getpid()):
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            time.sleep(0.01)

    thread = threading.Thread(target=killer)
    thread.start()
    try:
        options = JobOptions(attempts=1, limits=Limits(wall_seconds=60))
        run = Run(corpus, tmp_path, options, on_event=watch)
    finally:
        hanging.set()
        thread.join()
    assert len(killed) == 1, "the hang was never reached"
    finding = run.finding("neptune.runtime.adapter_crashed")
    assert finding.details["signal"] == "SIGKILL" and finding.details["attempts"] == 1
    assert not run.workspace.committed(str(finding.details["chunk"]))
    assert run.committed_for(run.source("hang.hostile")) == 3  # "after" ran too
    assert run.staging_is_empty()
    assert len(run.outcome.ingested) == 3


def test_killing_the_job_kills_its_sandboxed_call_and_the_rerun_completes(
    corpus: Path, tmp_path: Path
) -> None:
    for path in ("crash.hostile", "hog.hostile"):
        (corpus / path).unlink()
    home, script = tmp_path / "home", FIXTURES / "runtime" / "sandboxed_job.py"
    job = subprocess.Popen([sys.executable, str(script), str(corpus), str(home), "x", "600"])
    hang = None
    try:
        first_seen: dict[int, float] = {}
        deadline = time.monotonic() + 60
        while hang is None and time.monotonic() < deadline:
            now = time.monotonic()
            for pid in children_of(job.pid):
                first_seen.setdefault(pid, now)
                if now - first_seen[pid] > 1.0:  # every other call lasts milliseconds
                    hang = pid
            time.sleep(0.02)
        assert hang is not None, "the job never reached the hang"
    finally:
        job.kill()  # SIGKILL: no handler, no cleanup
        job.wait()
    deadline = time.monotonic() + 10
    while Path(f"/proc/{hang}").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not Path(f"/proc/{hang}").exists()  # it died with the job: nothing runs on
    assert not (tmp_path / "x").exists()

    rerun = Run(corpus, tmp_path, sandboxed(wall_seconds=1), home="home")
    stopped = rerun.finding("neptune.runtime.limit_exceeded")
    assert stopped.details["limit"] == "wall_seconds"
    assert not rerun.workspace.committed(str(stopped.details["chunk"]))
    assert len(rerun.of("chunk_skipped")) >= 3  # what the killed job committed is kept
    assert len(rerun.outcome.ingested) == 3 and rerun.staging_is_empty()


def test_a_host_that_cannot_sandbox_fails_the_job_before_any_work(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lacking() -> confine.Host:
        raise confine.ConfineError("platform", "the sandbox needs Linux, not plan9")

    with monkeypatch.context() as patch:
        patch.setattr(confine, "host", lacking)
        with pytest.raises(JobError, match="in_process"):
            IngestJob(corpus, tmp_path / "p", Workspace(tmp_path / "home"), registry())
    assert not list((tmp_path / "home" / "ledgers").iterdir())  # nothing was walked
    for path in ("crash.hostile", "hang.hostile", "hog.hostile"):
        (corpus / path).unlink()  # trusted adapters only, in process
    trusted = Run(corpus, tmp_path, JobOptions(isolation=Isolation.IN_PROCESS))
    assert len(trusted.outcome.ingested) == 3
    assert trusted.of("sandbox_ready")[0].details == {"isolation": "in_process"}


def test_a_host_below_the_landlock_floor_fails_closed_unless_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    # A segfault is safe even without Landlock (seccomp is independent of it) and gives the job a
    # runtime finding, so the receipt carries the runtime transform it was made under.
    (root / "crash.hostile").write_bytes(HOSTILE.hostile("before", "segfault", "after"))
    real = confine.host()
    monkeypatch.setattr(confine, "host", lambda: confine.Host(real.arch, 0))

    # Fail closed by default: a host that cannot keep the source immutable fails the job.
    with pytest.raises(JobError, match="allow_degraded_sandbox"):
        IngestJob(root, tmp_path / "p", Workspace(tmp_path / "closed"), registry())
    assert not (tmp_path / "p").exists()

    # Degraded by explicit choice: the job runs and records exactly what was lost.
    run = Run(root, tmp_path, JobOptions(allow_degraded_sandbox=True), home="deg-home")
    (ready,) = run.of("sandbox_ready")
    assert ready.details["landlock"] == 0
    lost = ready.details["degraded"]
    assert isinstance(lost, list) and "truncate a file, the source included" in lost
    (runtime,) = [
        r
        for r in run.package.records
        if isinstance(r, TransformRecord) and r.adapter_id == "neptune.runtime"
    ]
    assert runtime.config["degraded"] == lost  # the receipt records the lost guarantees too
    assert len(run.outcome.ingested) == 1  # the text landed; the crashing source is quarantined


@pytest.mark.parametrize(
    ("options", "problem"),
    [
        ({"isolation": "subprocess"}, "isolation must be an Isolation"),
        ({"limits": {"cpu_seconds": 1}}, "limits must be Limits"),
        (
            {"isolation": Isolation.IN_PROCESS, "limits": Limits(cpu_seconds=1)},
            "in-process calls have none",
        ),
        ({"allow_degraded_sandbox": "yes"}, "allow_degraded_sandbox must be a bool"),
        (
            {"isolation": Isolation.IN_PROCESS, "allow_degraded_sandbox": True},
            "in-process calls have none",
        ),
    ],
)
def test_isolation_options_are_checked(options: dict[str, Any], problem: str) -> None:
    with pytest.raises(JobError, match=problem):
        JobOptions(**options)
