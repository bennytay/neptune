"""The local workspace (ADR 0026): location, settings, ledgers, plans and atomic chunk commits."""

import importlib.util
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.reader import BytesReader
from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource
from neptune.identity import canonical_json
from neptune.identity.revisions import SourceLedger
from neptune.store.series import write_run
from neptune.store.workspace import (
    HOME_VARIABLE,
    LocalOnlyError,
    Workspace,
    WorkspaceError,
    default_home,
)

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "adapters"


def _tally() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tally_adapter", FIXTURES / "tally_adapter.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TALLY: Final = _tally()
DATA: Final = b"TALLY1\n10 1\n20 2\nbad\n40 4\n"


def tally_output() -> SourceOutput:
    return ingest_source(TALLY.TallyAdapter(), BytesReader(DATA))


def commit_all(workspace: Workspace, output: SourceOutput) -> list[bool]:
    return [
        workspace.commit(chunk, out.records, out.findings, out.series)
        for chunk, out in zip(output.plan.chunks, output.outputs, strict=True)
    ]


# --- Location and settings ---------------------------------------------------------------------


def test_the_home_is_explicit_or_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HOME_VARIABLE, str(tmp_path / "explicit"))
    assert default_home() == tmp_path / "explicit"
    monkeypatch.delenv(HOME_VARIABLE)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert default_home() == tmp_path / "xdg" / "neptune"
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    assert default_home() == tmp_path / "user" / ".cache" / "neptune"
    assert Workspace(tmp_path / "w").home == tmp_path / "w"


