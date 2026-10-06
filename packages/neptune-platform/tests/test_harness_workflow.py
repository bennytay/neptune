"""The workflow and the compose stack: triggers, token scope, path coverage, pinned images."""

import re
import tomllib
from pathlib import Path
from typing import Final

import pytest
from harness import contracts
from harness.stages import STAGES

REPO: Final = Path(__file__).resolve().parents[3]
WORKFLOW: Final = (REPO / ".github" / "workflows" / "harness.yml").read_text(encoding="utf-8")
HARNESS: Final = REPO / "harness"


def _pull_request_paths() -> list[str]:
    block = WORKFLOW.split("  pull_request:\n", 1)[1].split("\npermissions:", 1)[0]
    return re.findall(r'^\s+- "([^"]+)"$', block, re.MULTILINE)


def _glob(pattern: str) -> re.Pattern[str]:
    """GitHub's filter globs: ``**`` crosses directories, ``*`` and ``?`` do not."""
    out = ""
    for part in re.split(r"(\*\*|\*|\?)", pattern):
        out += {"**": ".*", "*": "[^/]*", "?": "[^/]"}.get(part, re.escape(part))
    return re.compile(out + r"\Z")


def _covered(path: str) -> bool:
    return any(_glob(pattern).match(path) for pattern in _pull_request_paths())


def test_it_runs_in_the_merge_queue_nightly_on_demand_and_on_pull_requests() -> None:
    triggers = WORKFLOW.split("\npermissions:", 1)[0]
    for trigger in ("merge_group:", "schedule:", "workflow_dispatch:", "pull_request:"):
        assert re.search(rf"^  {trigger}", triggers, re.MULTILINE), trigger
    assert re.search(r'cron: "[^"]+"', triggers)


def test_it_never_uses_pull_request_target_and_only_the_comment_job_can_write() -> None:
    assert "pull_request_target" not in re.sub(r"#.*", "", WORKFLOW)
    assert re.search(r"^permissions:\n  contents: read\n", WORKFLOW, re.MULTILINE)
    assert WORKFLOW.count("pull-requests: write") == 1
    comment = WORKFLOW.split("\n  comment:\n", 1)[1]
    assert "pull-requests: write" in comment
    assert "actions/checkout" not in comment  # it posts a finished report, it runs no PR code
    assert "github.event.pull_request.head.repo.full_name == github.repository" in comment
    assert "GITHUB_TOKEN" in comment and "LINEAR_API_KEY" not in comment


def test_linear_is_posted_to_only_on_schedule_and_dispatch() -> None:
    step = WORKFLOW.split("Post to the Linear gate issues", 1)[1].split("\n  comment:", 1)[0]
    assert "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'" in step
    assert "secrets.LINEAR_API_KEY" in step
    assert WORKFLOW.count("secrets.LINEAR_API_KEY") == 1


def test_the_harness_is_not_wired_into_the_required_status() -> None:
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "harness" not in ci


def test_every_contracts_exported_module_is_inside_the_pull_request_paths() -> None:
    registry = contracts.registry()
    members = {
        name: table.get("path", f"packages/{name}")
        for name, table in tomllib.loads(
            (REPO / "contracts" / "packages.toml").read_text(encoding="utf-8")
        ).items()
    }
    checked = 0
    for contract_id in registry.contract_ids():
        owner = registry.contract(contract_id).owner
        base = members[owner.package]
        references = (owner.module, owner.schema_export, owner.version_constant)
        modules = {ref.partition(":")[0] for ref in references if ref}
        for module in sorted(modules):
            relative = module.replace(".", "/")
            src = Path(base) / "src"
            candidates = [
                (src / f"{relative}.py").as_posix(),
                (src / relative / "__init__.py").as_posix(),
            ]
            assert any(_covered(c) for c in candidates), f"{contract_id}: {module} is not watched"
            checked += 1
    assert checked >= 8
    assert _covered("contracts/lock.toml") and _covered("harness/run.py")
    assert not _covered("docs/architecture.md")


def test_the_code_the_real_stages_run_is_inside_the_pull_request_paths() -> None:
    # The compiler stage runs the SDK and the store, not only the model.
    for path in (
        "src/neptune/sdk/__init__.py",
        "src/neptune/store/package.py",
        "src/neptune/model/x.py",
    ):
        assert _covered(path), path
    # register drives the catalog and its migrations, the lake, threads and lineage.
    ledger = "packages/neptune-ledger/src/neptune_ledger"
    for part in (
        "catalog/registry.py",
        "catalog/migrations/0001_catalog.sql",
        "lake/indexes.py",
        "threads/merge.py",
        "lineage/x.py",
    ):
        assert _covered(f"{ledger}/{part}"), part
    assert _covered("packages/neptune-ledger/pyproject.toml")  # pins pgserver
    assert not _covered("packages/neptune-ledger/tests/test_ledger_registration.py")


def test_the_deploy_stages_code_and_declarations_run_the_harness_and_the_platform_job() -> None:
    """The deploy stage runs ``python -m neptune_deploy map`` with the corpus's declared presets and
    templates (platform ADR 0008): a change to any of them runs the harness, and the required
    check's plan runs this package's job, whose tests map the corpus."""
    import importlib.util
    import json
    import sys

    from harness import acceptance

    lifecycle = "packages/neptune-deploy/src/neptune_deploy/lifecycle"
    watched = [
        f"{lifecycle}/mapper.py",
        f"{lifecycle}/presets/cmms_generic.json",
        "packages/neptune-deploy/src/neptune_deploy/__main__.py",
        "packages/neptune-deploy/src/neptune_deploy/packs/cli.py",  # the CLI imports it at start
        "packages/neptune-deploy/pyproject.toml",
        *json.loads(acceptance.DEPLOY.read_text(encoding="utf-8"))["templates"],
    ]
    spec = importlib.util.spec_from_file_location(
        "workflow_ci_plan", REPO / ".github" / "scripts" / "ci_plan.py"
    )
    assert spec is not None and spec.loader is not None
    ci_plan = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ci_plan
    spec.loader.exec_module(ci_plan)
    for path in watched:
        assert _covered(path), path
        assert path.startswith(ci_plan.DEPLOY_STAGE_INPUTS), path
    assert not _covered("packages/neptune-deploy/tests/test_deploy_packs_render.py")


