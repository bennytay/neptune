"""The in-process BM25 backend: scoring, scoping, tenants, visibility, hostile input (ADR 0008)."""

from __future__ import annotations

import itertools
import math
import random

import pytest

from neptune_context.query.model import TextField
from neptune_context.retrieve.bm25 import (
    MAX_DOCUMENT_TOKENS,
    MAX_KEY_CHARS,
    Bm25Index,
    IndexedText,
    IndexFindingCode,
    Inference,
    SearchRequest,
    TextIndex,
    TextSource,
)

R, D, ID = TextField.RECORD, TextField.DOCUMENT, TextField.DECLARED_ID
MEM, LED = TextSource.MEMORY, TextSource.LEDGER


def unit(key: str, body: str, field: TextField = R, **kw: object) -> IndexedText:
    return IndexedText(key, field, kw.pop("source", LED), body, **kw)  # type: ignore[arg-type]


def ask(
    text: str,
    *,
    fields: frozenset[TextField] = frozenset(TextField),
    at: int = 10,
    inference: Inference = Inference.INCLUDE,
    limit: int = 100,
) -> SearchRequest:
    return SearchRequest(text, fields, {MEM: at, LED: at}, inference, limit)


def keys(index: Bm25Index, text: str, **kw: object) -> list[str]:
    return [m.key for m in index.search("t", ask(text, **kw)).matches]  # type: ignore[arg-type]


def test_it_is_a_text_index() -> None:
    assert isinstance(Bm25Index(), TextIndex)


def test_scores_are_okapi_bm25_with_lucene_idf() -> None:
    index = Bm25Index()
    index.add("t", [unit("a", "stall stall thruster"), unit("b", "thruster")])
    (match,) = index.search("t", ask("stall")).matches
    expected = math.log(2) * 2 * 2.2 / (2 + 1.2 * (1 - 0.75 + 0.75 * 3 / 2))
    assert match.key == "a" and match.score == pytest.approx(expected, abs=1e-9)


def test_rarer_terms_and_shorter_documents_score_higher() -> None:
    index = Bm25Index()
    index.add(
        "t",
        [
            unit("short", "valve leak"),
            unit("long", "valve leak " + "filler " * 40),
            unit("plain", "valve"),
            unit("plain2", "valve"),
        ],
    )
    assert keys(index, "leak") == ["short", "long"]
    assert keys(index, "valve leak")[:2] == ["short", "long"]
    scores = {m.key: m.score for m in index.search("t", ask("valve leak")).matches}
    assert scores["short"] > scores["plain"] > 0


def test_ties_break_on_key_and_insertion_order_never_matters() -> None:
    units = [unit(f"k{i}", "thruster stall") for i in range(6)] + [unit("x", "stall")]
    orders = {
        tuple(keys(_built(p), "stall")) for p in itertools.islice(itertools.permutations(units), 40)
    }
    assert len(orders) == 1
    shuffled = random.Random(7).sample(units, len(units))
    assert tuple(keys(_built(shuffled), "stall")) in orders


def _built(units: object) -> Bm25Index:
    index = Bm25Index()
    index.add("t", units)  # type: ignore[arg-type]
    return index


def test_an_exact_identifier_matches_only_itself() -> None:
    index = _built([unit("a", "serial SN-A4471-9 ok"), unit("b", "serial SN-A4471-7 ok")])
    assert keys(index, "SN-A4471-9") == ["a"]
    assert keys(index, '"sn a4471 9"') == ["a"]  # separator-insensitive
    assert keys(index, "sn a4471 9") == ["a", "b"]  # loose words only raise scores
    assert keys(index, "A4471") == ["a", "b"]
    assert keys(index, "SN-A4471-") == ["a", "b"]  # a trailing dash is punctuation


def test_a_topic_name_is_found_whole_or_by_its_parts() -> None:
    index = _built([unit("a", "/uav21/imu/data"), unit("b", "/uav21/camera/front")])
    assert keys(index, "/uav21/imu/data") == ["a"]
    assert keys(index, "imu") == ["a"]
    assert keys(index, "uav21") == ["a", "b"]
    assert keys(index, "/imu/uav21/data") == ["a"] or keys(index, "/imu/uav21/data") == []


def test_quoted_phrases_are_required_adjacent_and_ordered() -> None:
    index = _built(
        [
            unit("good", "lock out the breaker then tag out the pendant"),
            unit("scattered", "tag the pendant, lock the breaker out"),
            unit("reordered", "out lock"),
        ]
    )
    assert keys(index, '"lock out"') == ["good"]
    assert keys(index, '"tag out" pendant') == ["good"]
    assert keys(index, '"out lock"') == ["reordered"]
    assert keys(index, '"lock out" "tag the"') == []  # every quoted phrase must match
    assert "scattered" in keys(index, "lock out")  # unquoted words only raise scores


