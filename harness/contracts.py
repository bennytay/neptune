"""``scripts/contracts.py`` as a module, so the harness reads the registry the way its owner does.

The tool is a script, not a package, so it is loaded from its path and registered under a private
name (its dataclasses need the module in ``sys.modules``). Everything it returns is typed ``Any``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

REPO: Final = Path(__file__).resolve().parents[1]
TOOL: Final = REPO / "scripts" / "contracts.py"
_NAME: Final = "neptune_contracts_tool"


def load_tool() -> Any:
    """The registry tool module (``Registry``, ``post_comment``, ``validate_golden`` ...)."""
    if _NAME in sys.modules:
        return sys.modules[_NAME]
    spec = importlib.util.spec_from_file_location(_NAME, TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {TOOL}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module
    spec.loader.exec_module(module)
    return module


def registry(root: Path | None = None) -> Any:
    """The ``Registry`` over ``root`` (default: the repository's ``contracts/``)."""
    tool = load_tool()
    return tool.Registry((root or REPO / "contracts").resolve())


def run_check_all(root: Path | None, *, owner_tests: bool) -> tuple[int, list[str], list[str], str]:
    """``contracts.py check --all`` in a child process: (exit code, notes, problems, tail).

    Only the tool's own lines are kept (``<id> <version>: ...`` notes and ``FAIL:`` problems), so
    the report never carries pytest timings. ``tail`` is the last lines of all output, for a
    failed run only.
    """
    command = [sys.executable, str(TOOL)]
    if root is not None:
        command += ["--root", str(root)]
    command += ["check", "--all"]
    if not owner_tests:
        command.append("--no-tests")
    done = subprocess.run(command, cwd=REPO, check=False, capture_output=True, text=True)
    notes = [line for line in done.stdout.splitlines() if _is_tool_line(line)]
    problems = [
        line.removeprefix("FAIL: ") for line in done.stderr.splitlines() if line.startswith("FAIL:")
    ]
    tail = ""
    if done.returncode != 0:
        tail = "\n".join((done.stdout + done.stderr).splitlines()[-20:])
    return done.returncode, notes, problems, tail


def _is_tool_line(line: str) -> bool:
    head = line.split(" ", 1)[0].rstrip(":")
    return (
        bool(head)
        and head[0].islower()
        and all(c.islower() or c.isdigit() or c == "-" for c in head)
    )
