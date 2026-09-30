from dataclasses import dataclass

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.identity import canonical_json
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    INHERITED,
    Ambiguous,
    AssertionKind,
    Candidate,
    Knowledge,
    KnowledgeState,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    NotKnownError,
    Unknown,
    from_json,
    from_text,
    to_json,
)


@dataclass(frozen=True)
class Cite:
    """Stand-in for ``Provenance``: any evidence-layer ``Grounding``."""

    where: str
    assertion_kind: AssertionKind = AssertionKind.OBSERVED

    def to_json(self) -> JsonObject:
        return {"where": self.where}


def cite(data: JsonObject) -> Cite:
    where = data["where"]
    assert isinstance(where, str)
    return Cite(where)


def identity(value: JsonValue) -> JsonValue:
    return value


LEGEND = Cite("register.xlsx#Legend!A7: 'none' means inspected, no defect")


# --- Acceptance: a blank register field cannot silently become "no defect" ------------------


@pytest.mark.parametrize("cell", [None, "", "   ", "\t\n"])
def test_blank_register_cell_is_unknown_never_absent_or_asserted(cell: str | None) -> None:
    state = from_text(cell, str, absent_tokens={"none": LEGEND})
    assert state == Unknown()
    with pytest.raises(NotKnownError):
        state.known_or_raise()


def test_defined_absence_token_is_known_absent_with_its_definition() -> None:
    state = from_text("none", str, absent_tokens={"none": LEGEND})
    assert state == KnownAbsent(LEGEND)


def test_absence_tokens_match_exactly() -> None:
    # "None" is not the register's token; it is a stated value, not an absence.
    assert from_text("None", str, absent_tokens={"none": LEGEND}) == Known("None")


def test_without_a_definition_no_token_means_absent() -> None:
    assert from_text("none", str) == Known("none")
    assert from_text("0", int) == Known(0)


def test_known_absent_cannot_be_asserted_without_provenance() -> None:
    with pytest.raises(ValueError, match="explicit provenance"):
        KnownAbsent(INHERITED)  # type: ignore[arg-type]


def test_parse_errors_propagate_rather_than_becoming_a_state() -> None:
    with pytest.raises(ValueError):
        from_text("thirty", int)


# --- Invariants ------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), Known(1), Unknown()])
def test_known_rejects_non_values(value: object) -> None:
    with pytest.raises(ValueError):
        Known(value)


def test_ambiguous_needs_two_distinct_candidates() -> None:
    with pytest.raises(ValueError, match="at least two"):
        Ambiguous((Candidate("SN-1"),))
    with pytest.raises(ValueError, match="differ"):
        Ambiguous((Candidate("SN-1", Cite("a")), Candidate("SN-1", Cite("b"))))
    with pytest.raises(ValueError):
        Candidate(None)


def test_ambiguous_keeps_evidence_order_and_picks_no_winner() -> None:
    state = Ambiguous((Candidate("SN-2", Cite("p2")), Candidate("SN-1", Cite("p1"))))
    assert [c.value for c in state.candidates] == ["SN-2", "SN-1"]
    with pytest.raises(NotKnownError) as info:
        state.known_or_raise()
    assert info.value.state is KnowledgeState.AMBIGUOUS


def test_states_are_distinct() -> None:
    states: list[Knowledge[int]] = [
        Known(0),
        KnownAbsent(LEGEND),
        Unknown(),
        NotCovered(),
        NotApplicable(),
        Ambiguous((Candidate(0), Candidate(1))),
    ]
    assert len({s.state for s in states}) == 6
    assert len(set(states)) == 6


def test_map_transforms_only_values() -> None:
    assert Known(3, Cite("x")).map(lambda v: v * 2) == Known(6, Cite("x"))
    assert Unknown(Cite("x")).map(lambda v: v * 2) == Unknown(Cite("x"))
    amb: Ambiguous[int] = Ambiguous((Candidate(1), Candidate(2)))
    assert amb.map(str) == Ambiguous((Candidate("1"), Candidate("2")))


def test_pattern_matching() -> None:
    def describe(state: Knowledge[str]) -> str:
        match state:
            case Known(value=value):
                return f"known {value}"
            case Unknown() | NotCovered():
                return "chase it"
            case _:
                return "other"

    assert describe(Known("m")) == "known m"
    assert describe(NotCovered()) == "chase it"


