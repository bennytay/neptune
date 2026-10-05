"""Declared contract pins agree with the compiler, the registry and the docs that state them."""

import json
import tomllib
from pathlib import Path

from neptune.model.record import SCHEMA_VERSION
from neptune_memory import pins
from neptune_memory.schema import GRAPH_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT.parents[1] / "contracts" / "graph-schema"


def test_compiler_pin_tracks_the_compiler() -> None:
    assert pins.COMPILER_SCHEMA_VERSION == SCHEMA_VERSION


def test_graph_schema_v1_is_published_and_active() -> None:
    assert pins.GRAPH_SCHEMA_VERSION == GRAPH_SCHEMA_VERSION == 1
    contract = tomllib.loads((REGISTRY / "contract.toml").read_text(encoding="utf-8"))
    assert contract["status"] == "active"
    assert contract["owner"]["version_constant"] == "neptune_memory.schema:GRAPH_SCHEMA_VERSION"
    # 1.1.0: has_name, stream and document (ADR 0008); 1.3.0: run thread predicates (ADR 0009;
    # 1.2.0 is MVL-127's configuration lineage, published on its own branch); 1.6.0: events
    # (ADR 0013; 1.4.0 and 1.5.0 are MVL-130's and MVL-133's)
    for published in ("1.0.0", "1.1.0", "1.3.0", "1.6.0"):
        version = json.loads(
            (REGISTRY / f"v{published}" / "version.json").read_text(encoding="utf-8")
        )
        assert (version["version"], version["owner_version"]) == (
            published,
            pins.GRAPH_SCHEMA_VERSION,
        )


def test_catalog_pin_is_pending() -> None:
    assert pins.CATALOG_API_VERSION.startswith("pending")


def test_docs_state_the_same_pins() -> None:
    # docs/contracts.md is the live mirror of pins.py; accepted ADRs keep the pins as written.
    contracts = (ROOT / "docs" / "contracts.md").read_text(encoding="utf-8")
    graph_adr = (
        ROOT / "docs" / "adr" / "0006-graph-schema-v1-contract-surface-and-memory-reader.md"
    ).read_text(encoding="utf-8")
    assert f"SCHEMA_VERSION = {pins.COMPILER_SCHEMA_VERSION}" in contracts
    assert 'CATALOG_API_VERSION = "pending' in contracts
    # ADR 0001 recorded the graph pin as 0 before v1; ADR 0006 sets it, and ADRs are not edited.
    for text in (contracts, graph_adr):
        assert f"GRAPH_SCHEMA_VERSION = {pins.GRAPH_SCHEMA_VERSION}" in text


def test_graph_schema_doc_lists_every_node_predicate_and_finding_code() -> None:
    from neptune_memory.schema.nodes import NodeType
    from neptune_memory.schema.predicates import CORE_PREDICATES, VOCABULARY_VERSION
    from neptune_memory.schema.supersede import FindingCode

    doc = (ROOT / "docs" / "graph-schema.md").read_text(encoding="utf-8")
    names = [*map(str, NodeType), *(s.name for s in CORE_PREDICATES.specs), *map(str, FindingCode)]
    assert [n for n in names if f"`{n}`" not in doc] == []
    assert f"GRAPH_SCHEMA_VERSION = {pins.GRAPH_SCHEMA_VERSION}" in doc
    assert f"VOCABULARY_VERSION = {VOCABULARY_VERSION}" in doc
