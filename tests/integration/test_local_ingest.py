"""MVL-73 acceptance, end to end: ingest a folder locally through the workspace, then package it.

The driver below is what the runtime (MVL-6) will run, without its phases, events or isolation:
scan into the root's persisted ledger, read each source in place, select an adapter, plan, ingest
each chunk not yet committed, commit it, and assemble the package from the workspace.
"""

import importlib.util
import shutil
import socket
import struct
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.check import check_chunk_output, check_plan
from neptune.adapters.contract import PROBE_HEAD_SIZE, ProbeHints, configure
from neptune.adapters.registry import AdapterRegistry
from neptune.discovery.reader import LocalReader, SourceChangedError
from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource
from neptune.model.ids import ContentId, RecordId
from neptune.model.package import Storage
from neptune.model.source import LocalPath, SourceRevision
from neptune.store.assemble import assemble, export
from neptune.store.package import PackageError, read_package
from neptune.store.series import SERIES_SETTINGS, read_rows
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"


def _load(name: str) -> ModuleType:
    path = FIXTURES / "adapters" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TALLY: Final = _load("tally_adapter")
FRAMELOG: Final = _load("framelog_adapter")


def registry() -> AdapterRegistry:
    return AdapterRegistry(
        [*builtin_adapters(), TALLY.TallyAdapter(), FRAMELOG.FrameLogAdapter(frames_per_chunk=64)]
    )


def ingest_local(
    root: Path,
    workspace: Workspace,
    adapters: AdapterRegistry,
    destination: Path,
    *,
    on_chunk: Callable[[str], None] = lambda _: None,
) -> ContentId:
    source = LocalSource(root)
    ledger = workspace.load_ledger(root)
    observations = scan(source, ledger).observations
    workspace.save_ledger(root, ledger)
    ingested: set[tuple[ContentId, RecordId]] = set()
    for observation in observations:
        location = observation.revision.location
        assert isinstance(location, LocalPath)
        artifact = ledger.artifact(observation.revision.content_id)
        assert artifact is not None
        with LocalReader(source, location, artifact) as reader:
            head = reader.read(0, PROBE_HEAD_SIZE)
            selection = adapters.select(head, ProbeHints(location.parts[-1], artifact.size))
            if selection.adapter is None:
                continue
            adapter = adapters.get(selection.adapter)
            config = configure(adapter.descriptor)
            key = (artifact.content_id, config.transform.id)
            if key in ingested:
                continue  # the same bytes at another location: one ingest
            plan = adapter.plan(reader, config)
            check_plan(adapter.descriptor, reader, config, plan)
            workspace.save_plan(config.transform, plan.chunks, plan.findings)
            for chunk in plan.chunks:
                if workspace.committed(chunk.id):
                    continue  # committed by an earlier run: resume skips it
                on_chunk(chunk.id)
                output = adapter.ingest(reader, chunk, config)
                check_chunk_output(adapter.descriptor, reader, config, chunk, output)
                workspace.commit(chunk, output.records, output.findings, output.series)
            ingested.add(key)
    return assemble(destination, workspace, ledger, ingested)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "site-visit"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    (root / "lift.tally").write_bytes(b"TALLY1\n10 1\n20 2\noops\n40 4\n50 5\n")
    (root / "copy.tally").write_bytes(b"TALLY1\n10 1\n20 2\noops\n40 4\n50 5\n")
    (root / "cam.framelog").write_bytes(
        FRAMELOG.frame_log([(t, bytes([t % 256]) * 100) for t in range(300)])
    )
    (root / "blob.bin").write_bytes(b"\x00\x01binary\x00")
    return root


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> Any:
        raise AssertionError("a local ingest reached for the network")

    for target, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    ):
        monkeypatch.setattr(target, name, refuse)


@pytest.mark.usefixtures("no_network")
def test_a_folder_ingests_into_a_package_through_the_workspace(
    corpus: Path, tmp_path: Path
) -> None:
    workspace = Workspace(tmp_path / "home")
    package_id = ingest_local(corpus, workspace, registry(), tmp_path / "package")
    package = read_package(tmp_path / "package")
    assert package.id == package_id
    assert {h.storage for h in package.manifest.sources} == {Storage.REFERENCED}
    assert package.manifest.store == {"series": SERIES_SETTINGS}
    assert len(package.series) == 2  # the tally stream (twice the same bytes: one) and the frames
    rows = {stream: list(read_rows(path)) for stream, path in package.series.items()}
    assert sorted(len(found) for found in rows.values()) == [4, 300]
    read_by = {str(s.location.to_json()["path"]): len(s.read_by) for s in package.receipt.sources}
    assert read_by == {
        "blob.bin": 0,
        "cam.framelog": 1,
        "copy.tally": 1,
        "lift.tally": 1,
        "notes.txt": 1,
    }
    assert workspace.local_only


