"""MVL-7 acceptance, end to end: scan a folder, pick an adapter per source, ingest, package.

- A new format plugs in without changing ingestion-core code: the ``tally`` adapter lives under
  ``tests/fixtures/adapters/``, and registering it is the only step.
- Adapter selection and parser version appear in the receipt: each source lists the transform
  (adapter id and version) that read it, and the receipt's adapter table names every version.
"""

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.contract import PROBE_HEAD_SIZE, ProbeHints
from neptune.adapters.harness import ingest_source
from neptune.adapters.registry import AdapterRegistry, Selection, SelectionStatus
from neptune.discovery.reader import BytesReader
from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource
from neptune.identity.revisions import SourceLedger
from neptune.model.source import LocalPath
from neptune.store.package import RECEIPT_TEXT, package_files, read_files

pytestmark = pytest.mark.integration

ROOT: Final = Path(__file__).parents[2]
FIXTURES: Final = ROOT / "tests" / "fixtures"


def _tally() -> ModuleType:
    path = FIXTURES / "adapters" / "tally_adapter.py"
    spec = importlib.util.spec_from_file_location("tally_adapter", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TALLY: Final = _tally()


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    for name in ("notes.txt", "operator_log", "corrupted.txt"):
        shutil.copy(FIXTURES / "text" / name, tmp_path / name)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "lift.tally").write_bytes(b"TALLY1\n10 1\n20 2\noops\n40 4\n")
    (tmp_path / "logs" / "renamed").write_bytes(b"TALLY1\n7 70\n")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02 binary payload \x00")
    return tmp_path


def ingest_folder(root: Path, registry: AdapterRegistry) -> tuple[dict[str, Selection], Any]:
    """Scan, select, ingest and package: what the runtime (MVL-6) will do, minus persistence."""
    source, ledger = LocalSource(root), SourceLedger()
    observations = scan(source, ledger).observations
    selections: dict[str, Selection] = {}
    records: dict[str, Any] = {}
    for observation in observations:
        location = observation.revision.location
        assert isinstance(location, LocalPath)
        with source.open(location) as stream:
            data = stream.read()
        hints = ProbeHints(location.parts[-1], len(data))
        selection = registry.select(data[:PROBE_HEAD_SIZE], hints)
        selections[location.path] = selection
        if selection.adapter is None:
            continue
        reader = BytesReader(data, observation.revision.content_id)
        output = ingest_source(registry.get(selection.adapter), reader)
        records.update((record_key(r), r) for r in output.package_records())
    ledger_records = (*ledger.artifacts(), *ledger.revisions(), *ledger.absences())
    files = package_files([*ledger_records, *records.values()])
    return selections, read_files(files)


def record_key(record: Any) -> str:
    return f"{record.kind}:{record.id}"


def registry() -> AdapterRegistry:
    return AdapterRegistry([*builtin_adapters(), TALLY.TallyAdapter()])


def test_each_source_gets_the_adapter_its_bytes_call_for(corpus: Path) -> None:
    selections, _ = ingest_folder(corpus, registry())
    assert {path: selection.adapter for path, selection in selections.items()} == {
        "blob.bin": None,
        "corrupted.txt": "text",
        "logs/lift.tally": "tally",
        "logs/renamed": "tally",
        "notes.txt": "text",
        "operator_log": "text",
    }
    assert selections["blob.bin"].status is SelectionStatus.UNSUPPORTED


def test_the_receipt_names_the_adapter_and_version_that_read_each_source(corpus: Path) -> None:
    _, package = ingest_folder(corpus, registry())
    receipt = package.receipt
    adapters = {t.id: (t.adapter_id, t.adapter_version) for t in receipt.transforms}
    assert sorted(adapters.values()) == [("tally", "1.0.0"), ("text", "0.1.0")]
    read_by = {
        source.location.path: [adapters[t] for t in source.read_by] for source in receipt.sources
    }
    assert read_by == {
        "blob.bin": [],
        "corrupted.txt": [("text", "0.1.0")],
        "logs/lift.tally": [("tally", "1.0.0")],
        "logs/renamed": [("tally", "1.0.0")],
        "notes.txt": [("text", "0.1.0")],
        "operator_log": [("text", "0.1.0")],
    }
    rendering = package.files()[RECEIPT_TEXT].decode()
    assert "| `notes.txt` |" in rendering and "| text 0.1.0 |" in rendering
    assert "| `logs/lift.tally` |" in rendering and "| tally 1.0.0 |" in rendering
    assert "| `blob.bin` |" in rendering and "| not read |" in rendering
    assert "| `text` | `0.1.0` |" in rendering and "| `tally` | `1.0.0` |" in rendering


def test_findings_and_streams_from_both_adapters_reach_the_receipt(corpus: Path) -> None:
    _, package = ingest_folder(corpus, registry())
    codes = sorted(finding.code for finding in package.receipt.findings)
    assert codes == ["tally.bad_row", "text.invalid_utf8"]
    assert len(package.receipt.streams) == 2
    counts = dict(package.receipt.records)
    assert counts["document_record"] == 3
    assert counts["run"] == 2


def test_packaging_the_same_folder_twice_gives_the_same_package(corpus: Path) -> None:
    _, first = ingest_folder(corpus, registry())
    _, second = ingest_folder(corpus, registry())
    assert first.id == second.id
    assert first.files() == second.files()


def test_equal_confidence_is_never_settled_silently(corpus: Path) -> None:
    class Clone(TALLY.TallyAdapter):  # type: ignore[misc, name-defined]
        descriptor = TALLY.DESCRIPTOR.__class__(
            **{**TALLY.DESCRIPTOR.__dict__, "id": "tally-clone", "finding_codes": ()}
        )

    tied = AdapterRegistry([TALLY.TallyAdapter(), Clone()])
    selections, package = ingest_folder(corpus, tied)
    selection = selections["logs/lift.tally"]
    assert selection.status is SelectionStatus.AMBIGUOUS
    assert [c.adapter for c in selection.tied] == ["tally", "tally-clone"]
    assert package.receipt.transforms == ()


def test_adding_a_format_touched_no_core_code() -> None:
    """The tally adapter lives in tests; nothing under src/neptune knows it exists."""
    assert not str(TALLY.__file__).startswith(str(ROOT / "src"))
    for path in (ROOT / "src" / "neptune").rglob("*.py"):
        assert "tally" not in path.read_text().lower(), path