def test_a_new_workspace_is_versioned_and_local_only(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    assert canonical_json.loads((tmp_path / "workspace.json").read_bytes()) == {
        "format": 1,
        "kind": "neptune_workspace",
        "local_only": True,
    }
    assert workspace.local_only
    with pytest.raises(LocalOnlyError, match="object-store sync needs the network"):
        workspace.require_network("object-store sync")


def test_allowing_the_network_is_explicit_and_remembered(tmp_path: Path) -> None:
    Workspace(tmp_path).allow_network(True)
    reopened = Workspace(tmp_path)
    assert not reopened.local_only
    reopened.require_network("upload")
    reopened.allow_network(False)
    with pytest.raises(LocalOnlyError):
        Workspace(tmp_path).require_network("upload")


def test_a_local_only_refusal_is_not_an_io_error(tmp_path: Path) -> None:
    """Code that shrugs off I/O errors must not shrug off the policy."""
    workspace = Workspace(tmp_path)

    def fetch() -> str:
        try:
            workspace.require_network("fetch")
        except OSError:
            return "skipped, as if the disk had failed"
        return "fetched"

    with pytest.raises(LocalOnlyError):
        fetch()
    assert not issubclass(LocalOnlyError, (OSError, ValueError))


def test_a_directory_that_is_not_a_workspace_is_refused(tmp_path: Path) -> None:
    (tmp_path / "workspace.json").write_bytes(b'{"kind":"other"}')
    with pytest.raises(WorkspaceError, match="not a Neptune workspace"):
        Workspace(tmp_path)
    (tmp_path / "workspace.json").write_bytes(b'{"format":2,"kind":"neptune_workspace"}')
    with pytest.raises(WorkspaceError, match="format 2"):
        Workspace(tmp_path)


# --- Ledgers -----------------------------------------------------------------------------------


def test_a_roots_ledger_survives_between_runs(tmp_path: Path) -> None:
    root, other = tmp_path / "root", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    (root / "a.txt").write_bytes(b"a")
    workspace = Workspace(tmp_path / "home")
    ledger = workspace.load_ledger(root)
    scan(LocalSource(root), ledger)
    workspace.save_ledger(root, ledger)
    again = workspace.load_ledger(root)
    assert again.revisions() == ledger.revisions()
    second = scan(LocalSource(root), again)
    assert not any(o.new_revision or o.new_artifact for o in second.observations)
    (root / "a.txt").unlink()
    gone = scan(LocalSource(root), again)
    workspace.save_ledger(root, again)
    assert len(gone.absences) == 1
    assert len(workspace.load_ledger(root).absences()) == 1
    assert workspace.load_ledger(other).revisions() == ()


def test_every_spelling_of_a_root_names_one_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "evidence" / "root"
    root.mkdir(parents=True)
    (root / "a.txt").write_bytes(b"a")
    (tmp_path / "link").symlink_to(root)
    workspace = Workspace(tmp_path / "home")
    ledger = workspace.load_ledger(root)
    scan(LocalSource(root), ledger)
    workspace.save_ledger(root, ledger)
    monkeypatch.chdir(tmp_path / "evidence")
    for spelling in (
        Path("root"),
        Path("./root/."),
        tmp_path / "evidence" / ".." / "evidence" / "root",
        tmp_path / "link",
    ):
        assert workspace.load_ledger(spelling).revisions() == ledger.revisions(), spelling
    workspace.save_ledger(tmp_path / "link", ledger)
    (directory,) = (tmp_path / "home" / "ledgers").iterdir()
    assert (directory / "root").read_bytes() == os.fsencode(root.resolve())


# --- Plans and chunks --------------------------------------------------------------------------


def test_a_plan_is_kept_with_its_transform(tmp_path: Path) -> None:
    workspace, output = Workspace(tmp_path), tally_output()
    source = output.plan.chunks[0].source
    transform = output.config.transform
    assert workspace.load_plan(source, transform.id) is None
    workspace.save_plan(transform, output.plan.chunks, output.plan.findings)
    stored = workspace.load_plan(source, transform.id)
    assert stored is not None
    assert stored.transform == transform
    assert stored.chunks == tuple(chunk.to_json() for chunk in output.plan.chunks)
    with pytest.raises(WorkspaceError):
        workspace.save_plan(transform, (), ())


def test_a_plan_is_kept_once_and_a_different_one_is_refused(tmp_path: Path) -> None:
    """Planning is deterministic: the same plan again is a no-op, another one an error."""
    workspace, output = Workspace(tmp_path), tally_output()
    transform, chunks = output.config.transform, output.plan.chunks
    workspace.save_plan(transform, chunks, output.plan.findings)
    (kept,) = (tmp_path / "plans").rglob("*.json")
    before = kept.read_bytes()
    workspace.save_plan(transform, chunks, output.plan.findings)
    with pytest.raises(WorkspaceError, match="different plan"):
        workspace.save_plan(transform, chunks[:1], output.plan.findings)
    assert kept.read_bytes() == before


def test_a_committed_chunk_reads_back_whole(tmp_path: Path) -> None:
    workspace, output = Workspace(tmp_path), tally_output()
    assert not any(workspace.committed(chunk.id) for chunk in output.plan.chunks)
    assert commit_all(workspace, output) == [True, True, True]
    header, rows = (
        workspace.load(output.plan.chunks[0].id),
        workspace.load(output.plan.chunks[1].id),
    )
    assert header.records == tuple(sorted(output.outputs[0].records, key=lambda r: r.id))
    assert header.chunk == output.plan.chunks[0].to_json()
    assert rows.findings == output.outputs[1].findings
    (stream,) = header.runs
    assert stream in rows.runs
    assert sorted(workspace.chunks()) == sorted(chunk.id for chunk in output.plan.chunks)


def test_committing_a_chunk_again_changes_nothing(tmp_path: Path) -> None:
    workspace, output = Workspace(tmp_path), tally_output()
    commit_all(workspace, output)
    before = {p: p.read_bytes() for p in (tmp_path / "chunks").rglob("*") if p.is_file()}
    assert commit_all(workspace, output) == [False, False, False]
    after = {p: p.read_bytes() for p in (tmp_path / "chunks").rglob("*") if p.is_file()}
    assert after == before
    assert not any((tmp_path / "staging").iterdir())


Flushed = list[tuple[int, frozenset[str]]]  # each fsync: the inode, and a directory's names


def record_flushes(monkeypatch: pytest.MonkeyPatch, renamed: list[int]) -> Flushed:
    """Record every fsync, and in ``renamed`` how many fsyncs came before each rename."""
    flushed: Flushed = []
    fsync, rename = os.fsync, os.rename

    def recording_fsync(descriptor: int) -> None:
        held = os.fstat(descriptor)
        fsync(descriptor)
        names = os.listdir(descriptor) if stat.S_ISDIR(held.st_mode) else []
        flushed.append((held.st_ino, frozenset(names)))

    def recording_rename(source: Any, target: Any) -> None:
        rename(source, target)
        renamed.append(len(flushed))

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "rename", recording_rename)
    return flushed


