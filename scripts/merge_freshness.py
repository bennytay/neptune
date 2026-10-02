"""Decide whether a PR that is behind ``main`` can merge without being refreshed.

Usage: ``python scripts/merge_freshness.py <pr-files> <main-files>``

Both arguments are files with one changed path per line: what the PR changes (``base...head``) and
what ``main`` changed since the PR's merge base. Prints ``fresh`` and exits 0 when the PR's green
``check`` still covers it; otherwise prints ``refresh: <reason>`` and exits 1.

There is no merge queue (a personal-account repository cannot have one), so ``main`` does not
require branches to be up to date. A PR may merge while behind when nothing ``main`` changed since
its merge base could change the outcome of the jobs that tested it. The unit of change follows CI
(``.github/scripts/ci_plan.py``):

* root plumbing (``pyproject.toml``, ``uv.lock``, ``Makefile``, ``.python-version``,
  ``.github/**``) and ``contracts/**`` touch everything, so either side changing them forces a
  refresh;
* ``packages/<name>/**`` is the member ``name`` (``harness/**`` is ``neptune-platform``'s);
* ``src/neptune/adapters/<format>/**`` is that adapter alone: adapters never import each other and
  no member imports an adapter (AGENTS.md), so two adapters, or an adapter and a member, never
  overlap;
* the template is ``packages/_template/**`` and ``scripts/new-package.sh``;
* every other path is the compiler core, which every adapter and every member depending on
  ``neptune`` sees.

A unit reaches the members that depend on it, transitively. The PR is fresh when the units reached
from its changes and from ``main``'s changes are disjoint. A ``check`` on ``main`` after every merge
(``push`` runs every job) catches what this rule cannot, and factory-merge.sh refuses to merge while
that check is red. Decision record:
``packages/neptune-platform/docs/adr/0005-merge-without-a-queue.md``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
EVERYTHING = "*"
COMPILER = "neptune"
ADAPTERS = "src/neptune/adapters/"


def _ci_plan() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_plan", ROOT / ".github/scripts/ci_plan.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_plan"] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


ci_plan = _ci_plan()


def unit(path: str) -> str:
    """The unit a changed path belongs to; ``EVERYTHING`` for plumbing and contracts."""
    if path in ci_plan.PLUMBING_FILES or path.startswith((".github/", "contracts/")):
        return EVERYTHING
    if path.startswith(ci_plan.TEMPLATE_DIR) or path == "scripts/new-package.sh":
        return "template"
    top = path.split("/", 1)[0] + "/"
    if top in ci_plan.MEMBER_DIRS:
        return str(ci_plan.MEMBER_DIRS[top])
    if path.startswith("packages/") and path.count("/") >= 2:
        return path.split("/")[1]
    rest = path.removeprefix(ADAPTERS)
    if rest != path and "/" in rest:  # a file inside one adapter's own subpackage
        return f"adapter:{rest.split('/', 1)[0]}"
    return COMPILER


def reach(units: set[str], members: dict[str, frozenset[str]]) -> set[str]:
    """``units`` plus every member that depends on one of them, transitively."""
    projects = {ci_plan._normalise(name): name for name in members}
    reached = set(units)
    grew = True
    while grew:
        grew = False
        for name, deps in members.items():
            if name in reached:
                continue
            if any(
                (d == COMPILER and COMPILER in reached) or projects.get(d) in reached for d in deps
            ):
                reached.add(name)
                grew = True
    return reached


def _overlap(a: set[str], b: set[str]) -> str | None:
    if EVERYTHING in a or EVERYTHING in b:
        return "root plumbing or contracts/ changed"
    if shared := a & b:
        return f"both sides reach {', '.join(sorted(shared))}"
    for core, other in ((a, b), (b, a)):
        if COMPILER in core and (adapters := sorted(u for u in other if u.startswith("adapter:"))):
            return f"the compiler core changed under {', '.join(adapters)}"
    return None


def decide(pr: list[str], main: list[str], members: dict[str, frozenset[str]]) -> str | None:
    """``None`` when the PR is fresh, else the reason it needs a refresh."""
    if not main:
        return None
    return _overlap(reach({unit(p) for p in pr}, members), reach({unit(p) for p in main}, members))


def _lines(path: str) -> list[str]:
    return [line for line in Path(path).read_text(encoding="utf-8").splitlines() if line]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: merge_freshness.py <pr-files> <main-files>\n")
        return 2
    reason = decide(_lines(argv[0]), _lines(argv[1]), ci_plan.workspace_members(ROOT))
    sys.stdout.write("fresh\n" if reason is None else f"refresh: {reason}\n")
    return 0 if reason is None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
