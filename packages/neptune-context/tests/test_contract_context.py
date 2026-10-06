"""The ``query-packet`` registry contract (ADR 0006 §1): the owner module's export, its version
constant and the registry's published goldens agree with the code and the package's goldens."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import jsonschema
import pytest

import neptune_context.contract as contract
from neptune.identity.canonical_json import dumps
from neptune_context.packets.model import PACKET_VERSION
from neptune_context.packets.schema import packet_schema
from neptune_context.query.model import QUERY_VERSION
from neptune_context.query.schema import query_schema

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path(__file__).resolve().parents[3]
CONTRACT = REPO / "contracts" / "query-packet"


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


def _registry() -> ModuleType:
    return _module(REPO / "scripts" / "contracts.py", "contracts_registry_tool")


def _latest() -> Path:
    versions = sorted(CONTRACT.glob("v*/version.json"))
    assert versions, "query-packet has a published version"
    return max(
        (p.parent for p in versions),
        key=lambda p: tuple(int(x) for x in p.name.removeprefix("v").split(".")),
    )


def test_the_export_embeds_both_halves_verbatim() -> None:
    schema = contract.contract_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$defs"] == {"ContextPacket": packet_schema(), "Query": query_schema()}


def test_the_registry_major_moves_with_both_halves() -> None:
    assert contract.QUERY_PACKET_VERSION == QUERY_VERSION == PACKET_VERSION == 1


def test_the_published_schema_and_constant_are_the_export() -> None:
    latest = _latest()
    meta = json.loads((latest / "version.json").read_text(encoding="utf-8"))
    assert meta["status"] == "stable" and meta["owner_version"] == contract.QUERY_PACKET_VERSION
    published = json.loads((latest / "schema.json").read_text(encoding="utf-8"))
    assert published == json.loads(dumps(contract.contract_schema()))


def test_the_published_goldens_are_the_packages_goldens() -> None:
    latest = _latest()
    produced = _module(CONTRACT / "goldens.py", "query_packet_goldens").goldens()
    meta = json.loads((latest / "version.json").read_text(encoding="utf-8"))
    assert meta["goldens"] == {name: entry["target"] for name, entry in produced.items()}
    for name, entry in produced.items():
        on_disk = json.loads((latest / "golden" / name).read_text(encoding="utf-8"))
        assert on_disk == entry["value"], f"{name} drifted from the package's golden"


@pytest.mark.parametrize("target", ["#/$defs/Query", "#/$defs/ContextPacket"])
def test_each_half_rejects_the_other_halfs_goldens(target: str) -> None:
    tool, latest = _registry(), _latest()
    schema = json.loads((latest / "schema.json").read_text(encoding="utf-8"))
    meta = json.loads((latest / "version.json").read_text(encoding="utf-8"))
    others = [name for name, pointer in meta["goldens"].items() if pointer != target]
    assert others
    for name in others:
        value = json.loads((latest / "golden" / name).read_text(encoding="utf-8"))
        assert tool.validate_golden(schema, target, value), f"{name} validated as {target}"


def test_published_goldens_read_back_through_the_contracts_own_readers() -> None:
    latest = _latest()
    for path in sorted((latest / "golden").glob("query.*.json")):
        assert isinstance(contract.loads(path.read_bytes()), contract.Query), path.name
    for path in sorted((latest / "golden").glob("packet.*.json")):
        assert contract.check_packet(path.read_bytes()) == (), path.name
        packet = contract.decode_packet(path.read_bytes())
        assert isinstance(packet, contract.ContextPacket)
        query = contract.loads((latest / "golden" / f"query.{path.name[7:]}").read_bytes())
        assert isinstance(query, contract.Query)
        assert contract.answer_problems(query, packet) == ()


def test_the_contract_module_does_not_re_export_the_sdk() -> None:
    # ADR 0006 §6: the SDK is a library over the contract, versioned with the package.
    assert not any(name in contract.__all__ for name in ("Client", "AsyncClient", "StubEngine"))