def flushed_holding(flushed: Flushed, directory: Path, name: str) -> bool:
    """Whether ``directory`` was fsynced while it held ``name``."""
    inode = directory.stat().st_ino
    return any(flushed_inode == inode and name in names for flushed_inode, names in flushed)


def test_a_commit_is_flushed_down_to_the_chunks_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chunk's tree before its rename; its name in the prefix and the new prefix's name in
    ``chunks/`` before the commit returns, so a returned commit survives a power loss."""
    workspace, output = Workspace(tmp_path), tally_output()
    chunk, out = output.plan.chunks[0], output.outputs[0]
    renamed: list[int] = []
    flushed = record_flushes(monkeypatch, renamed)
    assert workspace.commit(chunk, out.records, out.findings, out.series)
    final = workspace.chunk_path(chunk.id)
    (published,) = renamed
    before = {inode for inode, _ in flushed[:published]}
    for path in [final, *final.rglob("*")]:
        assert path.stat().st_ino in before, path
    assert flushed_holding(flushed, final.parent, final.name)
    assert flushed_holding(flushed, tmp_path / "chunks", final.parent.name)


def test_opening_a_workspace_flushes_its_home_and_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flushed = record_flushes(monkeypatch, [])
    Workspace(tmp_path / "home")
    assert flushed_holding(flushed, tmp_path, "home")
    (tmp_path / "home" / "plans").rmdir()
    flushed.clear()
    Workspace(tmp_path / "home")  # workspace.json exists: the remade folder is flushed anyway
    assert flushed_holding(flushed, tmp_path / "home", "plans")


def test_a_new_plan_or_ledger_directory_is_flushed_into_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, output = Workspace(tmp_path / "home"), tally_output()
    flushed = record_flushes(monkeypatch, [])
    workspace.save_plan(output.config.transform, output.plan.chunks, output.plan.findings)
    workspace.save_ledger(tmp_path, SourceLedger())
    for kept in ("plans", "ledgers"):
        (directory,) = (tmp_path / "home" / kept).iterdir()
        assert flushed_holding(flushed, directory.parent, directory.name), kept


def test_a_failed_commit_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, output = Workspace(tmp_path), tally_output()
    chunk, out = output.plan.chunks[1], output.outputs[1]

    def fail(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("neptune.store.workspace.write_run", fail)
    with pytest.raises(OSError, match="disk full"):
        workspace.commit(chunk, out.records, out.findings, out.series)
    assert not workspace.committed(chunk.id)
    assert not any((tmp_path / "staging").iterdir())
    monkeypatch.undo()
    assert workspace.commit(chunk, out.records, out.findings, out.series)


def test_a_commit_killed_midway_leaves_no_partial_chunk(tmp_path: Path) -> None:
    """A real SIGKILL just before the atomic rename: staging debris, and no committed chunk."""
    if os.name != "posix":  # pragma: no cover
        pytest.skip("SIGKILL is POSIX")
    script = textwrap.dedent(
        f"""
        import importlib.util, os, signal, sys
        from pathlib import Path
        sys.path.insert(0, {str(FIXTURES)!r})
        path = {str(FIXTURES / "tally_adapter.py")!r}
        spec = importlib.util.spec_from_file_location("tally_adapter", path)
        tally = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tally)
        from neptune.adapters.harness import ingest_source
        from neptune.discovery.reader import BytesReader
        from neptune.store.workspace import Workspace
        output = ingest_source(tally.TallyAdapter(), BytesReader({DATA!r}))
        workspace = Workspace(Path({str(tmp_path)!r}))
        Path.rename = lambda self, target: os.kill(os.getpid(), signal.SIGKILL)
        chunk, out = output.plan.chunks[1], output.outputs[1]
        workspace.commit(chunk, out.records, out.findings, out.series)
        """
    )
    killed = subprocess.run([sys.executable, "-c", script], check=False, capture_output=True)
    assert killed.returncode == -9, killed.stderr.decode()
    workspace, output = Workspace(tmp_path), tally_output()
    chunk, out = output.plan.chunks[1], output.outputs[1]
    assert not workspace.committed(chunk.id)
    assert workspace.clear_staging() == 1  # the dead process's staging directory
    assert workspace.commit(chunk, out.records, out.findings, out.series)
    assert workspace.load(chunk.id).findings == out.findings


def test_clearing_staging_leaves_a_commit_in_flight_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another process clears staging mid-commit: dead debris goes, the live one stays."""
    workspace, output = Workspace(tmp_path), tally_output()
    chunk, out = output.plan.chunks[1], output.outputs[1]
    abandoned = tmp_path / "staging" / "tmp-dead-writer"
    abandoned.mkdir()
    (abandoned / "chunk.json").write_bytes(b"{}")
    cleared: list[int] = []

    def clear_then_write(*args: object) -> None:
        cleared.append(Workspace(tmp_path).clear_staging())  # its own open file: its own lock
        write_run(*args)  # type: ignore[arg-type]

    monkeypatch.setattr("neptune.store.workspace.write_run", clear_then_write)
    assert workspace.commit(chunk, out.records, out.findings, out.series)
    assert cleared and sum(cleared) == 1  # the abandoned directory, never the commit in flight
    assert not abandoned.exists()
    assert workspace.load(chunk.id).findings == out.findings
    assert not any((tmp_path / "staging").iterdir())


