"""The CI plan (``.github/scripts/ci_plan.py``) and the ADR index generator (``adr_index.py``)."""

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
adr_index = _load("adr_index")

MEMBERS = {
    "neptune-ledger": frozenset({"neptune"}),
    "neptune-platform": frozenset(),
    "neptune-recall": frozenset({"neptune-ledger", "pyarrow"}),
}


def _plan(*changed: str) -> tuple[bool, tuple[str, ...], bool]:
    result = ci_plan.plan(list(changed), MEMBERS)
    return result.compiler, result.packages, result.template


def test_member_only_change_skips_the_compiler() -> None:
    assert _plan("packages/neptune-platform/src/neptune_platform/x.py") == (
        False,
        ("neptune-platform",),
        False,
    )


def test_member_change_runs_members_that_depend_on_it() -> None:
    assert _plan("packages/neptune-ledger/README.md") == (
        False,
        ("neptune-ledger", "neptune-recall"),
        False,
    )


def test_compiler_change_runs_dependent_members_transitively() -> None:
    assert _plan("src/neptune/model/x.py") == (True, ("neptune-ledger", "neptune-recall"), False)


def test_root_docs_and_scripts_are_the_compilers() -> None:
    assert _plan("docs/adr/README.md", "scripts/factory-merge.sh")[:2] == (
        True,
        ("neptune-ledger", "neptune-recall"),
    )


def test_contracts_run_every_member_but_not_the_compiler() -> None:
    assert _plan("contracts/ledger.schema.json") == (False, tuple(sorted(MEMBERS)), False)


@pytest.mark.parametrize(
    "path", ["pyproject.toml", "uv.lock", "Makefile", ".python-version", ".github/workflows/ci.yml"]
)
def test_plumbing_runs_everything(path: str) -> None:
    assert _plan(path) == (True, tuple(sorted(MEMBERS)), True)


def test_template_change_runs_only_the_template_smoke() -> None:
    assert _plan("packages/_template/AGENTS.md") == (False, (), True)
    assert _plan("scripts/new-package.sh") == (True, ("neptune-ledger", "neptune-recall"), True)


def test_unfiltered_events_run_everything() -> None:
    result = ci_plan.plan(None, MEMBERS)
    assert (result.compiler, result.packages, result.template) == (True, tuple(MEMBERS), True)


def test_empty_change_runs_nothing() -> None:
    assert _plan() == (False, (), False)


def test_outputs_are_github_output_lines() -> None:
    result = ci_plan.plan(["packages/neptune-platform/x"], MEMBERS)
    assert result.outputs() == 'compiler=false\npackages=["neptune-platform"]\ntemplate=false\n'


def test_workspace_members_reads_pyprojects_and_skips_the_template(tmp_path: Path) -> None:
    for name, deps in [("_template", '["x"]'), ("a-b", '["Neptune_Ledger>=1", "neptune"]')]:
        (tmp_path / "packages" / name).mkdir(parents=True)
        (tmp_path / "packages" / name / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\ndependencies = {deps}\n'
        )
    (tmp_path / "packages" / "stray").mkdir()
    assert ci_plan.workspace_members(tmp_path) == {"a-b": frozenset({"neptune-ledger", "neptune"})}


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
    assert output.read_text() == 'compiler=false\npackages=["p"]\ntemplate=false\n'
    assert ci_plan.main(["merge_group"]) == 0
    assert ci_plan.main([]) == 2


def _adr(directory: Path, name: str, title: str, status: str) -> None:
    (directory / name).write_text(f"# {name[:4]} — {title}\n\n- Status: {status}\n- Date: x\n")


def test_adr_index_lists_decisions_in_number_order(tmp_path: Path) -> None:
    _adr(tmp_path, "0000-template.md", "Title", "Proposed | Accepted")
    _adr(tmp_path, "0002-b.md", "Second | piped", "Superseded by 0003")
    _adr(tmp_path, "0001-a.md", "First", "Accepted")
    (tmp_path / "notes.md").write_text("# not an ADR\n")
    text = adr_index.render(tmp_path)
    assert text.endswith(
        "| [0001](0001-a.md) | First | Accepted |\n"
        "| [0002](0002-b.md) | Second \\| piped | Superseded by 0003 |\n"
    )
    assert "0000" not in text.split("|---|", 1)[1]


def test_adr_index_without_decisions_says_so(tmp_path: Path) -> None:
    assert adr_index.render(tmp_path).endswith("| — | _No decisions yet._ | — |\n")


def test_adr_index_check_fails_when_stale_and_write_fixes_it(tmp_path: Path) -> None:
    _adr(tmp_path, "0001-a.md", "First", "Accepted")
    assert adr_index.main(["--check", str(tmp_path)]) == 1
    assert adr_index.main([str(tmp_path)]) == 0
    first = (tmp_path / "README.md").read_bytes()
    assert adr_index.main(["--check", str(tmp_path)]) == 0
    assert adr_index.main([str(tmp_path)]) == 0
    assert (tmp_path / "README.md").read_bytes() == first
    assert adr_index.main([]) == 2


def test_committed_package_indexes_are_current() -> None:
    root = Path(__file__).parents[2]
    dirs = sorted(str(p) for p in (root / "packages").glob("*/docs/adr"))
    assert dirs
    assert adr_index.main(["--check", *dirs]) == 0