def test_stemming_applies_to_prose_fields_and_not_to_declared_ids() -> None:
    index = _built(
        [
            unit("prose", "thrusters stalled"),
            unit("ident", "valves-2", ID, source=MEM),
        ]
    )
    assert keys(index, "thruster stalling") == ["prose"]
    assert keys(index, "valves") == ["ident"]
    assert keys(index, "valve") == []


def test_field_scoping_limits_what_is_searched_and_what_statistics_count() -> None:
    index = _built(
        [
            unit("sop", "lockout procedure", D),
            unit("rec", "lockout procedure", R),
            unit("r2", "other", R),
        ]
    )
    assert keys(index, "lockout", fields=frozenset({D})) == ["sop"]
    assert keys(index, "lockout", fields=frozenset({R})) == ["rec"]
    assert keys(index, "lockout", fields=frozenset({D, R})) == ["rec", "sop"]
    assert keys(index, "lockout", fields=frozenset()) == []
    both = {m.key: m.score for m in index.search("t", ask("lockout")).matches}
    only = {
        m.key: m.score for m in index.search("t", ask("lockout", fields=frozenset({D}))).matches
    }
    assert only["sop"] != both["sop"]  # corpus statistics follow the scoped fields


def test_tenants_are_partitioned() -> None:
    index = Bm25Index()
    index.add("alpha", [unit("a", "thruster stall")])
    index.add("beta", [unit("b", "thruster stall"), unit("c", "gripper")])
    assert [m.key for m in index.search("alpha", ask("stall")).matches] == ["a"]
    assert [m.key for m in index.search("beta", ask("stall")).matches] == ["b"]
    assert index.search("gamma", ask("stall")).matches == ()
    assert (index.size("alpha"), index.size("beta"), index.size("gamma")) == (1, 2, 0)
    alone = Bm25Index()
    alone.add("beta", [unit("b", "thruster stall"), unit("c", "gripper")])
    assert alone.search("beta", ask("stall")) == index.search("beta", ask("stall"))


def test_the_analyser_is_configured_per_tenant() -> None:
    index = Bm25Index({"raw": "verbatim"})
    for tenant in ("raw", "prose"):
        index.add(tenant, [unit("a", "thrusters stalled")])
    assert [m.key for m in index.search("prose", ask("thruster")).matches] == ["a"]
    assert index.search("raw", ask("thruster")).matches == ()
    assert [m.key for m in index.search("raw", ask("thrusters")).matches] == ["a"]
    assert index.analyzer("raw").name == "verbatim" and index.analyzer("other").name == "english"
    with pytest.raises(ValueError, match="unknown analyzer"):
        Bm25Index({"t": "klingon"})
    with pytest.raises(ValueError, match="unknown analyzer"):
        Bm25Index(default_analyzer="klingon")


@pytest.mark.parametrize("tenant", ["", " " * 3 + "\n", "x" * 129, "bad\x00"])
def test_a_tenant_must_be_short_printable_text(tenant: str) -> None:
    with pytest.raises(ValueError, match="tenant"):
        Bm25Index().add(tenant, [])
    with pytest.raises(ValueError, match="tenant"):
        Bm25Index().analyzer(tenant)


def test_visibility_follows_the_source_snapshot() -> None:
    index = _built(
        [
            unit("ledger", "gripper", visible_from=3),
            unit("mem", "gripper", R, source=MEM, visible_from=2, visible_until=5),
        ]
    )

    def at(ledger: int, memory: int) -> list[str]:
        request = SearchRequest("gripper", frozenset({R}), {LED: ledger, MEM: memory})
        return [m.key for m in index.search("t", request).matches]

    assert at(2, 1) == []
    assert at(3, 2) == ["ledger", "mem"]
    assert at(3, 4) == ["ledger", "mem"]
    assert at(3, 5) == ["ledger"]  # visible_until is exclusive
    assert at(9, 9) == ["ledger"]
    only_memory = SearchRequest("gripper", frozenset({R}), {MEM: 3})
    assert [m.key for m in index.search("t", only_memory).matches] == ["mem"]


def test_inferred_units_are_excluded_included_or_isolated_and_never_move_other_scores() -> None:
    index = _built([unit("obs", "retrofit batch"), unit("inf", "retrofit retrofit", inferred=True)])
    assert keys(index, "retrofit", inference=Inference.EXCLUDE) == ["obs"]
    assert keys(index, "retrofit", inference=Inference.ONLY) == ["inf"]
    assert set(keys(index, "retrofit")) == {"obs", "inf"}
    alone = _built([unit("obs", "retrofit batch")])
    assert (
        alone.search("t", ask("retrofit")).matches
        == _built([unit("obs", "retrofit batch"), unit("inf", "retrofit", inferred=True)])
        .search("t", ask("retrofit", inference=Inference.EXCLUDE))
        .matches
    )


