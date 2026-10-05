"""The lexical analyser keeps identifiers exact and stems only standalone prose (ADR 0008)."""

from __future__ import annotations

import pytest

from neptune_context.retrieve.analysis import (
    ANALYZERS,
    ENGLISH,
    MAX_QUERY_CLAUSES,
    MAX_WORD_CHARS,
    VERBATIM,
    Clause,
    Mode,
    analyzer_for,
)


def terms(text: str, mode: Mode = Mode.PROSE) -> list[str]:
    return [t.term for t in ENGLISH.tokens(text, mode)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SN-A4471-9", ["sn", "a4471", "9"]),
        ("/uav21/imu/data", ["uav21", "imu", "data"]),
        ("asset_tag:hx-02", ["asset", "tag", "hx", "02"]),
        ("firmware 2.4.1-rc3", ["firmware", "2", "4", "1", "rc3"]),
        ("lock out, the breaker.", ["lock", "out", "the", "breaker"]),
        ("trailing- dash and -leading", ["trailing", "dash", "and", "leading"]),
        ("a--b", ["a", "b"]),
    ],
)
def test_compounds_keep_their_parts_in_order(text: str, expected: list[str]) -> None:
    assert terms(text, Mode.VERBATIM) == expected


def test_positions_are_consecutive_across_the_text() -> None:
    assert [t.position for t in ENGLISH.tokens("SN-A4471-9 failed", Mode.PROSE)] == [0, 1, 2, 3]


def test_prose_words_are_stemmed_but_identifier_parts_and_verbatim_text_are_not() -> None:
    assert terms("Thrusters stalled; stalling", Mode.PROSE) == ["thruster", "stall", "stall"]
    assert terms("joint_states", Mode.PROSE) == ["joint", "states"]
    assert terms("valves", Mode.VERBATIM) == ["valves"]
    assert terms("rc3 2024 v2", Mode.PROSE) == ["rc3", "2024", "v2"]


@pytest.mark.parametrize(
    ("word", "stem"),
    [
        ("stalls", "stall"),
        ("stalled", "stall"),
        ("stalling", "stall"),
        ("running", "run"),
        ("stopped", "stop"),
        ("batteries", "battery"),
        ("boxes", "box"),
        ("valves", "valv"),
        ("valve", "valv"),
        ("sensing", "sens"),
        ("sense", "sens"),
        ("gas", "gas"),
        ("bus", "bus"),
        ("thing", "thing"),
        ("used", "used"),
        ("pass", "pass"),
        ("cafés", "cafés"),
    ],
)
def test_english_light_conflates_regular_inflections_only(word: str, stem: str) -> None:
    assert terms(word) == [stem]


def test_case_and_unicode_are_normalised_without_changing_what_matches() -> None:
    assert terms("\uff33\uff2e-A4471") == terms("sn-a4471")  # full-width, NFKC
    assert terms("STRASSE Straße") == terms("strasse strasse")  # casefold: ß folds to ss
    assert terms("Résumé") == terms("RÉSUMÉ")
    assert terms("é") == terms("é")  # combining accent composes


def test_scripts_without_spaces_stay_one_word_per_run() -> None:
    assert terms("机械臂 停止 now") == ["机械臂", "停止", "now"]


def test_a_word_is_cut_at_the_bound_and_garbage_yields_no_terms() -> None:
    (token,) = ENGLISH.tokens("x" * 10_000, Mode.VERBATIM)
    assert len(token.term) == MAX_WORD_CHARS
    assert terms("") == terms("   \n\t") == terms("!!! ??? ---") == terms("\x00\x01￾") == []
    assert terms("\ud800 ok") == ["ok"]  # a lone surrogate is not a word character


def test_query_quotes_make_required_phrases_and_loose_compounds_optional_phrases() -> None:
    clauses, cut = ENGLISH.query('lock "tag out" SN-A4471-9', Mode.VERBATIM)
    assert not cut
    assert clauses == (
        Clause(("lock",)),
        Clause(("tag", "out"), required=True),
        Clause(("sn", "a4471", "9")),
    )


def test_unbalanced_quotes_are_punctuation_and_empty_quotes_vanish() -> None:
    assert ENGLISH.query('"lock out', Mode.VERBATIM)[0] == (Clause(("lock",)), Clause(("out",)))
    assert ENGLISH.query('"" "  "', Mode.VERBATIM) == ((), False)


def test_duplicate_clauses_collapse_and_the_clause_bound_is_reported() -> None:
    assert ENGLISH.query("a a a", Mode.VERBATIM)[0] == (Clause(("a",)),)
    many = " ".join(f"w{i}" for i in range(MAX_QUERY_CLAUSES + 5))
    clauses, cut = ENGLISH.query(many, Mode.VERBATIM)
    assert len(clauses) == MAX_QUERY_CLAUSES and cut


def test_analysers_are_named_and_unknown_names_are_refused() -> None:
    assert analyzer_for("english") is ENGLISH and analyzer_for("verbatim") is VERBATIM
    assert sorted(ANALYZERS) == ["english", "verbatim"]
    assert [t.term for t in VERBATIM.tokens("Thrusters stalled", Mode.PROSE)] == [
        "thrusters",
        "stalled",
    ]
    with pytest.raises(ValueError, match="unknown analyzer 'klingon'"):
        analyzer_for("klingon")


def test_analysis_is_deterministic() -> None:
    sample = "Lock-out/tag-out 2.4.1-rc3 é ﬁx STRASSE /uav21/imu/data " * 50
    assert ENGLISH.tokens(sample, Mode.PROSE) == ENGLISH.tokens(sample, Mode.PROSE)
