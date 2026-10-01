"""Series files as workspace derivatives: merged once, copied after, rebuilt if damaged."""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.text import TextAdapter
from neptune.discovery.reader import BytesReader
from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ContentId, RecordId
from neptune.store.assemble import SERIES_FILE, SERIES_RECIPE, series_key, stage
from neptune.store.package import read_package
from neptune.store.series import SERIES_SETTINGS
from neptune.store.workspace import Held, Workspace

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "adapters"
DATA: Final = b"TALLY1\n10 1\n20 2\n30 3\n40 4\n50 5\n"


def _tally() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tally_adapter", FIXTURES / "tally_adapter.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TALLY: Final = _tally()


@pytest.fixture
def kept(tmp_path: Path) -> tuple[Workspace, SourceLedger, SourceOutput]:
    """A workspace holding the tally source's plan and committed chunks, and its root's ledger."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "lift.tally").write_bytes(DATA)
    workspace = Workspace(tmp_path / "home")
    ledger = workspace.load_ledger(root)
    scan(LocalSource(root), ledger)
    output = ingest_source(TALLY.TallyAdapter(), BytesReader(DATA))
    workspace.save_plan(output.config.transform, output.plan.chunks, output.plan.findings)
    for chunk, out in zip(output.plan.chunks, output.outputs, strict=True):
        workspace.commit(chunk, out.records, out.findings, out.series)
    return workspace, ledger, output


def pair(output: SourceOutput) -> list[tuple[ContentId, RecordId]]:
    return [(output.plan.chunks[0].source, output.config.transform.id)]


def test_a_series_file_is_merged_once_and_copied_into_every_later_package(
    kept: tuple[Workspace, SourceLedger, SourceOutput], tmp_path: Path
) -> None:
    workspace, ledger, output = kept
    first = stage(tmp_path / "a", workspace, ledger, pair(output))
    second = stage(tmp_path / "b", workspace, ledger, pair(output))
    use_a, use_b = (*first.derivatives, *second.derivatives)
    assert (use_a.held, use_b.held) == (Held.BUILT, Held.HELD)
    assert use_a.key == use_b.key and use_a.key.recipe == SERIES_RECIPE
    assert use_a.key.owners == tuple(pair(output))
    assert first.id == second.id
    held = workspace.derivative(use_a.key)
    assert held is not None
    ((stream, series),) = read_package(first.path).series.items()
    assert isinstance(series, Path)
    assert series.read_bytes() == held.file(SERIES_FILE).read_bytes()
    assert stream in read_package(second.path).series


def test_a_damaged_series_file_is_merged_again_and_the_package_is_the_same(
    kept: tuple[Workspace, SourceLedger, SourceOutput], tmp_path: Path
) -> None:
    workspace, ledger, output = kept
    first = stage(tmp_path / "a", workspace, ledger, pair(output))
    (use,) = first.derivatives
    held = workspace.derivative(use.key)
    assert held is not None
    data = bytearray(held.file(SERIES_FILE).read_bytes())
    data[len(data) // 2] ^= 0xFF  # same size: only the hash can tell
    held.file(SERIES_FILE).write_bytes(bytes(data))
    second = stage(tmp_path / "b", workspace, ledger, pair(output))
    assert [u.held for u in second.derivatives] == [Held.REBUILT]
    assert second.id == first.id
    read_package(second.path)
    third = stage(tmp_path / "c", workspace, ledger, pair(output))
    assert [u.held for u in third.derivatives] == [Held.HELD]


def test_a_series_key_covers_its_chunks_and_the_writers_settings() -> None:
    stream = RecordId("rec:sha256:" + "1" * 64)
    owners = [(ContentId("sha256:" + "2" * 64), RecordId("rec:sha256:" + "3" * 64))]
    chunks = ["chunk:sha256:" + "4" * 64, "chunk:sha256:" + "5" * 64]
    key = series_key(stream, chunks, owners)
    assert key == series_key(stream, chunks[::-1] + chunks, owners * 2)
    assert key.inputs["settings"] == SERIES_SETTINGS and key.inputs["chunks"] == chunks
    assert key.id != series_key(stream, chunks[:1], owners).id
    other = RecordId("rec:sha256:" + "6" * 64)
    assert key.id != series_key(other, chunks, owners).id


def test_a_records_only_package_needs_no_series_derivative(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    text = b"just text\n"
    (root / "notes.txt").write_bytes(text)
    workspace = Workspace(tmp_path / "home")
    ledger = workspace.load_ledger(root)
    scan(LocalSource(root), ledger)
    output = ingest_source(TextAdapter(), BytesReader(text))
    workspace.save_plan(output.config.transform, output.plan.chunks, output.plan.findings)
    for chunk, out in zip(output.plan.chunks, output.outputs, strict=True):
        workspace.commit(chunk, out.records, out.findings, out.series)
    staged = stage(tmp_path / "p", workspace, ledger, pair(output))
    assert staged.derivatives == ()
    assert list(workspace.derivatives()) == []
