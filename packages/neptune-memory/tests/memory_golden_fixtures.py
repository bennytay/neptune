"""The graph-schema golden graph for tests: built from the worked examples, and as published.

The worked examples are read by the registry's own generator (``contracts/graph-schema/
goldens.py``), loaded by path, so the tests and the published goldens share one reader.
"""

from __future__ import annotations

import importlib.util
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Final

from neptune_memory.contract.suite import load_golden

if TYPE_CHECKING:
    from types import ModuleType

    from neptune_memory.schema.codec import GraphDocument

REPO: Final = Path(__file__).resolve().parents[3]
REGISTRY: Final = REPO / "contracts" / "graph-schema"
PUBLISHED: Final = REGISTRY / "v1.4.0"  # the latest: what this code must reproduce
FIRST: Final = REGISTRY / "v1.0.0"  # still read by consumers pinned to 1.0.0


@cache
def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("graph_schema_goldens", REGISTRY / "goldens.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def built() -> GraphDocument:
    from neptune_memory.contract.golden import build_golden

    return build_golden(generator().worked_examples())


@cache
def published() -> GraphDocument:
    return load_golden(PUBLISHED / "golden" / "graph.json")
