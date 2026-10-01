"""The local workspace (ADR 0026): location, settings, ledgers, plans and atomic chunk commits."""

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.reader import BytesReader
from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource
from neptune.identity import canonical_json
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
    if os.name != "posix":  # pragma: no cover
        pytest.skip("SIGKILL is POSIX")


def test_loading_an_uncommitted_chunk_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="not committed"):
        Workspace(tmp_path).load("chunk:sha256:" + "0" * 64)
    with pytest.raises(WorkspaceError, match="not a sha256 id"):
        Workspace(tmp_path).committed("chunk:nope")
