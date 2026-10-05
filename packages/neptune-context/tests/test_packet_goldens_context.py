"""The ten worked queries' golden packets (ADR 0003 §10): present, stable, valid, conforming."""

from __future__ import annotations

import dataclasses
import hashlib
import json

import jsonschema
import pytest

from context_packet_goldens import PACKETS, QUERIES, SCHEMA, WORKED, build, files
from neptune.identity.canonical_json import dumps
from neptune_context.packets.codec import canonical_bytes, decode
from neptune_context.packets.conformance import check
from neptune_context.packets.model import ITEM_KINDS, ContextPacket
from neptune_context.packets.schema import packet_schema
from neptune_context.query import Query, loads, query_id
from neptune_context.query import canonical_bytes as query_canonical_bytes
from neptune_context.render.citations import parse_citations, render_text

NAMES = [w.name for w in build()]


def test_there_are_ten_worked_queries_with_one_packet_each() -> None:
    assert len(WORKED) == 10
    assert sorted(p.stem for p in PACKETS.glob("*.json")) == sorted(NAMES)
    assert sorted(p.stem for p in QUERIES.glob("*.json")) == sorted(NAMES)


def test_goldens_on_disk_are_exactly_what_the_generator_builds() -> None:
    # Determinism and drift: rebuilding from the example graph gives the committed bytes.
    for path, data in files().items():
        assert path.read_bytes() == data, f"{path.name} drifted; rerun context_packet_goldens.py"


def test_building_twice_gives_identical_packets() -> None:
    first, second = build(), build()
    assert [canonical_bytes(w.packet) for w in first] == [canonical_bytes(w.packet) for w in second]


def test_the_personas_of_the_issue_are_all_served() -> None:
    personas = {w.persona for w in build()}
    assert {
        "fleet engineer",
        "safety lead",
        "VLA policy at inference",
        "simulator setup",
        "auditor",
    } <= personas


def test_every_item_kind_appears_in_some_golden() -> None:
    kinds = {item.kind for w in build() for item in w.packet.items}
    assert kinds == {cls.kind for cls in ITEM_KINDS}


def test_the_goldens_exercise_inference_supersession_findings_gaps_and_truncation() -> None:
    packets = [w.packet for w in build()]
    assert any(p.inference_included and any(i.is_inferred for i in p.items) for p in packets)
    assert any(p.superseded_since for p in packets)
    assert any(p.findings for p in packets)
    assert any(p.budget.dropped for p in packets)
    assert any(p.during is not None for p in packets)
    assert len({g.code for p in packets for g in p.gaps}) >= 4


@pytest.mark.parametrize("name", NAMES)
def test_golden_conforms(name: str) -> None:
    assert check((PACKETS / f"{name}.json").read_bytes()) == ()


@pytest.mark.parametrize("name", NAMES)
def test_golden_names_its_query_by_the_hash_of_the_query_document(name: str) -> None:
    packet = decode((PACKETS / f"{name}.json").read_bytes())
    assert isinstance(packet, ContextPacket)
    query = json.loads((QUERIES / f"{name}.json").read_bytes())
    assert packet.query_id == "query:sha256:" + hashlib.sha256(dumps(query)).hexdigest()


@pytest.mark.parametrize("name", NAMES)
def test_golden_query_decodes_with_the_query_reader_and_hashes_to_the_packets_query_id(
    name: str,
) -> None:
    # The C1 gate's cross-test (ADR 0003 §9, Consequences): the query documents the packets were
    # built for are queries of ADR 0002, read by its strict reader, re-encoded byte for byte.
    text = (QUERIES / f"{name}.json").read_bytes()
    query = loads(text)
    assert isinstance(query, Query), query
    assert query_canonical_bytes(query) == dumps(json.loads(text))
    packet = decode((PACKETS / f"{name}.json").read_bytes())
    assert isinstance(packet, ContextPacket)
    assert packet.query_id == query_id(query)


@pytest.mark.parametrize("name", NAMES)
def test_rendering_then_parsing_citations_recovers_every_evidence_ref(name: str) -> None:
    packet = decode((PACKETS / f"{name}.json").read_bytes())
    assert isinstance(packet, ContextPacket)
    assert parse_citations(render_text(packet)) == packet.evidence_refs()
    assert packet.evidence_refs()  # no golden answers without evidence


def test_the_schema_snapshot_matches_the_export() -> None:
    assert json.loads(SCHEMA.read_bytes()) == packet_schema()


@pytest.mark.parametrize("name", NAMES)
def test_golden_validates_against_the_json_schema(name: str) -> None:
    validator = jsonschema.Draft202012Validator(packet_schema())
    validator.validate(json.loads((PACKETS / f"{name}.json").read_bytes()))


def test_the_schema_is_a_valid_draft_2020_12_schema() -> None:
    jsonschema.Draft202012Validator.check_schema(packet_schema())


def test_the_schema_rejects_a_packet_with_an_extra_member() -> None:
    validator = jsonschema.Draft202012Validator(packet_schema())
    document = json.loads((PACKETS / f"{NAMES[0]}.json").read_bytes())
    document["items"][0]["note"] = "an opinion"
    assert not validator.is_valid(document)


def test_conformance_survives_packet_text_that_mentions_item_ids() -> None:
    packet = decode((PACKETS / f"{NAMES[3]}.json").read_bytes())
    assert isinstance(packet, ContextPacket) and packet.gaps
    gap = dataclasses.replace(packet.gaps[0], detail=f"see {packet.items[0].id} (dup)")
    tricky = dataclasses.replace(packet, gaps=(gap, *packet.gaps[1:]))
    assert check(canonical_bytes(tricky)) == ()
