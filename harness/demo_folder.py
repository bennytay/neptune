"""``python -m harness.demo_folder DIR`` (``make demo-folder DIR=<path>``): a clean folder for using
or recording Demo v1 with Claude Code.

Claude Code started inside this clone also reads the repository's ``CLAUDE.md`` and ``AGENTS.md``,
which are for building Neptune, not for asking it. The folder holds only what an agent needs:

- ``.mcp.json``: the ``neptune`` server, run from this clone by absolute paths, over
  ``demo/graph.json`` and ``demo/sources.json`` when ``make demo`` has written both, else over
  Memory's committed snapshot of the acceptance corpus and its committed source index;
- ``.claude/skills/neptune/``: a copy of ``packages/neptune-context/claude/skills/neptune``;
- ``.claude/settings.json``: ``{"env": {"MAX_MCP_OUTPUT_TOKENS": "100000"}}``, so a whole
  comparison reaches the agent.

Nothing else. ``DIR`` is created when missing. A folder that already holds anything else is
refused and left untouched; a previous demo folder is rewritten (a ``.claude/settings.local.json``
Claude Code wrote there, such as an approval of the server, is kept). Symlinks are never followed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from harness.corpus import REPO

SKILL: Final = REPO / "packages" / "neptune-context" / "claude" / "skills" / "neptune"
DEMO: Final = REPO / "demo"
SNAPSHOT: Final = (
    REPO / "packages" / "neptune-memory" / "tests" / "fixtures" / "acceptance_corpus.graph.json.gz"
)
SOURCES: Final = REPO / "harness" / "acceptance" / "sources.json"
SETTINGS: Final = {"env": {"MAX_MCP_OUTPUT_TOKENS": "100000"}}
SERVER: Final = "neptune"
# What a demo folder may hold: these, and a settings.local.json Claude Code writes beside settings.
OWN: Final[Mapping[str, object]] = {
    ".mcp.json": None,
    ".claude": {"settings.json": None, "skills": {"neptune": ...}},
}
KEPT: Final = frozenset({".claude/settings.local.json"})


class Refused(Exception):
    """The folder cannot be written: it holds something a demo folder does not."""


def served(demo: Path = DEMO) -> tuple[Path, Path]:
    """The graph and source index the folder's server reads: ``make demo``'s when it wrote both,
    else the committed snapshot and source index."""
    graph, sources = demo / "graph.json", demo / "sources.json"
    if graph.is_file() and sources.is_file():
        return graph.resolve(), sources.resolve()
    return SNAPSHOT, SOURCES


def mcp_config(graph: Path, sources: Path, uv: str | None = None) -> dict[str, object]:
    """The ``.mcp.json`` document: the server run from this clone, every path absolute."""
    command = uv or shutil.which("uv") or "uv"
    args = ["run", "--directory", str(REPO), "--all-packages", "python", "-m"]
    args += ["neptune_context.mcp", "--memory", str(graph), "--sources", str(sources)]
    return {"mcpServers": {SERVER: {"type": "stdio", "command": command, "args": args}}}


def _foreign(folder: Path, allowed: Mapping[str, object], at: str = "") -> list[str]:
    """What under ``folder`` a demo folder does not hold (relative paths), without following a
    symlink: a symlink is always foreign. ``allowed`` maps a name to ``None`` (a file), ``...``
    (a directory, whatever it holds: the skill copy) or the same kind of map (a directory)."""
    out = []
    for entry in sorted(folder.iterdir()):
        name = f"{at}{entry.name}"
        if name in KEPT and entry.is_file() and not entry.is_symlink():
            continue
        spec = allowed.get(entry.name, False)
        if entry.is_symlink() or spec is False:
            out.append(name)
        elif spec is None:
            out += [] if entry.is_file() else [name]
        elif not entry.is_dir():
            out.append(name)
        elif isinstance(spec, Mapping):
            out += _foreign(entry, spec, f"{name}/")
    return out


def check(folder: Path) -> None:
    """Refuse ``folder`` unless it is missing, empty or a previous demo folder."""
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise Refused(f"{folder} is not a directory")
    if not folder.exists():
        return
    foreign = _foreign(folder, OWN)
    if foreign:
        shown = ", ".join(foreign[:5]) + (", ..." if len(foreign) > 5 else "")
        raise Refused(f"{folder} is not empty and not a demo folder (it holds {shown})")
    mcp = folder / ".mcp.json"
    if mcp.is_file():
        try:
            document = json.loads(mcp.read_text(encoding="utf-8"))
            servers = set(document["mcpServers"])
        except (ValueError, KeyError, TypeError, OSError):
            servers = set()
        if servers != {SERVER}:
            raise Refused(f"{mcp} is not a demo folder's (it must serve only {SERVER!r})")


def write(folder: Path, demo: Path = DEMO, uv: str | None = None) -> tuple[Path, Path]:
    """Write the demo folder at ``folder`` (see the module docstring); the graph and source index
    it serves. ``Refused`` when ``folder`` holds anything else; nothing is written then."""
    check(folder)
    graph, sources = served(demo)
    claude = folder / ".claude"
    skill = claude / "skills" / "neptune"
    if skill.exists():
        shutil.rmtree(skill)
    skill.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SKILL, skill, symlinks=False)
    (claude / "settings.json").write_text(json.dumps(SETTINGS, indent=2) + "\n", encoding="utf-8")
    config = mcp_config(graph, sources, uv)
    (folder / ".mcp.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return graph, sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness.demo_folder", description=(__doc__ or "").split("\n")[0]
    )
    parser.add_argument("dir", type=Path, help="the folder to write (created when missing)")
    parser.add_argument(
        "--demo", type=Path, default=DEMO, help="where `make demo` wrote its output"
    )
    args = parser.parse_args(argv)
    folder = args.dir.expanduser().absolute()
    try:
        graph, sources = write(folder, args.demo.expanduser().absolute())
    except (Refused, OSError) as error:
        sys.stderr.write(f"demo-folder: {error}\n")
        return 2
    sys.stdout.write(
        f"wrote {folder}: .mcp.json (neptune over {graph}, sources {sources}),"
        " .claude/skills/neptune, .claude/settings.json\n"
        f"use it: cd {folder} && claude\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
