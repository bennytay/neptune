"""Refusing queries that could silently mix clocks, frames or units (ADR 0002 §3-5, §7-8)."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from typing import Any

import pytest

from neptune_context.query import (
    Box,
    Budget,
    Caller,
    CivilTime,
    ClockBridge,
    Diff,
    Direction,
    DomainClock,
    During,
    FindingCode,
    FrameBridge,
    FrameRef,
    FrameRegion,
    GraphClause,
    Instant,
    Query,
    SiteScope,
    Sphere,
    Subject,
    TextChannel,
    TextClause,
    TextField,
    Why,
    accept,
    default_include_inferred,
    query_id,
    validate,
)
from neptune_context.query.model import (
    INT64_MAX,
    INT64_MIN,
    MAX_BRIDGES,
    MAX_BYTES,
    MAX_EXPLAIN,
    MAX_HOPS,
    MAX_ITEMS,
    MAX_LATENCY_MS,
    MAX_REGIONS,
    MAX_SAME_AS_DEPTH,
    MAX_SUBJECTS,
    MAX_TEXT_CHARS,
    MAX_TOKENS,
    MAX_ZONES,
)


def _rec(n: int) -> str:
    return f"rec:sha256:{n:064x}"


ARM = Subject("machine", "asset_tag:arm-1")
BASE = Query(include_inferred=False, budget=Budget(items=10), subjects=frozenset({ARM}))
UTC_NS = CivilTime("utc", "unix", Fraction(1, 10**9))
DEVICE = DomainClock(_rec(1))
OTHER_DEVICE = DomainClock(_rec(2))
MAP, ODOM, BASE_LINK = (FrameRef(name, _rec(9)) for name in ("map", "odom", "base_link"))
UNIT_BOX = Box((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
TEXT = TextClause("gripper slip", frozenset({TextField.RECORD}), frozenset({TextChannel.LEXICAL}))
CLAIM = "claim:sha256:" + "c" * 64


def _codes(query: Query) -> list[tuple[str, str]]:
    return [(str(f.code), f.at) for f in validate(query)]


def _region(frame: FrameRef, unit: str = "m", shape: Box | Sphere = UNIT_BOX) -> FrameRegion:
    return FrameRegion(frame, unit, shape)


REFUSALS: list[tuple[str, Query, list[tuple[str, str]]]] = [
    ("empty-query", replace(BASE, subjects=frozenset()), [("empty_query", "/")]),
    (
        "unknown-kind",
        replace(BASE, subjects=frozenset({Subject("robot")})),
        [("unknown_kind", "/subjects/0/kind")],
    ),
    (
        "bad-declared-id",
        replace(BASE, subjects=frozenset({Subject("machine", "Asset Tag:1")})),
        [("bad_identifier", "/subjects/0/declared_id")],
    ),
    (
        "padded-declared-id",
        replace(BASE, subjects=frozenset({Subject("machine", "asset_tag: arm-1")})),
        [("bad_identifier", "/subjects/0/declared_id")],
    ),
    (
        "same-as-kind-wide",
        replace(BASE, subjects=frozenset({Subject("machine", None, 1)})),
        [("same_as_without_id", "/subjects/0/same_as_depth")],
    ),
    ("as-of-negative", replace(BASE, as_of=-1), [("out_of_range", "/as_of")]),
    (
        "naive-local-time",
        replace(BASE, during=During(CivilTime("local", "unix", Fraction(1)), 0, 10)),
        [("bad_clock", "/during/clock")],
    ),
    (
        "relative-epoch",
        replace(BASE, during=During(CivilTime("utc", "boot", Fraction(1)), 0, 10)),
        [("bad_clock", "/during/clock")],
    ),
    (
        "resolution-beyond-int64",
        replace(BASE, during=During(CivilTime("utc", "unix", Fraction(1, 2**63)), 0, 10)),
        [("bad_clock", "/during/clock")],
    ),
    (
        "bad-domain-id",
        replace(BASE, during=During(DomainClock("tsd:1"), 0, 10)),
        [("bad_identifier", "/during/clock/domain_id")],
    ),
    ("empty-interval", replace(BASE, during=During(DEVICE, 5, 5)), [("bad_interval", "/during")]),
    (
        "reversed-interval",
        replace(BASE, during=During(DEVICE, 6, 5)),
        [("bad_interval", "/during")],
    ),
    (
        "unbridged-clock-bridge",
        replace(
            BASE,
            during=During(DEVICE, 0, 10),
            clock_bridges=frozenset({ClockBridge(_rec(3), OTHER_DEVICE, UTC_NS)}),
        ),
        [("dangling_clock_bridge", "/clock_bridges/0")],
    ),
    (
        "bridge-to-itself",
        replace(
            BASE,
            during=During(DEVICE, 0, 10),
            clock_bridges=frozenset({ClockBridge(_rec(3), DEVICE, DEVICE)}),
        ),
        [("bad_clock", "/clock_bridges/0")],
    ),
    (
        "cross-clock-diff",
        replace(
            BASE,
            explain=(Diff(ARM, Instant(UTC_NS, 1), Instant(DEVICE, 2)),),
        ),
        [("cross_clock_without_mapping", "/explain/0/after/clock")],
    ),
    (
        "diff-mixed-axes",
        replace(BASE, explain=(Diff(ARM, 3, Instant(DEVICE, 2)),)),
        [("diff_mixed_axes", "/explain/0")],
    ),
    (
        "diff-not-ordered",
        replace(BASE, explain=(Diff(ARM, 5, 5),)),
        [("diff_not_ordered", "/explain/0")],
    ),
    (
        "diff-instants-not-ordered",
        replace(BASE, explain=(Diff(ARM, Instant(DEVICE, 9), Instant(DEVICE, 2)),)),
        [("diff_not_ordered", "/explain/0")],
    ),
    (
        "diff-beyond-as-of",
        replace(BASE, as_of=10, explain=(Diff(ARM, 5, 11),)),
        [("diff_beyond_as_of", "/explain/0/after")],
    ),
    (
        "diff-kind-wide",
        replace(BASE, explain=(Diff(Subject("machine"), 1, 2),)),
        [("bad_identifier", "/explain/0/subject")],
    ),
    (
        "bad-claim-id",
        replace(BASE, explain=(Why("claim:1"),)),
        [("bad_identifier", "/explain/0/claim_id")],
    ),
    (
        "repeated-explain",
        replace(BASE, explain=(Why(CLAIM), Why(CLAIM))),
        [("duplicate", "/explain/1")],
    ),
    (
        "cross-frame",
        replace(BASE, regions=frozenset({_region(MAP), _region(ODOM)})),
        [("cross_frame_without_transform", "/regions/1/frame")],
    ),
    (
        "mixed-units",
        replace(
            BASE, regions=frozenset({_region(MAP, "m"), _region(MAP, "mm", Sphere((0, 0, 0), 1))})
        ),
        [("mixed_region_units", "/regions/1/unit")],
    ),
    (
        "not-a-length",
        replace(BASE, regions=frozenset({_region(MAP, "kg")})),
        [("bad_unit", "/regions/0/unit")],
    ),
    (
        "not-catalogued",
        replace(BASE, regions=frozenset({_region(MAP, "metre")})),
        [("bad_unit", "/regions/0/unit")],
    ),
    (
        "flat-box",
        replace(BASE, regions=frozenset({_region(MAP, shape=Box((0, 0, 0), (1, 0, 1)))})),
        [("bad_region", "/regions/0/shape")],
    ),
    (
        "zero-radius",
        replace(BASE, regions=frozenset({_region(MAP, shape=Sphere((0, 0, 0), 0.0))})),
        [("bad_region", "/regions/0/shape")],
    ),
    (
        "bool-coordinate",  # a bool is an int to the type check; its value is refused
        replace(BASE, regions=frozenset({_region(MAP, shape=Sphere((True, 0, 0), 1))})),
        [("bad_region", "/regions/0/shape")],
    ),
    (
        "empty-frame-id",
        replace(BASE, regions=frozenset({_region(FrameRef("", _rec(9)))})),
        [("bad_identifier", "/regions/0/frame/frame_id")],
    ),
    (
        "dangling-frame-bridge",
        replace(
            BASE,
            regions=frozenset({_region(MAP)}),
            frame_bridges=frozenset({FrameBridge(_rec(4), ODOM, BASE_LINK)}),
        ),
        [("dangling_frame_bridge", "/frame_bridges/0")],
    ),
    (
        "frame-bridge-to-itself",
        replace(
            BASE,
            regions=frozenset({_region(MAP)}),
            frame_bridges=frozenset({FrameBridge(_rec(4), MAP, MAP)}),
        ),
        [("bad_region", "/frame_bridges/0")],
    ),
    (
        "bad-site",
        replace(BASE, site=SiteScope("plant 7", frozenset({"zone_map:a"}))),
        [("bad_identifier", "/site/site")],
    ),
    (
        "unanchored-graph",
        replace(
            BASE,
            subjects=frozenset({Subject("fleet")}),
            graph=GraphClause(None, 1, Direction.OUT),
        ),
        [("graph_without_anchor", "/graph")],
    ),
    (
        "unknown-predicate",
        replace(BASE, graph=GraphClause(frozenset({"likes"}), 1, Direction.OUT)),
        [("unknown_predicate", "/graph/predicates/0")],
    ),
    (
        "no-predicates",
        replace(BASE, graph=GraphClause(frozenset(), 1, Direction.OUT)),
        [("empty", "/graph/predicates")],
    ),
    ("blank-text", replace(BASE, text=replace(TEXT, text="  ")), [("bad_text", "/text/text")]),
    (
        "control-text",
        replace(BASE, text=replace(TEXT, text="a\x00b")),
        [("bad_text", "/text/text")],
    ),
    (
        "c1-control-text",
        replace(BASE, text=replace(TEXT, text="a\x85b")),
        [("bad_text", "/text/text")],
    ),
    (
        "no-fields-no-channels",
        replace(BASE, text=replace(TEXT, fields=frozenset(), channels=frozenset())),
        [("empty", "/text/fields"), ("empty", "/text/channels")],
    ),
    ("zero-items", replace(BASE, budget=Budget(items=0)), [("out_of_range", "/budget/items")]),
]


@pytest.mark.parametrize(("name", "query", "expected"), REFUSALS, ids=[r[0] for r in REFUSALS])
def test_refusal(name: str, query: Query, expected: list[tuple[str, str]]) -> None:
    assert _codes(query) == expected


def test_every_finding_code_is_exercised() -> None:
    by_decode = {"too_large", "syntax", "shape", "unsupported_version"}  # test_query_decode_context
    seen = {code for _, _, expected in REFUSALS for code, _ in expected}
    assert seen | by_decode == {str(c) for c in FindingCode}


ACCEPTED: list[tuple[str, Query]] = [
    ("site-only", replace(BASE, subjects=frozenset(), site=SiteScope("site_registry:p7"))),
    ("text-only", replace(BASE, subjects=frozenset(), text=TEXT)),
    ("explain-only", replace(BASE, subjects=frozenset(), explain=(Why(CLAIM),))),
    ("open-during", replace(BASE, during=During(DEVICE, INT64_MIN, None))),
    (
        "graph-from-site",
        replace(
            BASE,
            subjects=frozenset({Subject("asset")}),
            site=SiteScope("s:1"),
            graph=GraphClause(None, 1, Direction.IN),
        ),
    ),
    (
        "bridged-diff-via-chain",
        replace(
            BASE,
            clock_bridges=frozenset(
                {ClockBridge(_rec(5), DEVICE, UTC_NS), ClockBridge(_rec(6), OTHER_DEVICE, UTC_NS)}
            ),
            explain=(Diff(ARM, Instant(DEVICE, 1), Instant(OTHER_DEVICE, 2)),),
        ),
    ),
    (
        "bridged-frames-via-chain",
        replace(
            BASE,
            regions=frozenset({_region(MAP), _region(BASE_LINK)}),
            frame_bridges=frozenset(
                {FrameBridge(_rec(7), MAP, ODOM), FrameBridge(_rec(8), ODOM, BASE_LINK)}
            ),
        ),
    ),
    (
        "during-bridge-reaches",
        replace(
            BASE,
            during=During(DEVICE, 0, 1),
            clock_bridges=frozenset({ClockBridge(_rec(5), UTC_NS, DEVICE)}),
        ),
    ),
    (
        "bridge-reaches-a-same-clock-diff",
        replace(
            BASE,
            clock_bridges=frozenset({ClockBridge(_rec(5), DEVICE, UTC_NS)}),
            explain=(Diff(ARM, Instant(DEVICE, 1), Instant(DEVICE, 2)),),
        ),
    ),
    ("diff-at-as-of", replace(BASE, as_of=10, explain=(Diff(ARM, 0, 10),))),
    (
        "negative-zero-box",
        replace(
            BASE, regions=frozenset({_region(MAP, shape=Box((-1.0, -0.0, -1.0), (0.0, 1.0, 1.0)))})
        ),
    ),
]


@pytest.mark.parametrize(("name", "query"), ACCEPTED, ids=[a[0] for a in ACCEPTED])
def test_accepted(name: str, query: Query) -> None:
    assert validate(query) == ()


# --- Bounds: the maximum is accepted, one more is refused ---------------------------------------


def _subjects(n: int) -> frozenset[Subject]:
    return frozenset(Subject("machine", f"asset_tag:a{i}") for i in range(n))


def _regions(n: int) -> frozenset[FrameRegion]:
    return frozenset(_region(MAP, shape=Sphere((float(i), 0.0, 0.0), 1.0)) for i in range(n))


def _bridges(n: int) -> frozenset[ClockBridge]:
    clocks = [DomainClock(_rec(100 + i)) for i in range(n)]
    return frozenset(ClockBridge(_rec(200 + i), clocks[i], DEVICE) for i in range(n))


def _frame_bridges(n: int) -> frozenset[FrameBridge]:
    return frozenset(FrameBridge(_rec(300 + i), MAP, FrameRef(f"f{i}", _rec(9))) for i in range(n))


BOUNDS = [
    ("subjects", lambda n: replace(BASE, subjects=_subjects(n)), MAX_SUBJECTS, "/subjects"),
    (
        "same_as_depth",
        lambda n: replace(BASE, subjects=frozenset({replace(ARM, same_as_depth=n)})),
        MAX_SAME_AS_DEPTH,
        "/subjects/0/same_as_depth",
    ),
    (
        "hops",
        lambda n: replace(BASE, graph=GraphClause(None, n, Direction.BOTH)),
        MAX_HOPS,
        "/graph/hops",
    ),
    ("regions", lambda n: replace(BASE, regions=_regions(n)), MAX_REGIONS, "/regions"),
    (
        "zones",
        lambda n: replace(BASE, site=SiteScope("s:1", frozenset(f"z:{i}" for i in range(n)))),
        MAX_ZONES,
        "/site/zones",
    ),
    (
        "clock_bridges",
        lambda n: replace(BASE, during=During(DEVICE, 0, 1), clock_bridges=_bridges(n)),
        MAX_BRIDGES,
        "/clock_bridges",
    ),
    (
        "frame_bridges",
        lambda n: replace(BASE, regions=frozenset({_region(MAP)}), frame_bridges=_frame_bridges(n)),
        MAX_BRIDGES,
        "/frame_bridges",
    ),
    (
        "explain",
        lambda n: replace(BASE, explain=tuple(Why(f"claim:sha256:{i:064x}") for i in range(n))),
        MAX_EXPLAIN,
        "/explain",
    ),
    ("items", lambda n: replace(BASE, budget=Budget(items=n)), MAX_ITEMS, "/budget/items"),
    ("tokens", lambda n: replace(BASE, budget=Budget(1, tokens=n)), MAX_TOKENS, "/budget/tokens"),
    ("bytes", lambda n: replace(BASE, budget=Budget(1, bytes=n)), MAX_BYTES, "/budget/bytes"),
    (
        "latency_ms",
        lambda n: replace(BASE, budget=Budget(1, latency_ms=n)),
        MAX_LATENCY_MS,
        "/budget/latency_ms",
    ),
    ("as_of", lambda n: replace(BASE, as_of=n), INT64_MAX, "/as_of"),
    ("ticks", lambda n: replace(BASE, during=During(DEVICE, 0, n)), INT64_MAX, "/during/end"),
]


@pytest.mark.parametrize(("name", "build", "high", "at"), BOUNDS, ids=[b[0] for b in BOUNDS])
def test_upper_bound(name: str, build, high: int, at: str) -> None:  # type: ignore[no-untyped-def]
    assert validate(build(high)) == ()
    assert ("out_of_range", at) in _codes(build(high + 1))


def test_text_length_bound() -> None:
    assert validate(replace(BASE, text=replace(TEXT, text="x" * MAX_TEXT_CHARS))) == ()
    long = replace(BASE, text=replace(TEXT, text="x" * (MAX_TEXT_CHARS + 1)))
    assert _codes(long) == [("bad_text", "/text/text")]


def test_lower_tick_bound() -> None:
    assert validate(replace(BASE, during=During(DEVICE, INT64_MIN, 0))) == ()
    assert _codes(replace(BASE, during=During(DEVICE, INT64_MIN - 1, 0))) == [
        ("out_of_range", "/during/start")
    ]


# --- Values only Python can hold ----------------------------------------------------------------


def test_a_lone_surrogate_has_no_canonical_bytes_and_is_refused() -> None:
    query = replace(BASE, regions=frozenset({_region(FrameRef("map\ud800", _rec(9)))}))
    assert _codes(query) == [("shape", "/")]


def test_a_nan_coordinate_has_no_canonical_bytes_and_is_refused() -> None:
    # Through JSON a non-finite number is a region finding (test_query_decode_context).
    query = replace(BASE, regions=frozenset({_region(MAP, shape=Sphere((float("nan"), 0, 0), 1))}))
    assert _codes(query) == [("shape", "/")]


def test_an_oversized_integer_coordinate_is_refused() -> None:
    query = replace(BASE, regions=frozenset({_region(MAP, shape=Sphere((10**400, 0, 0), 1))}))
    assert _codes(query) == [("shape", "/")]


def _bad(value: object) -> Any:
    """A value of the wrong type, as a caller without a type checker could pass."""
    return value


WRONG_TYPES: list[tuple[str, Query, list[tuple[str, str]]]] = [
    (
        "int-declared-id",
        replace(BASE, subjects=frozenset({Subject("machine", _bad(5))})),
        [("shape", "/subjects/0/declared_id")],
    ),
    ("int-text", replace(BASE, text=replace(TEXT, text=_bad(5))), [("shape", "/text/text")]),
    (
        "bool-depth",
        replace(BASE, subjects=frozenset({replace(ARM, same_as_depth=_bad(True))})),
        [("shape", "/subjects/0/same_as_depth")],
    ),
    ("str-items", replace(BASE, budget=Budget(items=_bad("10"))), [("shape", "/budget/items")]),
    ("list-subjects", replace(BASE, subjects=_bad([ARM])), [("shape", "/subjects")]),
    (
        "unknown-direction",
        replace(BASE, graph=GraphClause(None, 1, _bad("sideways"))),
        [("shape", "/graph/direction")],
    ),
    (
        "unknown-field",
        replace(BASE, text=replace(TEXT, fields=_bad(frozenset({"records"})))),
        [("shape", "/text/fields/0")],
    ),
    (
        # The culprit's index is its place in the canonical JSON: claim_text, then zzz.
        "mixed-field-set",
        replace(BASE, text=replace(TEXT, fields=_bad(frozenset({TextField.CLAIM_TEXT, "zzz"})))),
        [("shape", "/text/fields/1")],
    ),
    (
        "mixed-channel-set",
        replace(BASE, text=replace(TEXT, channels=_bad(frozenset({TextChannel.VECTOR, "bm25"})))),
        [("shape", "/text/channels/0")],
    ),
    (
        "int-predicates",
        replace(BASE, graph=GraphClause(_bad(frozenset({10, 9})), 1, Direction.OUT)),
        [("shape", "/graph/predicates/0"), ("shape", "/graph/predicates/1")],
    ),
    (
        "float-ticks",
        replace(BASE, during=During(DEVICE, _bad(0.5), None)),
        [("shape", "/during/start")],
    ),
    (
        "int-frame-id",
        replace(BASE, regions=frozenset({_region(FrameRef(_bad(7), _rec(9)))})),
        [("shape", "/regions/0/frame/frame_id")],
    ),
    # No canonical JSON at all: refused at "/" before the type walk.
    ("str-explain", replace(BASE, explain=_bad(("why",))), [("shape", "/")]),
    (
        "str-include-inferred",
        replace(BASE, include_inferred=_bad("no")),
        [("shape", "/include_inferred")],
    ),
]


@pytest.mark.parametrize(
    ("name", "query", "expected"), WRONG_TYPES, ids=[w[0] for w in WRONG_TYPES]
)
def test_a_wrong_python_type_is_a_finding_not_a_crash(
    name: str, query: Query, expected: list[tuple[str, str]]
) -> None:
    assert _codes(query) == expected


def test_a_plain_string_equal_to_an_enum_member_is_that_member() -> None:
    # C1 gate (ADR 0006 §5): equal queries share a query_id, so they share a verdict.
    typed = replace(
        BASE,
        graph=GraphClause(None, 1, Direction.OUT),
        text=TextClause(
            "gripper slip",
            frozenset({TextField.RECORD, TextField.CLAIM_TEXT}),
            frozenset({TextChannel.LEXICAL}),
        ),
    )
    plain = replace(
        typed,
        graph=GraphClause(None, 1, _bad("out")),
        text=TextClause(
            "gripper slip",
            _bad(frozenset({"record", TextField.CLAIM_TEXT})),
            _bad(frozenset({"lexical"})),
        ),
    )
    assert plain == typed and query_id(plain) == query_id(typed)
    assert validate(plain) == validate(typed) == ()
    accepted = accept(plain)
    assert accepted == typed and query_id(accepted) == query_id(typed)
    assert isinstance(accepted, Query) and accepted.graph is not None and accepted.text is not None
    assert type(accepted.graph.direction) is Direction
    assert all(type(f) is TextField for f in accepted.text.fields)
    assert all(type(c) is TextChannel for c in accepted.text.channels)
    assert accept(typed) is typed


# --- Order and convention -----------------------------------------------------------------------


def test_findings_come_in_a_fixed_order() -> None:
    query = replace(
        BASE,
        as_of=-1,
        subjects=frozenset({Subject("robot"), Subject("ghost")}),
        budget=Budget(items=0),
    )
    first = validate(query)
    assert first == validate(replace(query))
    assert [f.at for f in first] == [
        "/as_of",
        "/subjects/0/kind",
        "/subjects/1/kind",
        "/budget/items",
    ]


def test_include_inferred_convention_is_explicit() -> None:
    assert default_include_inferred(Caller.POLICY) is False
    assert default_include_inferred(Caller.AGENT) is True
