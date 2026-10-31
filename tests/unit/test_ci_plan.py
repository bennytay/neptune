"""The CI plan (``.github/scripts/ci_plan.py``)."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).parents[2] / ".github" / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


ci_plan = _load("ci_plan")

MEMBERS = {
    "neptune-context": frozenset({"mcp", "neptune-memory"}),
    "neptune-memory": frozenset({"neptune"}),
    "neptune-other": frozenset(),
}


def _plan(*changed: str) -> tuple[bool, tuple[str, ...]]:
    result = ci_plan.plan(list(changed), MEMBERS)
    return result.compiler, result.packages


def test_a_member_change_runs_it_and_its_dependents() -> None:
    assert _plan("packages/neptune-memory/src/neptune_memory/site/query.py") == (
        False,
        ("neptune-context", "neptune-memory"),
    )
    assert _plan("packages/neptune-context/tests/x.py") == (False, ("neptune-context",))


def test_a_compiler_change_runs_the_compiler_and_every_member_that_depends_on_it() -> None:
    """The site model ingests through the compiler's adapters, so an adapter change reaches it."""
    every = (True, ("neptune-context", "neptune-memory"))
    assert _plan("src/neptune/model/x.py") == every
    assert _plan("src/neptune/adapters/tabular/adapter.py") == every
    assert _plan("scripts/factory-merge.sh") == every


@pytest.mark.parametrize(
    "path", ["pyproject.toml", "uv.lock", "Makefile", ".python-version", ".github/workflows/ci.yml"]
)
def test_plumbing_runs_everything(path: str) -> None:
    assert _plan(path) == (True, tuple(sorted(MEMBERS)))


def test_markdown_outside_the_schema_runs_no_compiler_job() -> None:
    assert _plan("docs/site-model/spec.md", "AGENTS.md") == (False, ())
    assert _plan("README.md")[0] is True
    assert _plan("docs/schema/x.md")[0] is True


def test_unfiltered_events_run_everything() -> None:
    result = ci_plan.plan(None, MEMBERS)
    assert (result.compiler, result.packages) == (True, tuple(sorted(MEMBERS)))


def test_empty_change_runs_nothing() -> None:
    assert _plan() == (False, ())


def test_outputs_are_github_output_lines() -> None:
    result = ci_plan.plan(["packages/neptune-other/x"], MEMBERS)
    assert result.outputs() == 'compiler=false\npackages=["neptune-other"]\n'


def test_workspace_members_reads_pyprojects(tmp_path: Path) -> None:
    (tmp_path / "packages" / "a-b").mkdir(parents=True)
    (tmp_path / "packages" / "a-b" / "pyproject.toml").write_text(
        '[project]\nname = "a-b"\ndependencies = ["Neptune_Memory>=1", "neptune"]\n'
    )
    (tmp_path / "packages" / "stray").mkdir()
    assert ci_plan.workspace_members(tmp_path) == {"a-b": frozenset({"neptune-memory", "neptune"})}


def test_main_diffs_base_against_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    (tmp_path / "packages" / "p").mkdir(parents=True)
    (tmp_path / "packages" / "p" / "pyproject.toml").write_text('[project]\nname = "p"\n')
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "packages" / "p" / "x.py").write_text("")
    git("add", ".")
    git("commit", "-qm", "head")
    output = tmp_path / "out"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_plan.main(["pull_request", base, "HEAD"]) == 0
    assert output.read_text() == 'compiler=false\npackages=["p"]\n'
    assert ci_plan.main(["merge_group"]) == 0
    assert ci_plan.main([]) == 2