def test_what_memorys_snapshot_is_built_through_runs_the_harness() -> None:
    """Memory's acceptance snapshot is built from the harness's packages (platform ADR 0009): an
    adapter or an acceptance-corpus change runs the harness, as it runs Memory's job in ci_plan."""
    for path in (
        "src/neptune/adapters/mcap/adapter.py",
        "src/neptune/adapters/tabular/csv_reader.py",
        "harness/acceptance/generate.py",
        "harness/acceptance/deploy.json",
        "harness/acceptance/gold.json",
    ):
        assert _covered(path), path


def test_every_contracts_owner_tests_are_inside_the_pull_request_paths() -> None:
    registry = contracts.registry()
    checked = 0
    for contract_id in registry.contract_ids():
        for target in registry.contract(contract_id).owner.contract_tests:
            path = REPO / target
            probe = f"{target}/test_probe.py" if path.is_dir() else target
            assert path.exists() and _covered(probe), f"{contract_id}: {target} is not watched"
            checked += 1
    assert checked >= 7  # package-schema 3, catalog-api 2, graph-schema 2


def test_the_compose_stack_has_postgres_and_minio_pinned() -> None:
    compose = (HARNESS / "compose.yaml").read_text(encoding="utf-8")
    assert re.search(r"^  postgres:", compose, re.MULTILINE)
    assert re.search(r"^  minio:", compose, re.MULTILINE)
    external = [
        image
        for image in re.findall(r"^\s+image:\s*(\S+)", compose, re.MULTILINE)
        if not image.startswith("neptune-harness-")
    ]
    assert external, "the MinIO image"
    for image in external:
        assert "@sha256:" in image or re.search(r":[^:@]*\d[^:@]*$", image), image
        assert not image.endswith(":latest")
    assert "127.0.0.1:" in compose and "0.0.0.0" not in compose  # loopback only


def test_the_postgres_image_pins_its_base_by_digest_and_adds_age_and_pgvector() -> None:
    dockerfile = (HARNESS / "postgres" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(
        r"^FROM apache/age:release_PG16_1\.5\.0@sha256:[0-9a-f]{64}$", dockerfile, re.M
    )
    assert "postgresql-16-pgvector" in dockerfile


def test_ci_needs_no_docker_while_no_stage_that_needs_services_is_real() -> None:
    real_and_needy = [s.id for s in STAGES if s.needs_services and s.real is not None]
    assert real_and_needy == []  # when this fails, the workflow needs the compose stack


@pytest.mark.parametrize("name", ["run.py", "stages.py", "publish.py", "consolidate.py", "demo.py"])
def test_the_harness_reads_no_clock_and_no_randomness(name: str) -> None:
    text = (HARNESS / name).read_text(encoding="utf-8")
    assert not re.search(r"\b(datetime|time\.time|random|uuid)\b", text)


def test_the_agent_stage_reads_no_clock_and_no_randomness() -> None:
    # It names a timedelta (the MCP session's read timeout), never the time.
    text = (HARNESS / "agent.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(now|today|utcnow|time\.time|monotonic|random|uuid)\b", text)


def _job(name: str) -> str:
    return WORKFLOW.split(f"\n  {name}:\n", 1)[1].split("\n  comment:", 1)[0]


def test_the_quickstart_job_runs_the_readmes_script_on_a_clean_runner_in_15_minutes() -> None:
    """Demo v1 (platform ADR 0011): the README's quickstart is what CI runs, as a new user would:
    no setup-uv, no cache, only the checkout and the script, within the README's fifteen minutes."""
    job = _job("quickstart")
    assert "timeout-minutes: 15" in job
    assert "run: bash scripts/quickstart.sh" in job
    assert "setup-uv" not in job and "make setup" not in job  # the script installs everything
    assert "permissions:\n      contents: read" in job and "secrets." not in job
    assert (REPO / "scripts" / "quickstart.sh").stat().st_mode & 0o111  # executable


def test_what_demo_v1_runs_is_inside_the_pull_request_paths() -> None:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "workflow_ci_plan_agent", REPO / ".github" / "scripts" / "ci_plan.py"
    )
    assert spec is not None and spec.loader is not None
    ci_plan = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ci_plan
    spec.loader.exec_module(ci_plan)
    watched = [
        "packages/neptune-memory/src/neptune_memory/cli/__init__.py",
        "packages/neptune-memory/pyproject.toml",
        "packages/neptune-memory/tests/fixtures/acceptance_corpus.graph.json.gz",
        "packages/neptune-memory/tests/fixtures/acceptance_corpus.memory_config.json",
        "packages/neptune-context/src/neptune_context/mcp/server.py",
        "packages/neptune-context/pyproject.toml",
        "packages/neptune-context/claude/mcp.sample.json",
        "packages/neptune-context/claude/skills/neptune/SKILL.md",
    ]
    for path in watched:
        assert _covered(path), path
        assert path.startswith(ci_plan.AGENT_STAGE_INPUTS), path
    assert _covered("scripts/quickstart.sh") and _covered("Makefile")
    assert not _covered("packages/neptune-memory/tests/test_cli_memory.py")
    assert not _covered("packages/neptune-context/docs/sdk.md")
