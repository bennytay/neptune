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


def test_the_code_the_real_ledger_stage_runs_is_inside_the_pull_request_paths() -> None:
    # The stage drives PostgresCatalog and its migrations, not only the catalog-api module.
    catalog = "packages/neptune-ledger/src/neptune_ledger/catalog"
    assert _covered(f"{catalog}/registry.py") and _covered(f"{catalog}/migrations/0001_catalog.sql")
    assert _covered("packages/neptune-ledger/pyproject.toml")  # pins pgserver


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


@pytest.mark.parametrize("name", ["run.py", "stages.py", "publish.py"])
def test_the_harness_reads_no_clock_and_no_randomness(name: str) -> None:
    text = (HARNESS / name).read_text(encoding="utf-8")
    assert not re.search(r"\b(datetime|time\.time|random|uuid)\b", text)
