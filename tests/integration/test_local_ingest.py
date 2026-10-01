"""MVL-73 acceptance, end to end: ingest a folder locally through the workspace, then package it.

The driver below is what the runtime (MVL-6) will run, without its phases, events or isolation:
scan into the root's persisted ledger, read each source in place, select an adapter, plan, ingest
each chunk not yet committed, commit it, and assemble the package from the workspace.
"""

import importlib.util
import os
import shutil
import socket
import stat
import struct
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pyarrow as pa
import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.check import check_chunk_output, check_plan
from neptune.adapters.contract import PROBE_HEAD_SIZE, ProbeHints, configure
from neptune.adapters.registry import AdapterRegistry
from neptune.discovery.reader import LocalReader, SourceChangedError
from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource, SourceAccessError
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ContentId, RecordId
from neptune.model.package import Storage
from neptune.model.source import LocalPath, SourceRevision
from neptune.store.assemble import _sibling, assemble, export
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


Ingested = set[tuple[ContentId, RecordId]]


def ingest_into(
    root: Path,
    workspace: Workspace,
    adapters: AdapterRegistry,
    *,
    on_chunk: Callable[[str], None] = lambda _: None,
) -> tuple[SourceLedger, Ingested]:
    """Scan, plan, ingest and commit; return what assembling needs."""
    source = LocalSource(root)
    ledger = workspace.load_ledger(root)
    observations = scan(source, ledger).observations
    workspace.save_ledger(root, ledger)
    ingested: Ingested = set()
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
    return ledger, ingested


def ingest_local(
    root: Path,
    workspace: Workspace,
    adapters: AdapterRegistry,
    destination: Path,
    *,
    on_chunk: Callable[[str], None] = lambda _: None,
) -> ContentId:
    ledger, ingested = ingest_into(root, workspace, adapters, on_chunk=on_chunk)
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
    exported = export(tmp_path / "package", tmp_path / "portable", LocalSource(corpus))
    shutil.rmtree(corpus)  # the evidence is gone; the export does not need it
    original, portable = read_package(tmp_path / "package"), read_package(tmp_path / "portable")
    assert portable.id == exported != original.id
    assert portable.receipt == original.receipt
    assert portable.records == original.records
    assert {h.storage for h in portable.manifest.sources} == {Storage.MATERIALISED}
    assert set(portable.blobs) == {h.content_id for h in portable.manifest.sources}


def _nothing_at(destination: Path) -> bool:
    """Neither the destination nor a hidden staging directory beside it was left behind.

    Staging is named ``.<name>.<hex>`` (``assemble._sibling``), so it starts with a dot.
    """
    return not any(
        entry.name == destination.name or entry.name.startswith(f".{destination.name}.")
        for entry in destination.parent.iterdir()
    )


def test_nothing_at_sees_leftover_staging(tmp_path: Path) -> None:
    """The helper the failure tests rely on: a planted leftover of either kind is seen."""
    destination = tmp_path / "portable"
    (tmp_path / "portable-other").mkdir()  # a neighbour that only shares the prefix
    assert _nothing_at(destination)
    planted = _sibling(destination)  # exactly what a failed export would leave
    assert planted.name.startswith(".portable.")
    assert not _nothing_at(destination)
    planted.rmdir()
    destination.mkdir()
    assert not _nothing_at(destination)


def test_exporting_without_the_sources_fails_loudly(corpus: Path, tmp_path: Path) -> None:
    ingest_local(corpus, Workspace(tmp_path / "home"), registry(), tmp_path / "package")
    (corpus / "notes.txt").unlink()
    with pytest.raises(SourceAccessError, match=r"notes\.txt"):
        export(tmp_path / "package", tmp_path / "portable", LocalSource(corpus))
    assert _nothing_at(tmp_path / "portable")


def test_exporting_a_source_that_changed_since_hashing_fails(corpus: Path, tmp_path: Path) -> None:
    ingest_local(corpus, Workspace(tmp_path / "home"), registry(), tmp_path / "package")
    (corpus / "blob.bin").write_bytes(b"\x00\x01BINARY\x00")  # the same size, other bytes
    with pytest.raises(PackageError, match="do not hash"):
        export(tmp_path / "package", tmp_path / "portable", LocalSource(corpus))
    assert _nothing_at(tmp_path / "portable")


