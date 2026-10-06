"""The golden why and diff packets (ADR 0010) stay what the engine builds, and are valid."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

import explain_goldens_context as G
from neptune_context.answer import answer_problems
from neptune_context.packets.codec import canonical_bytes, decode
from neptune_context.packets.conformance import check
from neptune_context.packets.model import ContextPacket
from neptune_context.packets.schema import packet_schema
from neptune_context.query import Query, loads, query_id

NAMES = sorted(G.questions())


def test_the_trail_goldens_on_disk_are_exactly_what_the_engine_builds() -> None:
    built = G.files()
    assert sorted(p.name for p in G.TRAILS.glob("*.json")) == sorted(p.name for p in built)
    for path, data in built.items():
        assert path.read_bytes() == data, f"{path.name} drifted; rerun explain_goldens_context.py"


@pytest.mark.parametrize("name", NAMES)
def test_a_trail_golden_decodes_conforms_and_answers_its_query(name: str) -> None:
    raw = (G.TRAILS / f"packet.{name}.json").read_text(encoding="utf-8")
    packet = decode(raw)
    assert isinstance(packet, ContextPacket) and packet.trails
    assert check(raw.encode("utf-8")) == ()
    query = loads((G.TRAILS / f"query.{name}.json").read_text(encoding="utf-8"))
    assert isinstance(query, Query) and packet.query_id == query_id(query)
    assert answer_problems(query, packet) == ()
    assert json.loads(canonical_bytes(packet)) == json.loads(raw)
    Draft202012Validator(packet_schema()).validate(json.loads(raw))
