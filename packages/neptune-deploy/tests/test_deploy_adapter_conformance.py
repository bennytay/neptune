"""The compiler's adapter conformance check, run against every adapter Deploy registers.

Samples are small lifecycle exports from different embodiments; the conformance check adds an
empty source and each sample cut in half.
"""

import ast
from pathlib import Path

import pytest

from neptune.adapters.conformance import check_conformance
from neptune.adapters.contract import Adapter, ChunkOutput, ProbeHints, configure
from neptune.identity.hashing import content_id
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import ContentId
from neptune_deploy.adapters.lifecycle import NOT_READ, LifecycleAdapter
from test_deploy_plugin_registration import registered_adapters

ADAPTERS = Path(__file__).resolve().parents[1] / "src" / "neptune_deploy" / "adapters"
SAMPLES = (
    # An arm cell's commissioning sign-off, as a CSV export.
    b"cell,test,result,signed_by\nCELL-3,e-stop latency,pass,J. Ortiz\nCELL-3,fence interlock,pass,"
    b"J. Ortiz\n",
    # An AMR fleet's incident ticket, as JSON.
    b'{"incident": "INC-0007", "site": "S-007", "asset": "AMR-07", "severity": "S2",'
    b' "zone": "DOCK-1", "speed": {"value": 1.5, "unit": "m/s"}}\n',
    # Bytes that are no format at all.
    bytes(range(256)),
)


class _Bytes:
    """One source's bytes in memory: the ``SourceReader`` the runtime would hand an adapter."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    @property
    def content_id(self) -> ContentId:
        return content_id(self._data)

    @property
    def size(self) -> int:
        return len(self._data)

    def read(self, offset: int, length: int) -> bytes:
        return self._data[offset : offset + length]


def _run(data: bytes) -> list[ChunkOutput]:
    adapter, source = LifecycleAdapter(), _Bytes(data)
    config = configure(adapter.descriptor, None)
    return [adapter.ingest(source, chunk, config) for chunk in adapter.plan(source, config).chunks]


@pytest.mark.parametrize("adapter", registered_adapters(), ids=lambda a: a.descriptor.id)
def test_every_registered_adapter_conforms(adapter: Adapter) -> None:
    check_conformance(adapter, SAMPLES)


@pytest.mark.parametrize("sample", SAMPLES)
def test_the_lifecycle_adapter_claims_no_source(sample: bytes) -> None:
    result = LifecycleAdapter().probe(sample, ProbeHints("export.json", len(sample)))
    assert result.confidence == 0.0


def test_the_lifecycle_adapter_reports_the_whole_source_unread() -> None:
    (output,) = _run(SAMPLES[1])
    assert (output.records, output.series) == ((), ())
    (finding,) = output.findings
    assert (finding.code, finding.category, finding.severity) == (
        NOT_READ,
        FindingCategory.UNSUPPORTED,
        Severity.ERROR,
    )
    assert finding.subject.to_json()["locator"] == [
        {"kind": "byte_range", "length": len(SAMPLES[1]), "offset": 0}
    ]


def test_the_lifecycle_adapter_output_is_deterministic() -> None:
    assert _run(SAMPLES[0]) == _run(SAMPLES[0])
    assert _run(SAMPLES[0]) != _run(SAMPLES[2])  # the finding cites its own source


# The leaf rule (root ADR 0008 §4) and the sandbox: an adapter imports the compiler's model,
# identity and contract only, and nothing that reaches the network, the filesystem or a process.
ALLOWED = (
    "neptune.model",
    "neptune.identity",
    "neptune.adapters.contract",
    "neptune_deploy.adapters",
)
STDLIB_ALLOWED = frozenset({"collections", "dataclasses", "enum", "re", "typing"})


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


@pytest.mark.parametrize("path", sorted(ADAPTERS.rglob("*.py")), ids=lambda p: p.stem)
def test_adapters_are_leaves_with_no_network(path: Path) -> None:
    for name in _imports(path):
        allowed = name.startswith(ALLOWED) or name.split(".")[0] in STDLIB_ALLOWED
        assert allowed, f"{path.relative_to(ADAPTERS)} imports {name}"
