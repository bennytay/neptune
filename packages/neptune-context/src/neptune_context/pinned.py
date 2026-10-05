"""Upstream vocabularies and definitions at the pinned contract versions (ADR 0006 §9).

What Context publishes depends on upstream vocabularies: the query schema's subject-kind and
predicate enums, the Memory definitions the packet schema embeds, and the planner's prompt. They
come from the registry versions declared in ``pins.py``, never from the owners' live code, so an
upstream release changes nothing Context publishes until Context bumps a pin, and that bump is a
reviewed Context change that regenerates this snapshot and Context's own exports with it.

``pinned.json`` is the snapshot: graph-schema's ``$defs`` and predicate names, and catalog-api's
thread kinds, copied from ``contracts/<contract>/v<pin>/``. It ships inside the package so a
reader never needs the repository's registry. Regenerate after a pin bump with
``uv run python -m neptune_context.pinned contracts
packages/neptune-context/src/neptune_context/pinned.json``; a test fails when it is stale.
"""

from __future__ import annotations

import copy
import functools
import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neptune_context.pins import CATALOG_API_VERSION, GRAPH_SCHEMA_VERSION

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue


def build(contracts: Path) -> bytes:
    """The snapshot's bytes, read from the registry at ``contracts`` at the pinned versions."""

    def read(contract: str, version: str, name: str) -> Any:
        return json.loads((contracts / contract / f"v{version}" / name).read_bytes())

    graph = read("graph-schema", GRAPH_SCHEMA_VERSION, "schema.json")
    vocabulary = read("graph-schema", GRAPH_SCHEMA_VERSION, "golden/vocabulary.json")
    catalog = read("catalog-api", CATALOG_API_VERSION, "schema.json")
    snapshot = {
        "catalog-api": {
            "thread_kinds": sorted(catalog["$defs"]["ThreadKey"]["properties"]["kind"]["enum"]),
            "version": CATALOG_API_VERSION,
        },
        "graph-schema": {
            "defs": graph["$defs"],
            "predicates": sorted(p["name"] for p in vocabulary["predicates"]),
            "version": GRAPH_SCHEMA_VERSION,
        },
    }
    text = json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


@functools.cache
def _snapshot() -> Any:
    return json.loads(files("neptune_context").joinpath("pinned.json").read_bytes())


def node_types() -> frozenset[str]:
    """graph-schema's node types at the pin."""
    return frozenset(_snapshot()["graph-schema"]["defs"]["NodeType"]["enum"])


def predicates() -> frozenset[str]:
    """graph-schema's predicate names at the pin."""
    return frozenset(_snapshot()["graph-schema"]["predicates"])


def thread_kinds() -> frozenset[str]:
    """catalog-api's thread kinds at the pin."""
    return frozenset(_snapshot()["catalog-api"]["thread_kinds"])


def graph_schema_defs() -> dict[str, JsonValue]:
    """A fresh copy of graph-schema's ``$defs`` at the pin (callers may not mutate the snapshot)."""
    defs: dict[str, JsonValue] = copy.deepcopy(_snapshot()["graph-schema"]["defs"])
    return defs


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: python -m neptune_context.pinned <contracts-dir> <output.json>\n")
        return 2
    Path(argv[1]).write_bytes(build(Path(argv[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
