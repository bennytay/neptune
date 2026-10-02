"""Decide whether a PR that is behind ``main`` can merge without being refreshed.

Usage: ``python scripts/merge_freshness.py <pr-files> <main-files> [<git-ref>]``

Both arguments are files with one changed path per line: what the PR changes and what ``main``
changed since the PR's merge base (old and new paths of renames included). The workspace graph is
read at ``<git-ref>`` when given (factory-merge.sh passes ``origin/main``), otherwise from this
checkout. Prints ``fresh`` and exits 0 when the PR's green ``check`` still covers it; otherwise
prints ``refresh: <reason>`` and exits 1.

There is no merge queue (a personal-account repository cannot have one), so ``main`` does not
require branches to be up to date. Each side's changes select CI jobs exactly as CI does
(``.github/scripts/ci_plan.py``). A job selected by both sides ran on the PR against a ``main``
whose inputs to that job have since changed, so the PR needs a refresh. Disjoint job sets mean
every job the PR ran would give the same result on top of today's ``main``.

The platform job is left out of the comparison: its integration harness drives the whole stack, so
it would make every pair of PRs overlap. Integration breaks between independently green PRs surface
in the ``push`` run on ``main`` (every job), and factory-merge.sh stops merging while that is red.
Decision record: ``packages/neptune-platform/docs/adr/0005-merge-without-a-queue.md``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _ci_plan() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_plan", ROOT / ".github/scripts/ci_plan.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_plan"] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


ci_plan = _ci_plan()
EXCLUDED = frozenset({ci_plan.HARNESS_MEMBER})


def jobs(changed: list[str], members: dict[str, frozenset[str]], root: Path = ROOT) -> set[str]:
    """The CI jobs ``changed`` selects: ``neptune``, member names and ``template``."""
    selected = ci_plan.plan(changed, members, ci_plan.contract_owners(root))
    return (
        set(selected.packages)
        | ({ci_plan.COMPILER} if selected.compiler else set())
        | ({"template"} if selected.template else set())
    )


def decide(pr: list[str], main: list[str], members: dict[str, frozenset[str]]) -> str | None:
    """``None`` when the PR is fresh, else the reason it needs a refresh."""
    if not pr or not main:
        return None
    if shared := (jobs(pr, members) & jobs(main, members)) - EXCLUDED:
        return f"main changed inputs to {', '.join(sorted(shared))}"
    return None


def members_at(ref: str) -> dict[str, frozenset[str]]:
    """``ci_plan.workspace_members`` as of the git ``ref``, not the checkout running this script."""
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-tree", "--name-only", f"{ref}:packages"],
        check=True,
        capture_output=True,
        text=True,
    )
    with tempfile.TemporaryDirectory() as tmp:
        for name in listing.stdout.split():
            shown = subprocess.run(
                ["git", "-C", str(ROOT), "show", f"{ref}:packages/{name}/pyproject.toml"],
                capture_output=True,
                text=True,
                check=False,
            )
            if shown.returncode == 0:
                (Path(tmp) / "packages" / name).mkdir(parents=True)
                (Path(tmp) / "packages" / name / "pyproject.toml").write_text(shown.stdout)
        return dict(ci_plan.workspace_members(Path(tmp)))


def _lines(path: str) -> list[str]:
    return [line for line in Path(path).read_text(encoding="utf-8").splitlines() if line]


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        sys.stderr.write("usage: merge_freshness.py <pr-files> <main-files> [<git-ref>]\n")
        return 2
    members = members_at(argv[2]) if len(argv) == 3 else ci_plan.workspace_members(ROOT)
    reason = decide(_lines(argv[0]), _lines(argv[1]), members)
    sys.stdout.write("fresh\n" if reason is None else f"refresh: {reason}\n")
    return 0 if reason is None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
