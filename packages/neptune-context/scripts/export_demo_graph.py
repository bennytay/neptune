#!/usr/bin/env python3
"""Check Memory's pipeline-built graph of the acceptance corpus and, given a target, copy it there.

    uv run --all-packages python packages/neptune-context/scripts/export_demo_graph.py [OUT.json.gz]
    claude mcp add neptune -- uv run --all-packages python -m neptune_context.mcp \
        --memory OUT.json.gz

The Demo v1 graph is Memory's own snapshot,
``packages/neptune-memory/tests/fixtures/acceptance_corpus.graph.json.gz``: the MCP server reads it
as it is, so no export is needed to serve it. This script reads it with the server's own reader
(Memory's strict codec, the same size cap) so a snapshot the server would refuse is found here, then
copies the bytes unchanged to ``OUT`` (the first argument, else ``$NEPTUNE_MEMORY_GRAPH``). With
neither it only checks and prints the path. Context's tests read a frozen copy of the snapshot
instead (``freeze_demo_graph.py``), so Memory regenerating it never moves them.
"""

# ruff: noqa: T201  (a command-line script: its output is the print)
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parents[1]
SNAPSHOT = (
    ROOT / "packages" / "neptune-memory" / "tests" / "fixtures" / "acceptance_corpus.graph.json.gz"
)

from neptune_context.engine import read_graph_document  # noqa: E402


def main(argv: list[str], snapshot: Path = SNAPSHOT) -> int:
    target = argv[0] if argv else os.environ.get("NEPTUNE_MEMORY_GRAPH")
    try:
        # The server's own reader accepts the snapshot, or this raises.
        document = read_graph_document(snapshot)
        if target:
            out = Path(target)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(snapshot, out)
            read_graph_document(out)
    except (OSError, ValueError) as error:
        print(f"export_demo_graph: {error}", file=sys.stderr)
        return 2
    where = f"copied to {target}" if target else "serve it as it is"
    name = snapshot.relative_to(ROOT) if snapshot.is_relative_to(ROOT) else snapshot
    print(f"{name}: head {int(document.head)}, {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