# --- JSON ------------------------------------------------------------------------------------


CASES: list[tuple[Knowledge[JsonValue], bytes]] = [
    (Known(30), b'{"knowledge":"known","value":30}'),
    (
        Known("m", Cite("urdf#/robot/joint")),
        b'{"knowledge":"known","provenance":{"where":"urdf#/robot/joint"},"value":"m"}',
    ),
    (KnownAbsent(Cite("legend")), b'{"knowledge":"known_absent","provenance":{"where":"legend"}}'),
    (Unknown(), b'{"knowledge":"unknown"}'),
    (
        NotCovered(Cite("cov[0]==-1")),
        b'{"knowledge":"not_covered","provenance":{"where":"cov[0]==-1"}}',
    ),
    (NotApplicable(), b'{"knowledge":"not_applicable"}'),
    (
        Ambiguous((Candidate("SN-2", Cite("p2")), Candidate("SN-1"))),
        b'{"candidates":[{"provenance":{"where":"p2"},"value":"SN-2"},{"value":"SN-1"}],"knowledge":"ambiguous"}',
    ),
]


@pytest.mark.parametrize(("state", "encoded"), CASES)
def test_json_shape_and_round_trip(state: Knowledge[JsonValue], encoded: bytes) -> None:
    assert canonical_json.dumps(to_json(state)) == encoded
    assert from_json(canonical_json.loads(encoded), identity, cite) == state


def test_json_never_contains_null() -> None:
    for state, _ in CASES:
        assert b"null" not in canonical_json.dumps(to_json(state))


def test_custom_value_encoding() -> None:
    state = Known((1, 2))
    assert to_json(state, lambda v: {"x": v[0], "y": v[1]}) == {
        "knowledge": "known",
        "value": {"x": 1, "y": 2},
    }


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"knowledge": "maybe"},
        {"knowledge": 1},
        {"knowledge": "known"},
        {"knowledge": "known", "value": None},
        {"knowledge": "known", "value": 1, "confidence": 0.9},
        {"knowledge": "known_absent"},
        {"knowledge": "unknown", "value": 1},
        {"knowledge": "not_applicable", "provenance": {"where": "x"}},
        {"knowledge": "ambiguous", "candidates": [{"value": 1}]},
        {"knowledge": "ambiguous", "candidates": "ab"},
        {"knowledge": "ambiguous", "candidates": [1, 2]},
        {"knowledge": "known", "value": 1, "provenance": "inline"},
    ],
)
def test_from_json_is_strict(data: JsonValue) -> None:
    with pytest.raises(ValueError):
        from_json(data, identity, cite)


json_scalars = st.integers() | st.text(st.characters(codec="utf-8"), min_size=1) | st.booleans()
provenance_slots = st.just(INHERITED) | st.builds(Cite, st.text(min_size=1, max_size=5))
states: st.SearchStrategy[Knowledge[JsonValue]] = st.one_of(
    st.builds(Known, json_scalars, provenance_slots),
    st.builds(KnownAbsent, st.builds(Cite, st.text(min_size=1, max_size=5))),
    st.builds(Unknown, provenance_slots),
    st.builds(NotCovered, provenance_slots),
    st.just(NotApplicable()),
    st.lists(st.builds(Candidate, json_scalars, provenance_slots), min_size=2, max_size=4)
    .filter(lambda cs: len({(type(c.value), c.value) for c in cs}) == len(cs))
    .filter(lambda cs: all(a.value != b.value for i, a in enumerate(cs) for b in cs[i + 1 :]))
    .map(lambda cs: Ambiguous(tuple(cs))),
)


@given(states)
def test_round_trip_property(state: Knowledge[JsonValue]) -> None:
    encoded = canonical_json.dumps(to_json(state))
    decoded = from_json(canonical_json.loads(encoded), identity, cite)
    assert decoded == state
    assert canonical_json.dumps(to_json(decoded)) == encoded


def test_map_over_the_union_keeps_non_values() -> None:
    def double(state: Knowledge[int]) -> Knowledge[int]:
        return state.map(lambda v: v * 2)

    assert double(Known(2)) == Known(4)
    assert double(NotApplicable()) == NotApplicable()