def test_a_second_run_resumes_and_builds_the_same_package(corpus: Path, tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "home")
    seen: list[str] = []

    def crash_after_three(chunk: str) -> None:
        if len(seen) == 3:
            raise KeyboardInterrupt("the process dies")
        seen.append(chunk)

    with pytest.raises(KeyboardInterrupt):
        ingest_local(corpus, workspace, registry(), tmp_path / "a", on_chunk=crash_after_three)
    first_run = list(seen)
    resumed: list[str] = []
    first = ingest_local(corpus, workspace, registry(), tmp_path / "a", on_chunk=resumed.append)
    assert not set(resumed) & set(first_run)  # nothing committed is ingested again
    again: list[str] = []
    second = ingest_local(corpus, workspace, registry(), tmp_path / "b", on_chunk=again.append)
    assert again == []  # all cached
    fresh = ingest_local(corpus, Workspace(tmp_path / "other"), registry(), tmp_path / "c")
    assert first == second == fresh
    with pytest.raises(PackageError, match="written once"):
        ingest_local(corpus, workspace, registry(), tmp_path / "a")


def test_an_exported_package_holds_every_source_and_the_same_receipt(
    corpus: Path, tmp_path: Path
) -> None:
    workspace = Workspace(tmp_path / "home")
    ingest_local(corpus, workspace, registry(), tmp_path / "package")
    exported = export(tmp_path / "package", tmp_path / "portable", corpus)
    shutil.rmtree(corpus)  # the evidence is gone; the export does not need it
    original, portable = read_package(tmp_path / "package"), read_package(tmp_path / "portable")
    assert portable.id == exported != original.id
    assert portable.receipt == original.receipt
    assert portable.records == original.records
    assert {h.storage for h in portable.manifest.sources} == {Storage.MATERIALISED}
    assert set(portable.blobs) == {h.content_id for h in portable.manifest.sources}


def test_exporting_without_the_sources_fails_loudly(corpus: Path, tmp_path: Path) -> None:
    ingest_local(corpus, Workspace(tmp_path / "home"), registry(), tmp_path / "package")
    (corpus / "notes.txt").unlink()
    with pytest.raises(PackageError, match="is not under"):
        export(tmp_path / "package", tmp_path / "portable", corpus)
    assert not (tmp_path / "portable").exists()


def test_a_source_that_changes_after_hashing_is_never_read(corpus: Path, tmp_path: Path) -> None:
    source, workspace = LocalSource(corpus), Workspace(tmp_path / "home")
    ledger = workspace.load_ledger(corpus)
    scan(source, ledger)
    (corpus / "notes.txt").write_bytes(b"Site visit - dock 5 (rewritten after the scan)\n" * 6)
    revision = ledger.head(LocalPath("notes.txt"))
    assert isinstance(revision, SourceRevision)
    artifact = ledger.artifact(revision.content_id)
    assert artifact is not None
    with pytest.raises(SourceChangedError):
        LocalReader(source, LocalPath("notes.txt"), artifact).read(0, 10)


@pytest.mark.slow
@pytest.mark.usefixtures("no_network")
def test_a_multi_gb_run_ingests_locally_with_no_copy_and_bounded_memory(tmp_path: Path) -> None:
    """MVL-16's acceptance: 2 GiB of camera frames, ingested in place, offline, bounded memory."""
    root = tmp_path / "run"
    root.mkdir()
    recording = root / "camera.framelog"
    frame, frames = 4 * 1024 * 1024, 512
    with recording.open("wb") as stream:  # sparse: headers are written, payloads are holes
        stream.write(FRAMELOG.MAGIC)
        for index in range(frames):
            offset = len(FRAMELOG.MAGIC) + index * (12 + frame)
            stream.seek(offset)
            stream.write(struct.pack("<QI", 1_000_000 * index, frame))
        stream.truncate(len(FRAMELOG.MAGIC) + frames * (12 + frame))
    size = recording.stat().st_size
    assert size > 2 * 1024**3
    before = recording.stat()

    workspace = Workspace(tmp_path / "home")
    tracemalloc.start()
    try:
        package_id = ingest_local(root, workspace, registry(), tmp_path / "package")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    def disk(path: Path) -> int:
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())

    package = read_package(tmp_path / "package")
    assert package.id == package_id
    assert disk(tmp_path / "package") < 4 * 1024**2  # no copy of the 2 GiB
    assert disk(tmp_path / "home") < 4 * 1024**2
    assert {h.storage for h in package.manifest.sources} == {Storage.REFERENCED}
    assert peak < 96 * 1024**2  # Python memory: bounded by chunks and pieces, not the source
    ((_, series),) = package.series.items()
    assert sum(1 for _ in read_rows(series)) == frames
    after = recording.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert recording.stat().st_blocks * 512 < size  # still sparse: nothing wrote the payloads
