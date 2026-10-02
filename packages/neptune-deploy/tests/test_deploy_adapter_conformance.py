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
# Adapters never import each other: a module may import its own subpackage of
# ``neptune_deploy.adapters`` (and the bare package), never another one. Relative imports are
# resolved against the module's package and checked like absolute ones.
ADAPTERS_PACKAGE = "neptune_deploy.adapters"
ALLOWED = ("neptune.model", "neptune.identity", "neptune.adapters.contract")
STDLIB_ALLOWED = frozenset({"collections", "dataclasses", "enum", "re", "typing"})


def _within(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(f"{prefix}.")


def _module_of(path: Path) -> tuple[str, bool]:
    """The dotted module name of ``path`` under ``src/``, and whether it is a package."""
    parts = path.relative_to(ADAPTERS.parents[1]).with_suffix("").parts
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]), True
    return ".".join(parts), False


def _imports(module: str, is_package: bool, text: str) -> list[str]:
    """Every name ``text`` imports, fully qualified; ``from m import x`` gives ``m.x``."""
    package = module if is_package else module.rpartition(".")[0]
    names: list[str] = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                keep = len(parts) - (node.level - 1)
                if keep < 1:  # beyond the top-level package: never allowed
                    names.append("." * node.level + (node.module or ""))
                    continue
                base = ".".join(parts[:keep])
                origin = f"{base}.{node.module}" if node.module else base
            else:
                origin = node.module or ""
            names.extend(origin if a.name == "*" else f"{origin}.{a.name}" for a in node.names)
    return names


def _violations(module: str, is_package: bool, text: str) -> list[str]:
    """The imports of ``module`` that break the leaf rule."""
    parts = module.split(".")
    own = ".".join(parts[:3]) if _within(module, ADAPTERS_PACKAGE) and len(parts) > 2 else None
    bad = []
    for name in _imports(module, is_package, text):
        if _within(name, ADAPTERS_PACKAGE):
            ok = name == ADAPTERS_PACKAGE or (own is not None and _within(name, own))
        else:
            ok = any(_within(name, prefix) for prefix in ALLOWED)
            ok = ok or name.split(".")[0] in STDLIB_ALLOWED
        if not ok:
            bad.append(name)
    return bad


@pytest.mark.parametrize("path", sorted(ADAPTERS.rglob("*.py")), ids=lambda p: p.stem)
def test_adapters_are_leaves_with_no_network(path: Path) -> None:
    module, is_package = _module_of(path)
    assert _violations(module, is_package, path.read_text(encoding="utf-8")) == []


LIFECYCLE_MODULE = f"{ADAPTERS_PACKAGE}.lifecycle"


@pytest.mark.parametrize(
    ("text", "is_package", "imported"),
    [
        ("from ...sources import connector\n", True, "neptune_deploy.sources.connector"),
        ("from .. import cmms\n", True, f"{ADAPTERS_PACKAGE}.cmms"),
        ("from ..cmms.forms import read\n", False, f"{ADAPTERS_PACKAGE}.cmms.forms.read"),
        ("from .... import x\n", True, "...."),  # beyond the top-level package
    ],
)
def test_a_relative_import_is_resolved_and_checked(
    text: str, is_package: bool, imported: str
) -> None:
    module = LIFECYCLE_MODULE if is_package else f"{LIFECYCLE_MODULE}.forms"
    assert _violations(module, is_package, text) == [imported]


@pytest.mark.parametrize(
    "text",
    [
        "import neptune_deploy.adapters.cmms\n",
        "from neptune_deploy.adapters.cmms import CmmsAdapter\n",
        "from neptune_deploy.adapters import cmms\n",
    ],
)
def test_an_adapter_never_imports_another_adapter(text: str) -> None:
    assert _violations(LIFECYCLE_MODULE, True, text) != []


def test_an_adapter_may_import_its_own_subpackage_and_the_allowed_modules() -> None:
    text = (
        "from . import _fields\n"
        "from neptune_deploy.adapters.lifecycle._rows import Row\n"
        "from neptune.model.lifecycle import LIFECYCLE_KINDS\n"
        "from typing import Final\n"
    )
    assert _violations(LIFECYCLE_MODULE, True, text) == []
    assert _violations(LIFECYCLE_MODULE, True, "import neptune.modelx\n") == ["neptune.modelx"]
