"""The ``clock_map`` literal: its invariants, its JSON and its JSON Schema (ADR 0011 §2)."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from memory_time_records import (
    STATED,
    TRANSFORM,
    at,
    build,
    cite,
    claims,
    clock,
    drone_flight,
    mapping_id,
    two_sites,
)
from neptune.identity import canonical_json
from neptune.model.alignment import ClockAnchor
from neptune.model.knowledge import (
    Ambiguous,
    Candidate,
    Known,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import Provenance
from neptune.model.time import Duration
from neptune_memory.schema.claim import TypedLiteral, ValueType
from neptune_memory.schema.clock_map import ClockMap, MapMethod, clock_map_from_json
from neptune_memory.schema.codec import claim_from_json
from neptune_memory.schema.export import graph_schema

GPS, BOOT = clock("px4 gps"), clock("px4 boot")
ANCHOR = ClockAnchor(at("px4 boot", 20), at("px4 gps", 7))
NA = NotApplicable()


def direct(**changes: Any) -> ClockMap:
    fields: dict[str, Any] = {
        "target": GPS,
        "method": MapMethod.STATED,
        "anchor": Known(ANCHOR),
        "rate": Known(Fraction(1, 1000)),
        "residual_bound": Known(Duration(2, GPS)),
        **changes,
    }
    return ClockMap(**fields)


def composed(**changes: Any) -> ClockMap:
    fields: dict[str, Any] = {
        "target": GPS,
        "method": MapMethod.COMPOSED,
        "anchor": NA,
        "rate": NA,
        "residual_bound": NA,
        "chain": (mapping_id("a"), mapping_id("b")),
        "via": (clock("dock"),),
        **changes,
    }
    return ClockMap(**fields)


def test_a_direct_map_is_affine_exactly() -> None:
    assert direct().affine() == (Fraction(1, 1000), Fraction(7) - Fraction(20, 1000))
    assert direct(rate=Unknown()).affine() is None
    assert direct(anchor=NotCovered()).affine() is None
    assert composed().affine() is None


@pytest.mark.parametrize(
    "value",
    [
        direct(),
        direct(rate=Unknown(), residual_bound=NotCovered()),
        direct(rate=Ambiguous((Candidate(Fraction(1)), Candidate(Fraction(2))))),
        direct(method=MapMethod.CO_SAMPLED, anchor=NA),
        composed(),
    ],
    ids=["stated", "unknowns", "ambiguous", "co-sampled", "composed"],
)
def test_json_round_trips_exactly(value: ClockMap) -> None:
    encoded = canonical_json.loads(canonical_json.dumps(value.to_json()))
    assert clock_map_from_json(encoded) == value


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"rate": Known(Fraction(0))}, "positive"),
        ({"rate": Known(Fraction(-1, 2))}, "positive"),
        ({"residual_bound": Known(Duration(-1, GPS))}, "non-negative"),
        ({"residual_bound": Known(Duration(1, BOOT))}, "target clock"),
        ({"anchor": Known(ClockAnchor(at("px4 boot", 0), at("px4 boot", 0)))}, "target"),
        ({"chain": (mapping_id("a"), mapping_id("b"))}, "only a composed"),
        ({"rate": Known(Fraction(1), Provenance(cite("sync"), TRANSFORM.id, STATED))}, "INHERITED"),
        ({"method": "stated"}, "MapMethod"),
        ({"target": "not an id"}, "record id"),
    ],
)
def test_a_direct_map_refuses_what_no_mapping_states(changes: dict[str, Any], message: str) -> None:
    with pytest.raises((ValueError, TypeError), match=message):
        direct(**changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"rate": Known(Fraction(1))}, "NotApplicable"),
        ({"via": ()}, "two hops"),
        ({"chain": (mapping_id("a"),)}, "two hops"),
        ({"via": (GPS,)}, "twice"),
        ({"chain": (mapping_id("a"), mapping_id("a"))}, "twice"),
    ],
)
def test_a_composed_map_names_its_chain_and_carries_no_parameters(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        composed(**changes)


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {**direct().to_json(), "offset": 3},
        {key: value for key, value in direct().to_json().items() if key != "via"},
        {**direct().to_json(), "rate": {"knowledge": "known", "value": 1}},
        {**direct().to_json(), "rate": {"knowledge": "known_absent"}},
        {**direct().to_json(), "chain": "rec:sha256:" + "0" * 64},
        {**direct().to_json(), "method": "fitted"},
        {
            **direct().to_json(),
            "rate": {
                "knowledge": "unknown",
                "provenance": {"assertion_kind": "stated"},
            },
        },
    ],
    ids=[
        "null",
        "array",
        "extra-key",
        "missing-key",
        "rate-not-a-fraction",
        "absent-without-provenance",
        "chain-not-an-array",
        "unknown-method",
        "own-provenance",
    ],
)
def test_malformed_json_is_refused(data: object) -> None:
    with pytest.raises((ValueError, TypeError, KeyError)):
        clock_map_from_json(data)  # type: ignore[arg-type]


def test_only_a_clock_map_literal_holds_a_clock_map() -> None:
    TypedLiteral(ValueType.CLOCK_MAP, direct())
    with pytest.raises(TypeError):
        TypedLiteral(ValueType.CLOCK_MAP, "offset 7")
    with pytest.raises(TypeError):
        TypedLiteral(ValueType.TEXT, direct())  # type: ignore[arg-type]


def test_registry_claims_round_trip_and_validate_against_the_published_schema() -> None:
    schema = graph_schema()
    validator = Draft202012Validator({**schema, "anyOf": [{"$ref": "#/$defs/Claim"}]})
    every = [*claims(build({"flight-17": drone_flight()})), *claims(build(two_sites()))]
    assert {c.predicate for c in every} == {"has_clock", "maps_to", "clock_map"}
    for claim in every:
        data = canonical_json.loads(canonical_json.dumps(claim.to_json()))
        validator.validate(data)
        assert claim_from_json(data) == claim
