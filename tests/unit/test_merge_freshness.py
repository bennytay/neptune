"""scripts/merge_freshness.py: when a PR behind main may merge without a refresh."""

import importlib.util
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
    "neptune-memory": frozenset({"neptune"}),
    "neptune-context": frozenset({"mcp", "neptune-memory"}),
    "neptune-other": frozenset(),
}
MCAP = "src/neptune/adapters/mcap/adapter.py"
CORE = "src/neptune/model/time.py"
MEMORY = "packages/neptune-memory/src/neptune_memory/site/query.py"
CONTEXT = "packages/neptune-context/src/neptune_context/mcp/site.py"
OTHER = "packages/neptune-other/src/neptune_other/x.py"


@pytest.mark.parametrize(
    ("pr", "main"),
    [
        ([CONTEXT], [OTHER]),  # unrelated members
        ([MEMORY], ["docs/site-model/spec.md"]),  # docs only
        ([MEMORY], []),  # not behind
    ],
)
def test_fresh(pr: list[str], main: list[str]) -> None:
    assert mf.decide(pr, main, MEMBERS) is None


@pytest.mark.parametrize(
    ("pr", "main", "shared"),
    [
        ([MCAP], [CORE], "neptune, neptune-context, neptune-memory"),
        ([CONTEXT], [MEMORY], "neptune-context"),
        ([CONTEXT], [CORE], "neptune-context"),  # the site model ingests through the compiler
        ([OTHER], ["uv.lock"], "neptune-other"),
        ([OTHER], [".github/workflows/ci.yml"], "neptune-other"),
    ],
)
def test_refresh(pr: list[str], main: list[str], shared: str) -> None:
    assert mf.decide(pr, main, MEMBERS) == f"main changed inputs to {shared}"


def test_cli_reports_and_exits(tmp_path: Path) -> None:
    pr, main = tmp_path / "pr", tmp_path / "main"
    pr.write_text(f"{CONTEXT}\n")
    main.write_text("docs/site-model/README.md\n")
    run = [sys.executable, str(SCRIPT), str(pr), str(main), "HEAD"]
    ok = subprocess.run(run, capture_output=True, text=True, check=False)
    assert (ok.returncode, ok.stdout) == (0, "fresh\n")
    main.write_text(f"{CORE}\n")
    stale = subprocess.run(run, capture_output=True, text=True, check=False)
    assert stale.returncode == 1
    assert stale.stdout.startswith("refresh: main changed inputs to neptune-context")


def test_members_are_read_at_a_git_ref() -> None:
    assert "neptune-memory" in mf.members_at("HEAD")
