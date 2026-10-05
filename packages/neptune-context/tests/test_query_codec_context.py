"""Canonical JSON and query id (ADR 0002 §6): one encoding per meaning, stable on every machine."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from fractions import Fraction
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from neptune_context.query import (
    QUERY_ID_PATTERN,
    Box,
    Budget,
    CivilTime,
    DomainClock,
    During,
    FrameRef,
    FrameRegion,
    Query,
    Sphere,
    Subject,
    TextChannel,
    TextClause,
    TextField,
    accept,
    canonical_bytes,
    loads,
    query_id,
    to_json,
)

GRAPH = "rec:sha256:" + "a" * 64
DOMAIN = "rec:sha256:" + "b" * 64
BASE = Query(
    include_inferred=False,
    budget=Budget(items=10),
    subjects=frozenset({Subject("machine", "asset_tag:a1")}),
)
# Pinned: a change here changes every stored packet's query key (ADR 0002 §9).
BASE_BYTES = (
    b'{"as_of":"head","budget":{"items":10},"clock_bridges":[],"explain":[],"frame_bridges":[],'
    b'"include_inferred":false,"query_version":1,"regions":[],"subjects":[{"declared_id":'
    b'"asset_tag:a1","kind":"machine","same_as_depth":0}]}'
)


def test_base_query_bytes_and_id_are_pinned() -> None:
    assert canonical_bytes(BASE) == BASE_BYTES
    assert query_id(BASE) == (
        "query:sha256:19317783bd5e8243cb04615c20dcbe3ce0a066e0bc3d0ec0a131d50fbf7bb4b8"
    )
    assert query_id(BASE) == "query:sha256:" + hashlib.sha256(BASE_BYTES).hexdigest()
    assert re.fullmatch(QUERY_ID_PATTERN, query_id(BASE))


def test_absent_single_clauses_are_omitted_and_lists_present() -> None:
    data = to_json(BASE)
    assert {"during", "graph", "site", "text"}.isdisjoint(data)
    assert data["regions"] == [] and data["explain"] == []
    assert "null" not in canonical_bytes(BASE).decode()


def test_set_order_never_changes_the_bytes() -> None:
    kinds = ["machine", "site", "run", "asset", "sensor", "zone", "fleet", "task"]
    forward = replace(BASE, subjects=frozenset(Subject(k) for k in kinds))
    backward = replace(BASE, subjects=frozenset(Subject(k) for k in reversed(kinds)))
    assert canonical_bytes(forward) == canonical_bytes(backward)
    data: Any = json.loads(canonical_bytes(forward))
    names = [s["kind"] for s in data["subjects"]]
    assert names == sorted(names)


def test_negative_zero_and_int_coordinates_encode_like_their_float_equal() -> None:
    frame = FrameRef("base_link", GRAPH)
    a = replace(BASE, regions=frozenset({FrameRegion(frame, "m", Box((-0.0, 0, 0), (1, 1, 1)))}))
    b = replace(
        BASE, regions=frozenset({FrameRegion(frame, "m", Box((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)))})
    )
    assert a == b
    assert canonical_bytes(a) == canonical_bytes(b)
    assert b'"min":[0.0,0.0,0.0]' in canonical_bytes(a)


def test_every_member_changes_the_id() -> None:
    variants = [
        replace(BASE, include_inferred=True),
        replace(BASE, as_of=7),
        replace(BASE, budget=Budget(items=11)),
        replace(BASE, budget=Budget(items=10, tokens=5)),
        replace(BASE, subjects=frozenset({Subject("machine", "asset_tag:a1", 1)})),
        replace(BASE, during=During(DomainClock(DOMAIN), 0, None)),
    ]
    ids = {query_id(v) for v in [BASE, *variants]}
    assert len(ids) == len(variants) + 1


def test_encoding_is_repeatable() -> None:
    assert canonical_bytes(BASE) == canonical_bytes(replace(BASE))
    assert query_id(BASE) == query_id(replace(BASE))


# --- Property: decoding the canonical bytes is the same as accepting the query -----------------

_ids = st.sampled_from(["asset_tag:a1", "fleet_registry:north", "vin:x9", "Bad Id", "ns: padded "])
_kinds = st.sampled_from(["machine", "site", "run", "fleet", "zone", "robot"])
_subjects = st.builds(
    Subject, _kinds, st.one_of(st.none(), _ids), st.integers(min_value=-1, max_value=4)
)
_clocks = st.one_of(
    st.builds(DomainClock, st.sampled_from([DOMAIN, "rec:sha256:bad"])),
    st.builds(
        CivilTime,
        st.sampled_from(["utc", "tai", "local"]),
        st.sampled_from(["unix", "gps", "boot"]),
        st.fractions(min_value=Fraction(1, 10**9), max_value=10, max_denominator=10**9),
    ),
)
_ticks = st.integers(min_value=-(2**64), max_value=2**64)
_during = st.builds(During, _clocks, _ticks, st.one_of(st.none(), _ticks))
_coord = st.floats(allow_nan=False, allow_infinity=False, width=64)
_vec = st.tuples(_coord, _coord, _coord)
_shape = st.one_of(st.builds(Box, _vec, _vec), st.builds(Sphere, _vec, _coord))
_region = st.builds(
    FrameRegion,
    st.builds(FrameRef, st.sampled_from(["base_link", "map"]), st.just(GRAPH)),
    st.sampled_from(["m", "mm", "kg"]),
    _shape,
)
_text = st.builds(
    TextClause,
    st.text(max_size=40),
    st.frozensets(st.sampled_from(list(TextField))),
    st.frozensets(st.sampled_from(list(TextChannel))),
)
_queries = st.builds(
    Query,
    include_inferred=st.booleans(),
    budget=st.builds(
        Budget,
        st.integers(min_value=0, max_value=20_000),
        st.one_of(st.none(), st.integers(min_value=0, max_value=10)),
    ),
    subjects=st.frozensets(_subjects, max_size=4),
    as_of=st.one_of(st.just("head"), st.integers(min_value=-2, max_value=2**64)),
    during=st.one_of(st.none(), _during),
    regions=st.frozensets(_region, max_size=3),
    text=st.one_of(st.none(), _text),
)


@settings(max_examples=300, deadline=None)
@given(_queries)
def test_decoding_canonical_bytes_equals_accepting(query: Query) -> None:
    result = accept(query)
    try:
        data = canonical_bytes(query)
    except ValueError:  # a lone surrogate in generated text: validate refuses it at "/"
        assert not isinstance(result, Query)
        return
    assert canonical_bytes(query) == data
    assert loads(data) == result