def test_loading_an_uncommitted_chunk_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="not committed"):
        Workspace(tmp_path).load("chunk:sha256:" + "0" * 64)
    with pytest.raises(WorkspaceError, match="not a chunk:sha256 id"):
        Workspace(tmp_path).committed("chunk:nope")


def test_only_a_chunk_id_names_a_chunk(tmp_path: Path) -> None:
    """A stream's id has the same digits' shape; it must not answer for a chunk."""
    workspace, output = Workspace(tmp_path), tally_output()
    commit_all(workspace, output)
    digest = output.plan.chunks[0].id.removeprefix("chunk:sha256:")
    for impostor in (f"rec:sha256:{digest}", f"sha256:{digest}", f"x:chunk:sha256:{digest}"):
        with pytest.raises(WorkspaceError, match="not a chunk:sha256 id"):
            workspace.committed(impostor)
    assert workspace.committed(output.plan.chunks[0].id)


def test_a_stray_file_among_chunks_is_named_not_crashed_on(tmp_path: Path) -> None:
    workspace, output = Workspace(tmp_path), tally_output()
    commit_all(workspace, output)
    (tmp_path / "chunks" / ".DS_Store").write_bytes(b"")
    with pytest.raises(WorkspaceError, match=r"\.DS_Store"):
        list(workspace.chunks())
    (tmp_path / "chunks" / ".DS_Store").unlink()
    prefix = workspace.chunk_path(output.plan.chunks[0].id).parent
    (prefix / "notes.txt").write_bytes(b"")
    with pytest.raises(WorkspaceError, match=r"notes\.txt is not a committed chunk"):
        list(workspace.chunks())


def test_a_stray_file_among_runs_is_not_read_as_a_stream(tmp_path: Path) -> None:
    workspace, output = Workspace(tmp_path), tally_output()
    commit_all(workspace, output)
    chunk = output.plan.chunks[1].id
    (workspace.chunk_path(chunk) / "runs" / "notes.parquet").write_bytes(b"")
    with pytest.raises(WorkspaceError, match=r"notes\.parquet is not a stream's run"):
        workspace.load(chunk)
