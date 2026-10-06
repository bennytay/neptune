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


def test_harness_runs_the_platform_job_which_lints_it() -> None:
    """harness/ is the platform's: its tests live in packages/neptune-platform/tests, and the
    platform job's `make lint` formats and lints harness/ (the Makefile's MEMBER_DIRS)."""
    assert _plan("harness/runner.py") == (False, ("neptune-platform",), False)
    assert _plan("harness/x.py", "docs/y.md")[:2] == (
        True,
        ("neptune-ledger", "neptune-platform", "neptune-recall"),
    )
    assert _plan("harnessy/x.py")[0] is True  # only the harness/ directory itself


def test_docsite_runs_the_platform_job_which_lints_and_tests_it() -> None:
    """docsite/ (the documentation site's build, platform ADR 0012) is the platform's like harness/;
    the site itself is built by ci.yml's unfiltered `docs` job, not selected here."""
    assert _plan("docsite/pages/index.md") == (False, ("neptune-platform",), False)
    assert _plan("docsitey/x.py")[0] is True  # only the docsite/ directory itself


def test_member_dirs_agree_with_the_makefile() -> None:
    makefile = (Path(__file__).parents[2] / "Makefile").read_text()
    (line,) = [x for x in makefile.splitlines() if x.startswith("MEMBER_DIRS :=")]
    pairs = [pair.split(":") for pair in line.split(":=", 1)[1].split()]
    assert {f"{d}/": member for member, d in pairs} == ci_plan.MEMBER_DIRS


OWNERS = {"package-schema": "neptune", "catalog-api": "neptune-ledger"}


@pytest.mark.parametrize(
    ("path", "compiler"),
    [
        ("contracts/package-schema/v1.0.0/schema.json", True),
        ("contracts/package-schema/contract.toml", True),
        ("contracts/catalog-api/v0.0.0/golden/x.json", False),
        ("contracts/lock.toml", False),
        ("contracts/unknown/contract.toml", False),
    ],
)
def test_a_compiler_owned_contract_runs_the_compiler(path: str, compiler: bool) -> None:
    """The compiler job runs the owner check for the contracts it owns."""
    result = ci_plan.plan([path], MEMBERS, OWNERS)
    assert (result.compiler, result.packages) == (
        compiler,
        tuple(sorted(MEMBERS)),
    )


def test_contract_owners_reads_each_contract_toml(tmp_path: Path) -> None:
    for name, text in [
        ("a", '[owner]\npackage = "neptune"\n'),
        ("b", '[owner]\npackage = "neptune-ledger"\n'),
        ("broken", "owner = [\n"),
        ("no-owner", 'title = "x"\n'),
    ]:
        (tmp_path / "contracts" / name).mkdir(parents=True)
        (tmp_path / "contracts" / name / "contract.toml").write_text(text)
    assert ci_plan.contract_owners(tmp_path) == {"a": "neptune", "b": "neptune-ledger"}
    assert ci_plan.contract_owners(tmp_path / "nowhere") == {}


def test_the_committed_package_schema_is_owned_by_the_compiler() -> None:
    owners = ci_plan.contract_owners(Path(__file__).parents[2])
    assert owners["package-schema"] == ci_plan.COMPILER


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


def test_committed_package_indexes_are_current() -> None:
    root = Path(__file__).parents[2]
    dirs = sorted(str(p) for p in (root / "packages").glob("*/docs/adr"))
    assert dirs
    assert adr_index.main(["--check", *dirs]) == 0


def test_adapter_only_change_runs_the_compiler_and_the_harness_not_every_dependent() -> None:
    members = {**MEMBERS, "neptune-platform": frozenset()}
    result = ci_plan.plan(["src/neptune/adapters/mcap/adapter.py"], members)
    assert (result.compiler, result.packages) == (True, ("neptune-platform",))


def test_adapter_contract_change_is_core() -> None:
    result = ci_plan.plan(["src/neptune/adapters/contract.py"], MEMBERS)
    assert result.packages == ("neptune-ledger", "neptune-recall")


def test_contracts_tool_is_plumbing() -> None:
    result = ci_plan.plan(["scripts/contracts.py"], MEMBERS)
    assert result.packages == tuple(sorted(MEMBERS))


@pytest.mark.parametrize("path", sorted(ci_plan.CORPUS_INPUTS))
def test_an_acceptance_corpus_generator_runs_the_platform_lock_test(path: str) -> None:
    members = {**MEMBERS, "neptune-deploy": frozenset()}
    packages = ci_plan.plan([path], members).packages
    assert "neptune-platform" in packages
    if path.startswith("packages/neptune-deploy/"):
        assert "neptune-deploy" in packages  # its own job still runs


def test_the_corpus_inputs_exist() -> None:
    root = Path(__file__).parents[2]
    assert all((root / path).is_file() for path in ci_plan.CORPUS_INPUTS)


@pytest.mark.parametrize(
    "path",
    [
        "packages/neptune-deploy/src/neptune_deploy/lifecycle/mapper.py",
        "packages/neptune-deploy/src/neptune_deploy/lifecycle/presets/cmms_generic.json",
        "packages/neptune-deploy/src/neptune_deploy/__main__.py",
        "packages/neptune-deploy/src/neptune_deploy/packs/cli.py",
        "packages/neptune-deploy/pyproject.toml",
    ],
)
def test_a_deploy_stage_input_runs_the_platform_tests_that_map_the_corpus(path: str) -> None:
    members = {**MEMBERS, "neptune-deploy": frozenset()}
    packages = ci_plan.plan([path], members).packages
    assert "neptune-platform" in packages and "neptune-deploy" in packages


