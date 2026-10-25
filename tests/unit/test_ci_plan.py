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


def test_adr_index_shows_who_amends_a_decision(tmp_path: Path) -> None:
    _adr(tmp_path, "0001-a.md", "First", "Accepted")
    _adr(tmp_path, "0002-b.md", "Second", "Accepted; §3 amended by 0003")
    (tmp_path / "0003-c.md").write_text(
        "# 0003 — Third\n\n- Status: Accepted\n- Amends: ADR 0001 §2 (one thing), ADR 0002\n"
        "  §3 (wrapped); settles ADR 0004's open point\n\n## Context\n\nAmends: ADR 0001\n"
    )
    rows = adr_index.render(tmp_path, compiler=True).splitlines()
    assert (
        "amended by 0003 |" not in adr_index.render(tmp_path).split("| First |")[1].split("\n")[0]
    )
    assert rows[-3].endswith("| First | Accepted; amended by 0003 |")
    assert rows[-2].endswith("| Second | Accepted; §3 amended by 0003 |")  # already named
    assert "amended by" not in rows[-1]  # the settled ADR 0004 is not amended


def test_adr_index_compiler_header_and_determinism(tmp_path: Path) -> None:
    _adr(tmp_path, "0001-a.md", "First", "Accepted")
    compiler = adr_index.render(tmp_path, compiler=True)
    assert "Status values:" in compiler
    assert "Numbers are local" in adr_index.render(tmp_path)
    assert adr_index.main(["--compiler", str(tmp_path)]) == 0
    assert (tmp_path / "README.md").read_text() == compiler
    assert adr_index.main(["--check", "--compiler", str(tmp_path)]) == 0
    assert adr_index.main(["--check", str(tmp_path)]) == 1  # the other header: stale


def test_compiler_adr_index_is_current() -> None:
    """A PR adding an ADR runs `make adr-index` (or `make fmt`); nobody edits the table by hand."""
    root = Path(__file__).parents[2]
    assert adr_index.main(["--check", "--compiler", str(root / "docs" / "adr")]) == 0
