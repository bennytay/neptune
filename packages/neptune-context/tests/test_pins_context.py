"""Declared contract pins agree with the registry, the upstream owners and the docs (ADR 0001)."""

import json
import tomllib
from pathlib import Path

from neptune_ledger.api import CATALOG_API_VERSION as LEDGER_CATALOG_API_VERSION
from neptune_memory.schema import GRAPH_SCHEMA_VERSION as MEMORY_GRAPH_SCHEMA_VERSION

from neptune_context import pins

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT.parents[1] / "contracts"


def _lock() -> dict[str, str]:
    lock = tomllib.loads((CONTRACTS / "lock.toml").read_text(encoding="utf-8"))
    entry: dict[str, str] = lock["neptune-context"]
    return entry


def test_lock_declares_exactly_the_pins() -> None:
    assert _lock() == {
        "catalog-api": pins.CATALOG_API_VERSION,
        "graph-schema": pins.GRAPH_SCHEMA_VERSION,
    }


def test_each_pin_is_a_published_version_of_its_contract() -> None:
    for contract, pin in (
        ("catalog-api", pins.CATALOG_API_VERSION),
        ("graph-schema", pins.GRAPH_SCHEMA_VERSION),
    ):
        version = json.loads(
            (CONTRACTS / contract / f"v{pin}" / "version.json").read_text(encoding="utf-8")
        )
        assert version["version"] == pin


def test_pins_track_what_the_upstream_owners_export() -> None:
    # A consumer may lag an additive (minor or patch) owner release; the lock check warns until
    # this package moves its pin (platform ADR 0002). It never runs ahead of the owner or across
    # a major.
    pinned = tuple(int(part) for part in pins.CATALOG_API_VERSION.split("."))
    owner = tuple(int(part) for part in LEDGER_CATALOG_API_VERSION.split("."))
    assert pinned[0] == owner[0] and pinned <= owner
    assert pins.GRAPH_SCHEMA_VERSION.split(".")[0] == str(MEMORY_GRAPH_SCHEMA_VERSION)


def test_registry_lists_context_as_a_consumer_of_both() -> None:
    for contract in ("catalog-api", "graph-schema"):
        toml = tomllib.loads((CONTRACTS / contract / "contract.toml").read_text(encoding="utf-8"))
        assert "neptune-context" in toml["consumers"]


def test_docs_state_the_same_pins() -> None:
    # docs/contracts.md is the live mirror of pins.py; the accepted ADR keeps the pins as written.
    contracts = (ROOT / "docs" / "contracts.md").read_text(encoding="utf-8")
    adr = (ROOT / "docs" / "adr" / "0001-place-in-the-programme-and-contract-pins.md").read_text(
        encoding="utf-8"
    )
    for text in (contracts, adr):
        assert f'CATALOG_API_VERSION = "{pins.CATALOG_API_VERSION}"' in text
        assert f'GRAPH_SCHEMA_VERSION = "{pins.GRAPH_SCHEMA_VERSION}"' in text