def test_a_symlink_at_a_sources_location_is_not_that_source(corpus: Path, tmp_path: Path) -> None:
    """The walk's policy applies to export: a link where the file was is refused, not followed."""
    ingest_local(corpus, Workspace(tmp_path / "home"), registry(), tmp_path / "package")
    (corpus / "notes.txt").rename(corpus / "moved.txt")
    (corpus / "notes.txt").symlink_to("moved.txt")
    with pytest.raises(SourceAccessError, match="symlink"):
        export(tmp_path / "package", tmp_path / "portable", LocalSource(corpus))
    assert _nothing_at(tmp_path / "portable")


def test_an_uncited_source_seen_absent_stays_referenced(corpus: Path, tmp_path: Path) -> None:
    """Nothing in the second package was read from the deleted file: the export goes ahead."""
    workspace = Workspace(tmp_path / "home")
    ingest_local(corpus, workspace, registry(), tmp_path / "first")
    notes = read_package(tmp_path / "first").receipt.sources
    (gone,) = (s.content_id for s in notes if s.location == LocalPath("notes.txt"))
    (corpus / "notes.txt").unlink()
    ingest_local(corpus, workspace, registry(), tmp_path / "second")  # the ledger gains an absence
    export(tmp_path / "second", tmp_path / "portable", LocalSource(corpus))
    storage = {
        h.content_id: h.storage for h in read_package(tmp_path / "portable").manifest.sources
    }
    assert storage.pop(gone) is Storage.REFERENCED
    assert set(storage.values()) == {Storage.MATERIALISED}


def test_a_cited_source_seen_absent_fails_the_export(corpus: Path, tmp_path: Path) -> None:
    """The package's records were read from a file whose last known state is absence."""
    workspace = Workspace(tmp_path / "home")
    ledger, ingested = ingest_into(corpus, workspace, registry())
    (corpus / "notes.txt").unlink()
    scan(LocalSource(corpus), ledger)  # the ledger gains an absence; the plans still cite it
    assemble(tmp_path / "package", workspace, ledger, ingested)
    with pytest.raises(PackageError, match="no local location holds sources the package cites"):
        export(tmp_path / "package", tmp_path / "portable", LocalSource(corpus))
    assert _nothing_at(tmp_path / "portable")


def test_a_package_never_cites_a_source_its_ledger_lacks(corpus: Path, tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "home")
    _, ingested = ingest_into(corpus, workspace, registry())
    with pytest.raises(PackageError, match="the ledger does not hold it"):
        assemble(tmp_path / "package", workspace, SourceLedger(), ingested)
    assert _nothing_at(tmp_path / "package")


def _content_at(ledger: SourceLedger, path: str) -> ContentId:
    revision = ledger.head(LocalPath(path))
    assert isinstance(revision, SourceRevision)
    return revision.content_id


def test_a_materialised_source_is_copied_into_the_package(corpus: Path, tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "home")
    ledger, ingested = ingest_into(corpus, workspace, registry())
    lift = _content_at(ledger, "lift.tally")
    assemble(
        tmp_path / "package", workspace, ledger, ingested, materialise={lift: corpus / "lift.tally"}
    )
    package = read_package(tmp_path / "package")
    assert set(package.blobs) == {lift}
    storage = {h.content_id: h.storage for h in package.manifest.sources}
    assert storage.pop(lift) is Storage.MATERIALISED
    assert set(storage.values()) == {Storage.REFERENCED}


