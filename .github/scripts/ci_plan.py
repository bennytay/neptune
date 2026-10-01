"""Decide which CI jobs a change needs: the compiler, which members, and the template smoke.

Usage: ``python .github/scripts/ci_plan.py <event> [<base-sha> <head-sha>]``

Only ``pull_request`` events are filtered; ``merge_group``, ``push`` and others run every job.
For a pull request the changed paths are ``git diff --name-only base...head`` and:

* root plumbing (``pyproject.toml``, ``uv.lock``, ``Makefile``, ``.python-version``, ``.github/**``)
  runs every job;
* the compiler runs when any path outside ``packages/`` and ``contracts/`` changed, or a path under
  ``contracts/<id>/`` of a contract the compiler owns (so its owner check runs on the PR);
* a member runs when ``packages/<name>/**`` or ``contracts/**`` changed, or when a workspace
  project it depends on (the compiler is the project ``neptune``) runs;
* the template smoke runs when ``packages/_template/**`` or ``scripts/new-package.sh`` changed.

Writes ``compiler``, ``packages`` (a JSON list) and ``template`` to ``$GITHUB_OUTPUT`` when set, and
always to stdout. Standard library only, so it runs before anything is installed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

COMPILER = "neptune"
TEMPLATE_DIR = "packages/_template/"
PLUMBING_FILES = frozenset({"pyproject.toml", "uv.lock", "Makefile", ".python-version"})
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


@dataclass(frozen=True)
class Plan:
    compiler: bool
    packages: tuple[str, ...]
    template: bool

    def outputs(self) -> str:
        return (
            f"compiler={str(self.compiler).lower()}\n"
            f"packages={json.dumps(list(self.packages), separators=(',', ':'))}\n"
            f"template={str(self.template).lower()}\n"
        )


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def workspace_members(root: Path) -> dict[str, frozenset[str]]:
    """Each member directory under ``packages/`` mapped to its declared dependency names."""
    members: dict[str, frozenset[str]] = {}
    for pyproject in sorted((root / "packages").glob("*/pyproject.toml")):
        name = pyproject.parent.name
        if name.startswith("_"):
            continue
        project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
        deps = {
            _normalise(match[1])
            for requirement in project.get("dependencies", [])
            if (match := _REQUIREMENT_NAME.match(requirement))
        }
        members[name] = frozenset(deps)
    return members


def contract_owners(root: Path) -> dict[str, str]:
    """Each contract id under ``contracts/`` mapped to its ``[owner].package``.

    A contract.toml that does not parse is left out; the registry check reports it.
    """
    owners: dict[str, str] = {}
    for path in sorted((root / "contracts").glob("*/contract.toml")):
        try:
            owner = tomllib.loads(path.read_text(encoding="utf-8")).get("owner")
        except tomllib.TOMLDecodeError:
            continue
        if isinstance(owner, dict) and isinstance(owner.get("package"), str):
            owners[path.parent.name] = owner["package"]
    return owners


def plan(
    changed: list[str] | None,
    members: dict[str, frozenset[str]],
    owners: dict[str, str] | None = None,
) -> Plan:
    """The jobs to run; ``changed=None`` means unfiltered (every job).

    ``owners`` maps contract ids to owner packages (``contract_owners``).
    """
    if changed is None or any(p in PLUMBING_FILES or p.startswith(".github/") for p in changed):
        return Plan(compiler=True, packages=tuple(sorted(members)), template=True)
    owned = {
        (owners or {}).get(p.split("/")[1])
        for p in changed
        if p.startswith("contracts/") and p.count("/") >= 2
    }
    compiler = COMPILER in owned or any(
        not p.startswith(("packages/", "contracts/")) for p in changed
    )
    contracts = any(p.startswith("contracts/") for p in changed)
    affected = {
        name
        for name in members
        if contracts or any(p.startswith(f"packages/{name}/") for p in changed)
    }
    projects = {_normalise(name): name for name in members}
    grew = True
    while grew:  # propagate through workspace dependencies until nothing new runs
        grew = False
        for name, deps in members.items():
            if name in affected:
                continue
            if (compiler and COMPILER in deps) or any(projects.get(d) in affected for d in deps):
                affected.add(name)
                grew = True
    template = any(p.startswith(TEMPLATE_DIR) or p == "scripts/new-package.sh" for p in changed)
    return Plan(compiler=compiler, packages=tuple(sorted(affected)), template=template)


def changed_paths(base: str, head: str) -> list[str]:
    diff = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in diff.stdout.splitlines() if line]


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 3):
        sys.stderr.write("usage: ci_plan.py <event> [<base-sha> <head-sha>]\n")
        return 2
    changed = (
        changed_paths(argv[1], argv[2]) if argv[0] == "pull_request" and len(argv) == 3 else None
    )
    root = Path.cwd()
    result = plan(changed, workspace_members(root), contract_owners(root)).outputs()
    sys.stdout.write(result)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
