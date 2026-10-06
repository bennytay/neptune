"""The README's 15-minute quickstart is the script CI runs, and its status numbers are the pins
(Platform ADR 0011)."""

import json
import re
from pathlib import Path
from typing import Final

from harness import acceptance

REPO: Final = Path(__file__).resolve().parents[3]
README: Final = (REPO / "README.md").read_text(encoding="utf-8")
SCRIPT: Final = REPO / "scripts" / "quickstart.sh"
START: Final = "# --- quickstart ---\n"
END: Final = "# --- end ---\n"


def _script_block() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.count(START) == 1 and text.count(END) == 1
    return text.split(START, 1)[1].split(END, 1)[0]


def _readme_block() -> str:
    section = README.split("## 15-minute quickstart\n", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```bash\n(.*?)```", section, re.DOTALL)
    assert len(blocks) == 1, "the quickstart section holds exactly one bash block"
    return str(blocks[0])


def test_the_readme_quickstart_block_is_the_script_ci_runs() -> None:
    assert _readme_block() == _script_block()


def test_the_script_is_strict_and_ends_with_the_demo_wired_into_claude_code() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n") and "\nset -euo pipefail\n" in text
    lines = _script_block().splitlines()
    assert "make setup" in lines and "make demo" in lines
    assert lines.index("make setup") < lines.index("make demo")
    # what it copies exists, and what it serves is what `make demo` writes
    for path in ("packages/neptune-context/claude/mcp.sample.json",):
        assert path in _script_block() and (REPO / path).is_file()
    assert (REPO / "packages/neptune-context/claude/skills/neptune/SKILL.md").is_file()
    assert "NEPTUNE_MEMORY_GRAPH=demo/graph.json" in _script_block()
    assert "learn" not in _script_block().lower()  # no LeRobot step in Demo v1


def test_what_the_quickstart_writes_is_ignored_by_git() -> None:
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in ("/demo/", "/.mcp.json", "/.claude/skills/"):
        assert entry in ignored, entry


def test_the_makefile_has_the_demo_targets() -> None:
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^demo: ## ", makefile, re.MULTILINE)
    assert re.search(r"^demo-pin: ## ", makefile, re.MULTILINE)
    assert "python -m harness.demo" in makefile


def test_the_readme_status_counts_are_the_pinned_answers() -> None:
    answers = json.loads(acceptance.ANSWERS.read_text(encoding="utf-8"))
    supported = sum(len(q["supported"]) for q in answers["questions"])
    gaps = sum(len(q["gaps"]) for q in answers["questions"])
    status = README.split("## Status\n", 1)[1].split("\n## ", 1)[0]
    assert f"{supported} of {supported + gaps} gold claims" in status
    assert f"the other {gaps}," in status
    assert "make demo" in status