def test_a_source_that_changes_while_it_is_copied_fails_the_package(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(tmp_path / "home")
    ledger, ingested = ingest_into(corpus, workspace, registry())
    lift = _content_at(ledger, "lift.tally")
    copy = shutil.copyfile

    def copy_then_change(source: Path, target: Path) -> None:
        copy(source, target)
        data = target.read_bytes()
        target.write_bytes(data[:-1] + b"6")  # the same size, other bytes: as if written mid-copy

    monkeypatch.setattr(shutil, "copyfile", copy_then_change)
    with pytest.raises(PackageError, match="changed while it was copied"):
        assemble(
            tmp_path / "package",
            workspace,
            ledger,
            ingested,
            materialise={lift: corpus / "lift.tally"},
        )
    assert _nothing_at(tmp_path / "package")


def test_only_copied_files_are_read_back(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The export hashes each source once, where it lands, and never re-reads the whole export."""
    ingest_local(corpus, Workspace(tmp_path / "home"), registry(), tmp_path / "package")
    opened: list[Path] = []
    read = read_package

    def recording(root: Path) -> Any:
        opened.append(root)
        return read(root)

    monkeypatch.setattr("neptune.store.assemble.read_package", recording)
    export(tmp_path / "package", tmp_path / "portable", LocalSource(corpus))
    assert opened == [tmp_path / "package"]
    assert read_package(tmp_path / "portable").receipt == read_package(tmp_path / "package").receipt


def test_packages_and_exports_are_flushed_before_they_appear(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every file and directory is fsynced before the rename that publishes it, and the
    directory it is renamed into is fsynced after."""
    workspace = Workspace(tmp_path / "home")
    ledger, ingested = ingest_into(corpus, workspace, registry())
    events: list[tuple[str, object]] = []
    fsync, rename = os.fsync, os.rename

    def recording_fsync(descriptor: int) -> None:
        held = os.fstat(descriptor)
        fsync(descriptor)
        events.append(("fsync", (held.st_dev, held.st_ino)))

    def recording_rename(source: Any, target: Any, *args: Any, **kwargs: Any) -> None:
        rename(source, target, *args, **kwargs)
        events.append(("rename", Path(target)))

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "rename", recording_rename)
    out = tmp_path / "out"
    out.mkdir()
    assemble(out / "package", workspace, ledger, ingested)
    export(out / "package", out / "portable", LocalSource(corpus))

    begun = 0  # each package's own window: from the previous publish to its own
    for package in (out / "package", out / "portable"):
        published = events.index(("rename", package))
        before, after = events[begun:published], events[published + 1 :]
        written = [package, *package.rglob("*")]
        assert len(written) > 10
        for path in written:
            named = path.stat()
            assert ("fsync", (named.st_dev, named.st_ino)) in before, path
        parent = out.stat()
        assert ("fsync", (parent.st_dev, parent.st_ino)) in after
        begun = published + 1


def test_a_package_is_made_under_the_umask_without_touching_it(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The umask is process state; reading it means setting it, which races other threads."""
    workspace = Workspace(tmp_path / "home")
    ledger, ingested = ingest_into(corpus, workspace, registry())
    umask = os.umask

    def refuse(_: int) -> int:
        raise AssertionError("the umask was changed")

    previous = umask(0o027)
    try:
        monkeypatch.setattr(os, "umask", refuse)
        assemble(tmp_path / "package", workspace, ledger, ingested)
        export(tmp_path / "package", tmp_path / "portable", LocalSource(corpus))
    finally:
        umask(previous)
    for package in (tmp_path / "package", tmp_path / "portable"):
        assert stat.S_IMODE(package.stat().st_mode) == 0o750
        assert stat.S_IMODE((package / "manifest.json").stat().st_mode) == 0o640


def test_an_export_is_written_once(corpus: Path, tmp_path: Path) -> None:
    ingest_local(corpus, Workspace(tmp_path / "home"), registry(), tmp_path / "package")
    (tmp_path / "taken").mkdir()
    with pytest.raises(PackageError, match="written once"):
        export(tmp_path / "package", tmp_path / "taken", LocalSource(corpus))
    assert list((tmp_path / "taken").iterdir()) == []


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
    before = recording.stat()
    assert before.st_size > 2 * 1024**3

    workspace = Workspace(tmp_path / "home")
    arrow = pa.default_memory_pool()
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
    # Arrow's memory, outside the Python heap: the pool's peak over the whole process, so this
    # run's peak included, bounded by row groups and merge batches, not the source.
    assert arrow.max_memory() < 128 * 1024**2
    ((_, series),) = package.series.items()
    assert sum(1 for _ in read_rows(series)) == frames
    after = recording.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert after.st_blocks == before.st_blocks  # still as sparse: nothing wrote the payloads