def test_deploy_tests_and_docs_do_not_run_the_platform() -> None:
    members = {**MEMBERS, "neptune-deploy": frozenset()}
    for path in ("packages/neptune-deploy/tests/test_x.py", "packages/neptune-deploy/docs/a.md"):
        assert "neptune-platform" not in ci_plan.plan([path], members).packages


def test_the_deploy_stage_inputs_exist() -> None:
    root = Path(__file__).parents[2]
    assert all((root / path).exists() for path in ci_plan.DEPLOY_STAGE_INPUTS)


@pytest.mark.parametrize(
    "path",
    [
        "src/neptune/adapters/mcap/adapter.py",
        "src/neptune/adapters/tabular/csv_reader.py",
        "src/neptune/adapters/registry.py",  # core: Memory depends on the compiler anyway
        "harness/acceptance/generate.py",
        "harness/acceptance/deploy.json",
        "tests/fixtures/mcap/make_mcap.py",
        "packages/neptune-deploy/src/neptune_deploy/lifecycle/mapper.py",
        "harness/stages.py",
        "harness/run.py",
    ],
)
def test_what_memorys_acceptance_snapshot_is_built_through_runs_memory(path: str) -> None:
    """Memory's snapshot is rebuilt from the harness's compiled and mapped corpus packages
    (platform ADR 0009), so an adapter, the corpus, the stages or Deploy's code runs its job, and
    not its dependents' (Memory's code did not change)."""
    members = {
        **MEMBERS,
        "neptune-memory": frozenset({"neptune"}),
        "neptune-context": frozenset({"neptune-memory"}),
        "neptune-deploy": frozenset(),
    }
    packages = ci_plan.plan([path], members, snapshots=True).packages
    assert "neptune-memory" in packages
    # a compiler path outside one adapter's own subpackage is core: every compiler dependent runs
    core = path in ("src/neptune/adapters/registry.py", "tests/fixtures/mcap/make_mcap.py")
    assert ("neptune-context" in packages) is core


def test_the_rest_of_the_harness_does_not_run_memory() -> None:
    members = {**MEMBERS, "neptune-memory": frozenset({"neptune"})}
    for path in ("harness/publish.py", "harness/report.py", "harness/acceptancy/x.py"):
        assert "neptune-memory" not in ci_plan.plan([path], members, snapshots=True).packages


def test_only_ci_asks_for_the_snapshot_jobs() -> None:
    """merge_freshness.py plans without ``snapshots``: an adapter PR and a Memory change on main do
    not overlap through the snapshot (left to main's push run, platform ADR 0005)."""
    members = {**MEMBERS, "neptune-memory": frozenset({"neptune"})}
    adapter = ["src/neptune/adapters/mcap/adapter.py"]
    assert "neptune-memory" not in ci_plan.plan(adapter, members).packages
    assert "neptune-memory" in ci_plan.plan(adapter, members, snapshots=True).packages


def test_the_memory_snapshot_inputs_exist() -> None:
    root = Path(__file__).parents[2]
    assert all((root / path).exists() for path in ci_plan.MEMORY_SNAPSHOT_INPUTS)
    assert ci_plan.MEMORY_MEMBER in ci_plan.workspace_members(root)


@pytest.mark.parametrize(
    "path",
    [
        "packages/neptune-memory/src/neptune_memory/consolidate/runs.py",
        "packages/neptune-memory/pyproject.toml",
        "packages/neptune-memory/tests/fixtures/acceptance_corpus.graph.json.gz",
        "packages/neptune-memory/tests/fixtures/acceptance_corpus.memory_config.json",
        "packages/neptune-context/src/neptune_context/render/agent.py",
        "packages/neptune-context/pyproject.toml",
        "packages/neptune-context/claude/mcp.sample.json",
        "packages/neptune-context/claude/skills/neptune/SKILL.md",
        "README.md",
        "scripts/quickstart.sh",
    ],
)
def test_what_demo_v1_runs_runs_the_platform_tests(path: str) -> None:
    """The memory and context stages, Memory's committed snapshot of the corpus and the quickstart
    are what the platform's Demo v1 tests run and pin (platform ADR 0011)."""
    members = {
        **MEMBERS,
        "neptune-memory": frozenset({"neptune"}),
        "neptune-context": frozenset({"neptune-memory"}),
    }
    assert "neptune-platform" in ci_plan.plan([path], members).packages


def test_memory_and_context_tests_and_docs_do_not_run_the_platform() -> None:
    members = {**MEMBERS, "neptune-memory": frozenset(), "neptune-context": frozenset()}
    for path in (
        "packages/neptune-memory/tests/test_x.py",
        "packages/neptune-memory/tests/fixtures/other.json",
        "packages/neptune-context/docs/adr/0001-x.md",
        "docs/architecture.md",
    ):
        assert "neptune-platform" not in ci_plan.plan([path], members).packages


def test_the_agent_stage_and_quickstart_inputs_exist() -> None:
    root = Path(__file__).parents[2]
    assert all(any(root.glob(path + "*")) for path in ci_plan.AGENT_STAGE_INPUTS)
    assert all((root / path).is_file() for path in ci_plan.QUICKSTART_INPUTS)
