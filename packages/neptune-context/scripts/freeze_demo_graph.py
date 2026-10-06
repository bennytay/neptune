#!/usr/bin/env python3
"""Freeze Memory's pipeline-built graph of the acceptance corpus as Context's golden snapshot.

    uv run --all-packages python packages/neptune-context/scripts/freeze_demo_graph.py

Copies ``packages/neptune-memory/tests/fixtures/acceptance_corpus.graph.json.gz`` byte for byte to
``tests/golden/demo-graph-2.0.0.json.gz`` after checking that it decodes with the codec Context
pins (graph-schema 2.0.0). Context's golden and transcript tests read the frozen copy, never
Memory's live file, so Memory regenerating its snapshot does not break Context's CI. Run this when
the demo should move to Memory's newer snapshot, then regenerate the goldens that read it
(``tests/agent_goldens_context.py``) and explain the changes in the PR.
"""

# ruff: noqa: T201  (a command-line script: its output is the print)
from __future__ import annotations

import shutil
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parents[1]
LIVE = (
    ROOT / "packages" / "neptune-memory" / "tests" / "fixtures" / "acceptance_corpus.graph.json.gz"
)
FROZEN = PACKAGE / "tests" / "golden" / "demo-graph-2.0.0.json.gz"
MAX_FROZEN_BYTES = 512 * 1024  # the repository's fixture limit

from neptune_context.engine import read_graph_document  # noqa: E402
from neptune_context.pins import GRAPH_SCHEMA_VERSION  # noqa: E402


def main() -> int:
    document = read_graph_document(LIVE)  # the pinned codec accepts it, or this raises
    if document.to_json()["graph_schema"] != GRAPH_SCHEMA_VERSION:
        print(f"{LIVE.name} is not graph-schema {GRAPH_SCHEMA_VERSION}", file=sys.stderr)
        return 1
    size = LIVE.stat().st_size
    if size > MAX_FROZEN_BYTES:
        print(
            f"{LIVE.name} is {size} bytes; fixtures stay under {MAX_FROZEN_BYTES}", file=sys.stderr
        )
        return 1
    shutil.copyfile(LIVE, FROZEN)
    assert read_graph_document(FROZEN).head == document.head
    print(f"froze {LIVE.relative_to(ROOT)} -> {FROZEN.relative_to(ROOT)} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
