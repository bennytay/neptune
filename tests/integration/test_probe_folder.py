"""MVL-8 acceptance over a folder: scan, probe every source, ingest what is selected, package.

- Renamed and extensionless files are read by the adapter their bytes call for.
- A tie between adapters and a source nobody claims are findings in the package and the receipt,
  never a silent guess; the probe engine's transform shows who looked at an unread source.
"""

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.contract import (
    ABI_VERSION,
    SIGNATURE,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    FormatSpec,
    InspectResult,
    Magic,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
)
from neptune.adapters.harness import ingest_source
from neptune.adapters.registry import AdapterRegistry
from neptune.discovery.probe import PROBE_ID, ProbeEngine, SourceProbe, hint_name
from neptune.discovery.reader import BytesReader
from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource
from neptune.identity.revisions import SourceLedger
from neptune.model.source import LocalPath
from neptune.store.package import RECEIPT_TEXT, package_files, read_files

pytestmark = pytest.mark.integration

ROOT: Final = Path(__file__).parents[2]
FIXTURES: Final = ROOT / "tests" / "fixtures"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TALLY: Final = _load("tally_adapter", FIXTURES / "adapters" / "tally_adapter.py")
ULOG_HEAD: Final = b"ULog\x01\x12\x35\x01" + bytes(8)


class Rival:
    """A second adapter that claims the tally signature exactly as strongly: a real tie."""

    descriptor = AdapterDescriptor(
        id="rival",
        version="2.0.0",
        abi=ABI_VERSION,
        summary="Claims tally files too, for the ambiguity test.",
        formats=(FormatSpec("Tally", magic=(Magic(0, b"TALLY1\n"),)),),
        record_kinds=("document_record",),
        config=(),
        libraries=(),
        finding_codes=(),
        locator_steps=(),
        conventions=(),
        resources=Resources(0, True),
        security=(),
    )

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if head.startswith(b"TALLY1\n"):
            return ProbeResult(SIGNATURE, (ProbeReason("rival.magic", "starts TALLY1"),))
        return ProbeResult(0.0, ())

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        raise NotImplementedError

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        raise NotImplementedError

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        raise NotImplementedError


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    for name in ("notes.txt", "operator_log"):
        shutil.copy(FIXTURES / "text" / name, tmp_path / name)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "renamed").write_bytes(b"TALLY1\n7 70\n")
    (tmp_path / "logs" / "flight_log").write_bytes(ULOG_HEAD)
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02 binary payload \x00")
    shutil.copy(FIXTURES / "probe" / "containers" / "members.zip", tmp_path / "bundle")
    return tmp_path


def ingest_folder(root: Path, registry: AdapterRegistry) -> tuple[dict[str, SourceProbe], Any]:
    """Scan, probe, ingest and package: what the runtime (MVL-6) will do, minus persistence."""
    source, ledger, engine = LocalSource(root), SourceLedger(), ProbeEngine(registry)
    probes: dict[str, SourceProbe] = {}
    records: dict[str, Any] = {engine.transform.id: engine.transform}
    for observation in scan(source, ledger).observations:
        location = observation.revision.location
        assert isinstance(location, LocalPath)
        with source.open(location) as stream:
            data = stream.read()
        reader = BytesReader(data, observation.revision.content_id)
        probed = engine.probe(reader, hint_name(location))
        probes[location.path] = probed
        records.update((f.id, f) for f in probed.findings)
        if probed.adapter is not None:
            output = ingest_source(registry.get(probed.adapter), reader)
            records.update((r.id, r) for r in output.package_records())
    ledger_records = (*ledger.artifacts(), *ledger.revisions(), *ledger.absences())
    files = package_files([*ledger_records, *records.values()])
    return probes, read_files(files)


def test_bytes_decide_what_reads_each_source_and_what_is_left_unread(corpus: Path) -> None:
    registry = AdapterRegistry([*builtin_adapters(), TALLY.TallyAdapter(), Rival()])
    probes, package = ingest_folder(corpus, registry)
    assert {path: probed.adapter for path, probed in probes.items()} == {
        "blob.bin": None,
        "bundle": None,
        "logs/flight_log": None,
        "logs/renamed": None,  # tally and rival tie: nobody is guessed
        "notes.txt": "text",
        "operator_log": "text",
    }
    receipt = package.receipt
    transforms = {t.id: (t.adapter_id, t.adapter_version) for t in receipt.transforms}
    read_by = {
        source.location.path: sorted(transforms[t] for t in source.read_by)
        for source in receipt.sources
    }
    assert read_by == {
        "blob.bin": [(PROBE_ID, "0.1.0")],
        "bundle": [(PROBE_ID, "0.1.0")],
        "logs/flight_log": [(PROBE_ID, "0.1.0")],
        "logs/renamed": [(PROBE_ID, "0.1.0")],
        "notes.txt": [("text", "0.1.0")],
        "operator_log": [("text", "0.1.0")],
    }
    findings = {(f.code, f.severity) for f in receipt.findings}
    assert findings == {
        (f"{PROBE_ID}.ambiguous", "error"),
        (f"{PROBE_ID}.unsupported", "error"),
    }
    messages = sorted(f.message for f in receipt.findings)
    assert any("rival, tally" in m for m in messages)
    assert any("ULog signature" in m for m in messages)
    assert any("zip container holding 6 members" in m for m in messages)
    rendering = package.files()[RECEIPT_TEXT].decode()
    assert f"{PROBE_ID} 0.1.0" in rendering
    assert f"{PROBE_ID}.unsupported" in rendering


def test_without_the_rival_the_renamed_tally_is_read_and_the_zip_members_are_reported(
    corpus: Path,
) -> None:
    registry = AdapterRegistry([*builtin_adapters(), TALLY.TallyAdapter()])
    probes, package = ingest_folder(corpus, registry)
    assert probes["logs/renamed"].adapter == "tally"
    assert probes["logs/renamed"].findings == ()
    transforms = {t.id: t.adapter_id for t in package.receipt.transforms}
    renamed = next(s for s in package.receipt.sources if s.location.path == "logs/renamed")
    assert [transforms[t] for t in renamed.read_by] == ["tally"]
    bundle = probes["bundle"]
    assert bundle.container is not None
    inside = {m.name: m.probe.selection.adapter for m in bundle.container.members if m.probe}
    assert inside == {
        b"../escape.txt": "text",
        b"logs/lift.tally": "tally",
        b"logs/renamed": "tally",
        b"notes.txt": "text",
        b"recording.mcap": None,
    }
