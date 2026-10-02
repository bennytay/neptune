"""scripts/merge_freshness.py: when a PR behind main may merge without a refresh."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "merge_freshness.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("merge_freshness", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["merge_freshness"] = module
    spec.loader.exec_module(module)
    return module


mf = _load()

MEMBERS = {
    "neptune-ledger": frozenset({"neptune"}),
    "neptune-memory": frozenset({"neptune", "neptune-ledger"}),
    "neptune-platform": frozenset(),
}
MCAP = "src/neptune/adapters/mcap/adapter.py"
URDF = "src/neptune/adapters/urdf/adapter.py"
CORE = "src/neptune/model/time.py"
LEDGER = "packages/neptune-ledger/src/neptune_ledger/catalog.py"
MEMORY = "packages/neptune-memory/src/neptune_memory/claims.py"
PLATFORM = "packages/neptune-platform/src/neptune_platform/x.py"


@pytest.mark.parametrize(
    ("pr", "main"),
    [
        ([MCAP], [URDF]),  # two adapters never import each other
        ([MCAP], [LEDGER, MEMORY]),  # no member imports an adapter
        ([LEDGER], [PLATFORM]),  # unrelated members
        ([MEMORY], []),  # not behind
    ],
)
def test_fresh(pr: list[str], main: list[str]) -> None:
    assert mf.decide(pr, main, MEMBERS) is None


@pytest.mark.parametrize(
    ("pr", "main", "reason"),
    [
        ([MCAP], [CORE], "the compiler core changed under adapter:mcap"),
        ([CORE], [MCAP], "the compiler core changed under adapter:mcap"),
        ([MEMORY], [LEDGER], "both sides reach neptune-memory"),
        ([LEDGER], [CORE], "both sides reach neptune-ledger, neptune-memory"),
        ([MCAP], ["src/neptune/adapters/mcap/probe.py"], "both sides reach adapter:mcap"),
        ([PLATFORM], ["harness/run.py"], "both sides reach neptune-platform"),
        ([MCAP], ["uv.lock"], "root plumbing or contracts/ changed"),
        (
            ["contracts/catalog-api/1.2.0/schema.json"],
            [URDF],
            "root plumbing or contracts/ changed",
        ),
        (
            [MCAP],
            ["src/neptune/adapters/contract.py"],
            "the compiler core changed under adapter:mcap",
        ),
        (["packages/_template/AGENTS.md"], ["scripts/new-package.sh"], "both sides reach template"),
    ],
)
def test_refresh(pr: list[str], main: list[str], reason: str) -> None:
    assert mf.decide(pr, main, MEMBERS) == reason


def test_cli_reports_and_exits(tmp_path: Path) -> None:
    pr, main = tmp_path / "pr", tmp_path / "main"
    pr.write_text(f"{MCAP}\n")
    main.write_text(f"{URDF}\n")
    ok = subprocess.run(
        [sys.executable, str(SCRIPT), str(pr), str(main)], capture_output=True, text=True
    )
    assert (ok.returncode, ok.stdout) == (0, "fresh\n")
    main.write_text(f"{CORE}\n")
    stale = subprocess.run(
        [sys.executable, str(SCRIPT), str(pr), str(main)], capture_output=True, text=True
    )
    assert stale.returncode == 1
    assert stale.stdout.startswith("refresh: ")
