"""Declared contract pins agree with the compiler and with the docs that state them."""

from pathlib import Path

from neptune.model.record import SCHEMA_VERSION
from neptune_memory import pins

ROOT = Path(__file__).resolve().parents[1]


def test_compiler_pin_tracks_the_compiler() -> None:
    assert pins.COMPILER_SCHEMA_VERSION == SCHEMA_VERSION


def test_graph_schema_is_undefined_until_mvl_105() -> None:
    assert pins.GRAPH_SCHEMA_VERSION == 0


def test_catalog_pin_is_pending() -> None:
    assert pins.CATALOG_API_VERSION.startswith("pending")


def test_docs_state_the_same_pins() -> None:
    contracts = (ROOT / "docs" / "contracts.md").read_text(encoding="utf-8")
    adr = (ROOT / "docs" / "adr" / "0001-place-in-the-programme-and-contract-pins.md").read_text(
        encoding="utf-8"
    )
    for text in (contracts, adr):
        assert f"SCHEMA_VERSION = {pins.COMPILER_SCHEMA_VERSION}" in text
        assert f"GRAPH_SCHEMA_VERSION = {pins.GRAPH_SCHEMA_VERSION}" in text
        assert 'CATALOG_API_VERSION = "pending' in text