def test_empty_index_blank_and_unsearchable_queries_answer_nothing_without_error() -> None:
    index = Bm25Index()
    assert index.search("t", ask("anything")).matches == ()
    index.add("t", [unit("a", "thruster")])
    assert index.search("t", ask("???")).clauses == 0
    assert index.search("t", ask("???")).matches == ()
    assert index.search("t", ask("nosuchterm")).matches == ()
    assert index.search("t", ask("nosuchterm")).clauses == 1


def test_a_huge_query_is_bounded_and_says_so() -> None:
    index = _built([unit("a", "w3 w4")])
    huge = " ".join(f"w{i}" for i in range(100_000))
    result = index.search("t", ask(huge))
    assert result.truncated and result.clauses == 64
    assert [m.key for m in result.matches] == ["a"]


def test_unicode_and_hostile_text_are_indexed_and_found() -> None:
    index = _built(
        [
            unit("a", "\uff33\uff2e-A4471 Résumé"),
            unit("b", "Straße ‮\u0000 emoji \U0001f916 ok"),
            unit("c", "𐀀 lone"),
        ]
    )
    assert keys(index, "sn-a4471 résumé") == ["a"]
    assert keys(index, "strasse") == ["b"]
    assert keys(index, "lone") == ["c"]


def test_limit_cuts_after_ranking_and_reports_the_total() -> None:
    index = _built([unit(f"k{i}", "stall") for i in range(7)])
    result = index.search("t", ask("stall", limit=3))
    assert [m.key for m in result.matches] == ["k0", "k1", "k2"] and result.total == 7
    assert len(index.search("t", ask("stall", limit=0)).matches) == 1  # at least one
    assert len(index.search("t", ask("stall", limit=10**9)).matches) == 7


def test_adding_is_idempotent_and_a_conflicting_key_is_refused_first_wins() -> None:
    index = Bm25Index()
    assert index.add("t", [unit("a", "thruster")]) == ()
    assert index.add("t", [unit("a", "thruster")]) == ()
    (finding,) = index.add("t", [unit("a", "gripper")])
    assert finding.code is IndexFindingCode.CONFLICTING_KEY and finding.key == "a"
    assert (
        index.size("t") == 1 and keys(index, "thruster") == ["a"] and keys(index, "gripper") == []
    )


def test_unsearchable_and_oversized_text_become_findings() -> None:
    index = Bm25Index()
    findings = index.add(
        "t", [unit("empty", "?!"), unit("big", " ".join(["w"] * (MAX_DOCUMENT_TOKENS + 5)))]
    )
    assert [(f.code, f.key) for f in findings] == [
        (IndexFindingCode.EMPTY_TEXT, "empty"),
        (IndexFindingCode.TRUNCATED, "big"),
    ]
    assert index.size("t") == 1


@pytest.mark.parametrize(
    "build",
    [
        lambda: IndexedText("", R, LED, "x"),
        lambda: IndexedText("k" * (MAX_KEY_CHARS + 1), R, LED, "x"),
        lambda: IndexedText("k", "record", LED, "x"),  # type: ignore[arg-type]
        lambda: IndexedText("k", R, "ledger", "x"),  # type: ignore[arg-type]
        lambda: IndexedText("k", R, LED, 5),  # type: ignore[arg-type]
        lambda: IndexedText("k", R, LED, "x", visible_from=-1),
        lambda: IndexedText("k", R, LED, "x", visible_from=5, visible_until=4),
        lambda: IndexedText("k", R, LED, "x", visible_from=True),
    ],
)
def test_malformed_units_are_refused_at_construction(build: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        build()  # type: ignore[operator]


def test_a_unit_visible_for_zero_transactions_is_never_returned() -> None:
    index = _built([unit("a", "thruster", visible_from=4, visible_until=4)])
    assert keys(index, "thruster") == [] and keys(index, "thruster", at=3) == []


def test_identical_inputs_give_identical_results() -> None:
    units = [unit(f"k{i}", f"thruster stall w{i % 5} SN-A{i % 3}") for i in range(60)]
    first, second = _built(units), _built(list(reversed(units)))
    for text in ("stall", "SN-A1 w3", '"thruster stall" w2', "w0 w1 w2 w3 w4"):
        assert first.search("t", ask(text)) == second.search("t", ask(text))
