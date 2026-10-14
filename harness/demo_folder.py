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

Nothing else. ``DIR`` is created when missing. A non-empty folder is rewritten only when it is
provably this tool's: it holds nothing but the files the tool writes (and a
``.claude/settings.local.json`` Claude Code wrote there, such as an approval of the server, which is
kept), with a ``.mcp.json`` of the tool's shape and the tool's ``settings.json``. Anything else is
refused and left untouched. Nothing is ever deleted, and symlinks are never followed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
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
MODULE: Final = "neptune_context.mcp"
KEPT: Final = frozenset({".claude/settings.local.json"})  # Claude Code's own (an approval)


class Refused(Exception):
    """The folder cannot be written: it holds something this tool did not write."""


def written() -> tuple[frozenset[str], frozenset[str]]:
    """The exact files and directories the tool writes, relative to the folder."""
    skill = sorted(p.relative_to(SKILL) for p in SKILL.rglob("*"))
    files = {".mcp.json", ".claude/settings.json"}
    files |= {f".claude/skills/neptune/{p}" for p in skill if (SKILL / p).is_file()}
    dirs = {".claude", ".claude/skills", ".claude/skills/neptune"}
    dirs |= {f".claude/skills/neptune/{p}" for p in skill if (SKILL / p).is_dir()}
    return frozenset(files), frozenset(dirs)


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
    args += [MODULE, "--memory", str(graph), "--sources", str(sources)]
    return {"mcpServers": {SERVER: {"type": "stdio", "command": command, "args": args}}}


def _ours(document: object) -> bool:
    """Whether ``document`` is a ``.mcp.json`` this tool wrote: one server, ``neptune``, of
    exactly the shape ``mcp_config`` gives, running ``neptune_context.mcp``."""
    if not isinstance(document, dict) or set(document) != {"mcpServers"}:
        return False
    servers = document["mcpServers"]
    if not isinstance(servers, dict) or set(servers) != {SERVER}:
        return False
    server = servers[SERVER]
    if not isinstance(server, dict) or set(server) != {"type", "command", "args"}:
        return False
    args = server["args"]
    return (
        server["type"] == "stdio"
        and isinstance(server["command"], str)
        and isinstance(args, list)
        and len(args) == 11
        and all(isinstance(a, str) for a in args)
        and args[:2] == ["run", "--directory"]
        and args[3:7] == ["--all-packages", "python", "-m", MODULE]
        and args[7] == "--memory"
        and args[9] == "--sources"
    )


def _json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _walk(folder: Path) -> list[Path]:
    """Every entry under ``folder``, sorted, never descending into a symlink."""
    out = []
    for entry in sorted(folder.iterdir()):
        out.append(entry)
        if entry.is_dir() and not entry.is_symlink():
            out += _walk(entry)
    return out


def check(folder: Path) -> None:
    """Refuse ``folder`` unless it is missing, empty or a demo folder this tool wrote: it holds
    only the files and directories the tool writes (and ``KEPT``), no symlink, a ``.mcp.json``
    of the tool's shape and, if present, the tool's ``settings.json``."""
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise Refused(f"{folder} is not a directory")
    if not folder.exists() or not any(folder.iterdir()):
        return
    files, dirs = written()
    foreign = []
    for path in _walk(folder):
        name = path.relative_to(folder).as_posix()
        if path.is_symlink():
            foreign.append(name)
        elif path.is_dir():
            foreign += [] if name in dirs else [name]
        elif not (path.is_file() and (name in files or name in KEPT)):
            foreign.append(name)
    if foreign:
        shown = ", ".join(foreign[:5]) + (", ..." if len(foreign) > 5 else "")
        raise Refused(
            f"{folder} is not empty and not a demo folder (it holds {shown}); nothing was changed"
        )
    mcp = folder / ".mcp.json"
    if not mcp.is_file() or not _ours(_json(mcp)):
        raise Refused(
            f"{folder} is not empty and not a demo folder (it holds no .mcp.json this tool"
            f" wrote: one {SERVER!r} server running {MODULE}); nothing was changed"
        )
    settings = folder / ".claude" / "settings.json"
    if settings.exists() and _json(settings) != SETTINGS:
        raise Refused(
            f"{folder} is not a demo folder ({settings} is not the one this tool writes);"
            " nothing was changed"
        )


def write(folder: Path, demo: Path = DEMO, uv: str | None = None) -> tuple[Path, Path]:
    """Write the demo folder at ``folder`` (see the module docstring); the graph and source index
    it serves. ``Refused`` when ``folder`` holds anything the tool did not write; nothing is
    written then. Only the tool's own files are (over)written; nothing is ever deleted."""
    check(folder)
    graph, sources = served(demo)
    folder.mkdir(parents=True, exist_ok=True)
    # .mcp.json first: a folder an interrupted run leaves is still recognised as this tool's.
    config = mcp_config(graph, sources, uv)
    (folder / ".mcp.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    claude = folder / ".claude"
    skill = claude / "skills" / "neptune"
    skill.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SKILL, skill, symlinks=False, dirs_exist_ok=True)
    (claude / "settings.json").write_text(json.dumps(SETTINGS, indent=2) + "\n", encoding="utf-8")
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
