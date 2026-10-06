#!/usr/bin/env python3
"""Write the Demo v1 corpus snapshot as a Memory graph document the MCP server can serve.

    uv run --all-packages python packages/neptune-context/scripts/export_demo_graph.py OUT.json
    claude mcp add neptune -- uv run --all-packages python -m neptune_context.mcp --memory OUT.json

``OUT`` defaults to ``$NEPTUNE_MEMORY_GRAPH``. The snapshot is the one the Context tests read
(``retrieve_fixtures_context.demo_document``: one constant, ``DEMO_SNAPSHOT``), normalised there
into a document Memory's strict codec accepts. When Memory publishes its pipeline-built graph of
the acceptance corpus, serve that file directly and this script retires.
"""

# ruff: noqa: T201  (a command-line script: its output is the print)
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "tests"))

import retrieve_fixtures_context as F  # noqa: E402
from neptune_context.engine import read_graph_document  # noqa: E402


def main(argv: list[str]) -> int:
    target = argv[0] if argv else os.environ.get("NEPTUNE_MEMORY_GRAPH")
    if not target:
        print("usage: export_demo_graph.py OUT.json (or set NEPTUNE_MEMORY_GRAPH)", file=sys.stderr)
        return 2
    out = Path(target)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(F.demo_document().to_json(), sort_keys=True), encoding="utf-8")
    read_graph_document(out)  # the server's own reader accepts it
    print(f"wrote {out} (from {F.DEMO_SNAPSHOT.relative_to(F.ROOT)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
