"""Reading hostile query JSON (ADR 0002 §6-7): every failure is a finding, never an exception."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from neptune_context.query import FindingCode, Query, Refused, from_json, loads
from neptune_context.query.model import MAX_DOCUMENT_BYTES

GOLDEN = Path(__file__).resolve().parent / "golden" / "worked-queries"
DOMAIN = "rec:sha256:" + "b" * 64
GRAPH = "rec:sha256:" + "a" * 64


def _base() -> dict[str, Any]:
    return {
        "as_of": "head",
        "budget": {"items": 10},
        "clock_bridges": [],
        "explain": [],
        "frame_bridges": [],
        "include_inferred": False,
        "query_version": 1,
        "regions": [],
        "subjects": [{"declared_id": "asset_tag:a1", "kind": "machine", "same_as_depth": 0}],
    }


def _codes(result: Query | Refused) -> list[tuple[str, str]]:
    assert isinstance(result, Refused), result
    return [(str(f.code), f.at) for f in result.findings]


def test_the_base_document_reads() -> None:
    assert isinstance(loads(json.dumps(_base())), Query)


@pytest.mark.parametrize(
    "document",
    [
        b"",
        b"{",
        b"\xff\xfe{}",
        b'{"a": 1, "a": 2}',
        b'{"as_of": NaN}',
        b'{"as_of": Infinity}',
        b'{"as_of": -Infinity}',
        b"[" * 20_000 + b"]" * 20_000,
        b"1" * 5000,  # beyond Python's int digit limit
    ],
    ids=["empty", "truncated", "not-utf8", "dup-key", "nan", "inf", "neg-inf", "deep", "huge-int"],
)
def test_unreadable_documents_are_syntax_findings(document: bytes) -> None:
    assert _codes(loads(document)) == [("syntax", "/")]


def test_size_limit_boundary() -> None:
    text = json.dumps(_base())
    at_limit = text + " " * (MAX_DOCUMENT_BYTES - len(text))
    assert isinstance(loads(at_limit), Query)
    assert _codes(loads(at_limit + " ")) == [("too_large", "/")]


def _mutated(path: list[str | int], value: Any) -> dict[str, Any]:
    doc = _base()
    target: Any = doc
    for key in path[:-1]:
        target = target[key]
    if value is _DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return doc


_DELETE = object()
CIVIL = {
    "epoch": "unix",
    "kind": "civil",
    "resolution": {"denominator": 1000, "numerator": 1},
    "timescale": "utc",
}


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (["budget"], _DELETE, [("shape", "/budget")]),
        (["surprise"], 1, [("shape", "/surprise")]),
        (["as_of"], "yesterday", [("shape", "/as_of")]),
        (["as_of"], 1.5, [("shape", "/as_of")]),
        (["include_inferred"], 0, [("shape", "/include_inferred")]),
        (["budget", "items"], True, [("shape", "/budget/items")]),
        (["budget", "tokens"], "many", [("shape", "/budget/tokens")]),
        (["subjects"], {}, [("shape", "/subjects")]),
        (["subjects", 0, "kind"], 3, [("shape", "/subjects/0/kind")]),
        (["subjects", 0, "colour"], "red", [("shape", "/subjects/0/colour")]),
        (["subjects", 0, "declared_id"], "ns:\ud800", [("shape", "/subjects/0/declared_id")]),
        (["query_version"], 2, [("unsupported_version", "/query_version")]),
        (["query_version"], 1.0, [("unsupported_version", "/query_version")]),
        (["query_version"], True, [("unsupported_version", "/query_version")]),
        (
            ["subjects"],
            [{"kind": "site", "same_as_depth": 0}, {"kind": "site", "same_as_depth": 0}],
            [("duplicate", "/subjects/1")],
        ),
        (
            ["during"],
            {"clock": {"kind": "wall"}, "end": "open", "start": 0},
            [("shape", "/during/clock")],
        ),
        (
            ["during"],
            {
                "clock": {**CIVIL, "resolution": {"denominator": 2000, "numerator": 2}},
                "end": "open",
                "start": 0,
            },
            [("bad_clock", "/during/clock/resolution")],
        ),
        (
            ["during"],
            {
                "clock": {**CIVIL, "resolution": {"denominator": 0, "numerator": 1}},
                "end": "open",
                "start": 0,
            },
            [("bad_clock", "/during/clock/resolution")],
        ),
        (
            ["during"],
            {"clock": {"domain_id": DOMAIN, "kind": "domain"}, "end": "later", "start": 0},
            [("shape", "/during/end")],
        ),
        (
            ["regions"],
            [
                {
                    "frame": {"frame_id": "map", "graph_id": GRAPH},
                    "shape": {"kind": "box", "max": [1, 1], "min": [0, 0, 0]},
                    "unit": "m",
                }
            ],
            [("shape", "/regions/0/shape/max")],
        ),
        (
            ["graph"],
            {"direction": "up", "hops": 1, "predicates": "any"},
            [("shape", "/graph/direction")],
        ),
        (
            ["text"],
            {"channels": ["lexical", "lexical"], "fields": ["record"], "text": "x"},
            [("duplicate", "/text/channels/1")],
        ),
        (["explain"], [{"kind": "how"}], [("shape", "/explain/0")]),
    ],
)
def test_shape_findings_point_at_the_member(
    path: list[str | int], value: Any, expected: list[tuple[str, str]]
) -> None:
    assert _codes(loads(json.dumps(_mutated(path, value)))) == expected


def test_an_overflowing_coordinate_is_a_region_finding() -> None:
    region = {
        "frame": {"frame_id": "map", "graph_id": GRAPH},
        "shape": {"kind": "box", "max": [1, 1, 9], "min": [0, 0, 0]},
        "unit": "m",
    }
    text = json.dumps(_mutated(["regions"], [region])).replace("9]", "1e400]")
    assert _codes(loads(text)) == [("bad_region", "/regions/0/shape/max/2")]


def test_a_top_level_non_object_is_refused() -> None:
    assert _codes(from_json([1, 2])) == [("shape", "/")]


def test_shape_findings_are_all_reported_at_once() -> None:
    doc = _base()
    doc["as_of"] = "soon"
    doc["include_inferred"] = "yes"
    assert [at for _, at in _codes(from_json(doc))] == ["/as_of", "/include_inferred"]


def test_a_valid_shape_still_runs_validation() -> None:
    doc = _mutated(["subjects", 0, "kind"], "robot")
    assert _codes(from_json(doc)) == [("unknown_kind", "/subjects/0/kind")]


@pytest.mark.parametrize("golden", sorted(GOLDEN.glob("q*.json")), ids=lambda p: p.stem)
def test_every_truncation_of_a_golden_is_refused_without_raising(golden: Path) -> None:
    data = golden.read_bytes().rstrip(b"\n")
    for end in range(len(data)):
        result = loads(data[:end])
        assert isinstance(result, Refused)
        assert result.findings and result.findings[0].code is FindingCode.SYNTAX


_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=20,
)


@settings(max_examples=300, deadline=None)
@given(st.fixed_dictionaries({}, optional={k: _json for k in _base()}) | _json)
def test_arbitrary_json_never_raises(value: Any) -> None:
    assert isinstance(from_json(value), Query | Refused)


@settings(max_examples=300, deadline=None)
@given(st.binary(max_size=256))
def test_arbitrary_bytes_never_raise(data: bytes) -> None:
    assert isinstance(loads(data), Query | Refused)


def test_reading_is_deterministic() -> None:
    doc = _mutated(["as_of"], "soon")
    assert loads(json.dumps(doc)) == loads(json.dumps(doc))
