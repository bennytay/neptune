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
    # uv, not make: the Makefile needs GNU make 3.82+, and macOS ships 3.81 (ADR 0011 §6)
    setup = "uv sync --all-packages --all-groups"
    demo = "uv run --all-packages --all-groups python -m harness.demo --out demo"
    assert setup in lines and demo in lines and lines.index(setup) < lines.index(demo)
    assert not any(line.startswith(("make ", "gmake ")) for line in lines)
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


def test_the_makefile_targets_run_the_same_commands_as_the_script() -> None:
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^demo: ## ", makefile, re.MULTILINE)
    assert re.search(r"^demo-pin: ## ", makefile, re.MULTILINE)
    assert "> $(UV) sync --all-packages --all-groups" in makefile
    assert '> $(RUN) python -m harness.demo --out "$(DEMO_DIR)"' in makefile
    assert "RUN := $(UV) run --all-packages --all-groups" in makefile


def test_the_readme_status_counts_are_the_pinned_answers() -> None:
    """The README counts only ``supported`` as answered (ADR 0011 §4)."""
    answers = json.loads(acceptance.ANSWERS.read_text(encoding="utf-8"))
    count = {
        k: sum(len(q[k]) for q in answers["questions"]) for k in ("supported", "co_cited", "gaps")
    }
    status = README.split("## Status\n", 1)[1].split("\n## ", 1)[0]
    total = sum(count.values())
    assert f"({count['supported']} of {total} gold claims supported)" in status
    assert f"{count['co_cited']} gold claims are cited only" in status
    assert f"{count['gaps']} are gaps" in status
    assert "neptune_hydrate" in README and "unavailable" in README
