"""scripts/merge_freshness.py: when a PR behind main may merge without a refresh."""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "merge_freshness.py"


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
    "neptune-other": frozenset(),
}
MCAP = "src/neptune/adapters/mcap/adapter.py"
URDF = "src/neptune/adapters/urdf/adapter.py"
CORE = "src/neptune/model/time.py"
LEDGER = "packages/neptune-ledger/src/neptune_ledger/catalog.py"
MEMORY = "packages/neptune-memory/src/neptune_memory/claims.py"
PLATFORM = "packages/neptune-platform/src/neptune_platform/x.py"
OTHER = "packages/neptune-other/src/neptune_other/x.py"


@pytest.mark.parametrize(
    ("pr", "main"),
    [
        ([MCAP], [LEDGER, MEMORY]),  # no member imports an adapter
        ([LEDGER], [OTHER]),  # unrelated members
        ([LEDGER], [PLATFORM]),  # the harness overlap is left to main's push run
        (["harness/stages.py"], [CORE]),  # likewise
        ([MEMORY], []),  # not behind
    ],
)
def test_fresh(pr: list[str], main: list[str]) -> None:
    assert mf.decide(pr, main, MEMBERS) is None


@pytest.mark.parametrize(
    ("pr", "main", "shared"),
    [
        ([MCAP], [CORE], "neptune"),
        ([MCAP], [URDF], "neptune"),  # discovery probes every adapter against every file
        ([MEMORY], [LEDGER], "neptune-memory"),
        ([LEDGER], [CORE], "neptune-ledger, neptune-memory"),
        ([MCAP], ["src/neptune/adapters/contract.py"], "neptune"),
        ([OTHER], ["uv.lock"], "neptune-other"),
        ([OTHER], ["scripts/contracts.py"], "neptune-other"),  # every job runs contracts.py
        ([OTHER], [".github/workflows/ci.yml"], "neptune-other"),
        (["packages/_template/AGENTS.md"], ["scripts/new-package.sh"], "template"),
        (["harness/stages.py"], ["harness/run.py"], "neptune-platform"),  # both edit the platform
        ([PLATFORM], ["harness/run.py"], "neptune-platform"),
    ],
)
def test_refresh(pr: list[str], main: list[str], shared: str) -> None:
    assert mf.decide(pr, main, MEMBERS) == f"main changed inputs to {shared}"


def test_cli_reports_and_exits(tmp_path: Path) -> None:
    pr, main = tmp_path / "pr", tmp_path / "main"
    pr.write_text(f"{MCAP}\n")
    main.write_text("packages/neptune-ledger/README.md\n")
    run = [sys.executable, str(SCRIPT), str(pr), str(main), "HEAD"]
    ok = subprocess.run(run, capture_output=True, text=True, check=False)
    assert (ok.returncode, ok.stdout) == (0, "fresh\n")
    main.write_text(f"{CORE}\n")
    stale = subprocess.run(run, capture_output=True, text=True, check=False)
    assert stale.returncode == 1
    assert stale.stdout.startswith("refresh: main changed inputs to neptune")


def test_members_are_read_at_a_git_ref() -> None:
    members = mf.members_at("HEAD")
    assert "neptune-platform" in members
    assert "_template" not in members


ADAPTER_IMPORT = re.compile(
    r"neptune\.adapters\.(?!contract\b|registry\b)\w+"
    r"|from\s+neptune\.adapters\s+import\s+(?!contract\b|registry\b)"
    r"|neptune\.(sdk|runtime|discovery)\b"
)


def test_no_member_imports_a_format_adapter_or_runs_ingestion() -> None:
    """ci_plan and merge_freshness rely on it: adapter changes do not reach members' jobs.

    The platform is exempt: its harness ingests, and adapter changes select its job.
    """
    offenders = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "packages").glob("*/*/**/*.py"))
        if path.parts[len(ROOT.parts) + 2] in ("src", "tests")
        and path.parts[len(ROOT.parts) + 1] not in ("neptune-platform", "_template")
        and ADAPTER_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
