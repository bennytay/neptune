"""A connector's first sync that stops part way keeps what it hashed (MVL-42, ADR 0069 §5).

A bucket larger than the free disk, or a connector that fails on one object, stops the fetch in
``fingerprint`` and fails the job. Every object already fetched and hashed stays in the URI's
ledger, so the retry recognises those by their revision tokens and fetches only the rest, instead
of failing identically on every attempt. Only whole observations are kept: nothing is marked
absent from a pass that did not finish. The connector is the in-process fake; no socket opens.
"""

import errno
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.builtin import default_registry
from neptune.discovery import external
from neptune.discovery.external import ExternalRoot, Spool, SpoolError
from neptune.model.ids import ExternalObjectRef
from neptune.model.source import SourceRevision
from neptune.runtime import IngestJob, JobError, JobEvent, JobOutcome, JobState, Phase
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

URI: Final = "fake://fleet/amr-7/"
KEYS: Final = ("amr-7/a-odometry.csv", "amr-7/b-notes.md", "amr-7/c-events.log", "amr-7/d.txt")
DATA: Final = (
    b"t_ns,x_m\n0,0.0\n1000000,0.4\n",
    b"# AMR 7\n\nDocked twice; the lidar was cleaned before the second run.\n",
    b"dock 1\nundock 1\n",
    b"spare\n",
)


class _Network:
    def require_network(self, purpose: str) -> None:
        return None


@pytest.fixture
def store(fake_store: ModuleType, tmp_path: Path) -> Path:
    store = tmp_path / "store"
    store.mkdir()
    for key, data in zip(KEYS, DATA, strict=True):
        fake_store.put(store, key, data, f"etag-{key}")
    return store


def job(
    fake: ModuleType, store: Path, workspace: Workspace, out: Path, events: list[JobEvent]
) -> IngestJob:
    source = fake.make(URI, network=_Network(), options={"store": str(store)})
    root = ExternalRoot(URI, fake.CONNECTOR, source)
    return IngestJob(root, out, workspace, default_registry(), on_event=events.append)


def held(workspace: Workspace) -> set[str]:
    """The object ids the URI's saved ledger holds a revision of."""
    return {
        head.location.object_id
        for head in workspace.load_ledger(URI).heads()
        if isinstance(head, SourceRevision) and isinstance(head.location, ExternalObjectRef)
    }


def disk_full(monkeypatch: pytest.MonkeyPatch, fake: ModuleType, after: int) -> None:
    """The spool's disk fills on the fetch after ``after`` whole ones."""
    fill = Spool.fill
    calls = [0]

    def filling(self: Spool, *args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        if calls[0] > after:
            raise SpoolError("the spool cannot be written") from OSError(errno.ENOSPC, "full")
        return fill(self, *args, **kwargs)

    monkeypatch.setattr(external.Spool, "fill", filling)


def connector_fails(monkeypatch: pytest.MonkeyPatch, fake: ModuleType, after: int) -> None:
    """The connector raises (not an ``OSError``: its own bug) on the object after ``after``."""
    fetch = fake.FakeSource.fetch
    victim = f"fleet/{KEYS[after]}"

    def fetching(self: Any, entry: Any, start: int, length: int) -> bytes:
        if entry.location.object_id == victim:
            raise RuntimeError("the connector lost its session")
        data: bytes = fetch(self, entry, start, length)
        return data

    monkeypatch.setattr(fake.FakeSource, "fetch", fetching)


@pytest.mark.parametrize(
    ("fault", "says"),
    [(disk_full, "spool"), (connector_fails, "failed to fetch")],
)
def test_a_fetch_that_stops_part_way_keeps_its_hashes_and_the_retry_resumes(
    fake_store: ModuleType,
    store: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: Callable[[pytest.MonkeyPatch, ModuleType, int], None],
    says: str,
) -> None:
    workspace = Workspace(tmp_path / "ws")
    with monkeypatch.context() as patch:
        fault(patch, fake_store, 2)
        first = job(fake_store, store, workspace, tmp_path / "first", [])
        with pytest.raises(JobError, match=says):
            first.run()
    assert first.state is JobState.FAILED and not (tmp_path / "first").exists()
    fetched = {f"fleet/{key}" for key in KEYS[:2]}
    assert held(workspace) == fetched  # the two whole fetches, and nothing it did not see
    assert not workspace.load_ledger(URI).absences()

    events: list[JobEvent] = []
    retry: JobOutcome = job(fake_store, store, workspace, tmp_path / "retry", events).run()
    assert retry.state is JobState.COMMITTED
    (scanned,) = [e for e in events if e.kind == "phase_finished" and e.phase is Phase.FINGERPRINT]
    assert scanned.details["recognised"] == 2  # carried forward by token, not fetched again
    assert held(workspace) == {f"fleet/{key}" for key in KEYS}

    fresh: JobOutcome = job(
        fake_store, store, Workspace(tmp_path / "fresh"), tmp_path / "fresh-pkg", []
    ).run()
    assert retry.package == fresh.package  # resuming changes nothing the package says
