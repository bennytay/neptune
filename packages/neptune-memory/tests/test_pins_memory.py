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
    version = json.loads((REGISTRY / "v1.0.0" / "version.json").read_text(encoding="utf-8"))
    assert (version["version"], version["owner_version"]) == ("1.0.0", pins.GRAPH_SCHEMA_VERSION)


def test_catalog_pin_is_pending() -> None:
    assert pins.CATALOG_API_VERSION.startswith("pending")


def test_docs_state_the_same_pins() -> None:
    contracts = (ROOT / "docs" / "contracts.md").read_text(encoding="utf-8")
    adr = (ROOT / "docs" / "adr" / "0001-place-in-the-programme-and-contract-pins.md").read_text(
        encoding="utf-8"
    )
    graph_adr = (
        ROOT / "docs" / "adr" / "0006-graph-schema-v1-contract-surface-and-memory-reader.md"
    ).read_text(encoding="utf-8")
    for text in (contracts, adr):
        assert f"SCHEMA_VERSION = {pins.COMPILER_SCHEMA_VERSION}" in text
        assert 'CATALOG_API_VERSION = "pending' in text
    # ADR 0001 recorded the graph pin as 0 before v1; ADR 0006 sets it, and ADRs are not edited.
    for text in (contracts, graph_adr):
        assert f"GRAPH_SCHEMA_VERSION = {pins.GRAPH_SCHEMA_VERSION}" in text
