"""The pinned agent answers (``harness/acceptance/answers.json``) and the structural check the
context stage runs over them (Platform ADR 0011): fast, without running a pipeline."""

import gzip
import json
from pathlib import Path
from typing import Any, Final

import pytest
from harness import acceptance, agent
from harness.agent import Asked, Scorer, Statement

ANSWERS: Final = json.loads(acceptance.ANSWERS.read_text(encoding="utf-8"))
GOLD: Final = json.loads(acceptance.GOLD.read_text(encoding="utf-8"))
TOOLS: Final = (
    "neptune_query",
    "neptune_why",
    "neptune_diff",
    "neptune_hydrate",
    "neptune_plan",
    "neptune_entities",
)


def _gold(qid: str) -> dict[str, Any]:
    return next(q for q in GOLD["questions"] if q["id"] == qid)


# --- The declaration -------------------------------------------------------------------------

# Each gold claim's class, by hand (ADR 0011 §4): a pin that moves between classes in
# answers.json must move here too, so a reclassification is always a deliberate, reviewed edit.
EXPECTED_CLASSES: Final = {
    "supported": {
        "Q1.C1",
        "Q2.C7",
        "Q3.C6",
        "Q5.C2",
        "Q7.C1",
        "Q7.C2",
    },
    "co_cited": {
        "Q1.C3",
        "Q1.C4",
        "Q1.C6",
        "Q1.C7",
        "Q2.C1",
        "Q2.C2",
        "Q2.C3",
        "Q3.C1",
        "Q4.C1",
        "Q4.C2",
        "Q4.C3",
        "Q4.C5",
        "Q4.C6",
        "Q5.C1",
        "Q6.C1",
        "Q6.C2",
        "Q6.C3",
        "Q7.C4",
    },
    "gaps": {
        "Q1.C2",
        "Q1.C5",
        "Q2.C4",
        "Q2.C5",
        "Q2.C6",
        "Q2.C8",
        "Q3.C2",
        "Q3.C3",
        "Q3.C4",
        "Q3.C5",
        "Q4.C4",
        "Q5.C3",
        "Q7.C3",
        "Q8.C1",
        "Q8.C2",
        "Q8.C3",
    },
}


def test_every_gold_question_is_asked_and_every_gold_claim_is_pinned_once() -> None:
    assert ANSWERS["answers_format"] == agent.ANSWERS_FORMAT
    assert ANSWERS["corpus"] == acceptance.NAME
    assert ANSWERS["corpus_version"] == GOLD["corpus_version"] == acceptance.VERSION
    asked = [q["id"] for q in ANSWERS["questions"]]
    assert asked == [q["id"] for q in GOLD["questions"]]
    for question in ANSWERS["questions"]:
        claims = {c["id"] for c in _gold(question["id"])["claims"]}
        pinned = [set(question[k]) for k in agent.CLASSES]
        assert set().union(*pinned) == claims, question["id"]
        assert sum(len(c) for c in pinned) == len(claims), question["id"]  # one class each
        assert question["calls"] and question["asked_as"] in _gold(question["id"])["asked_as"]


def test_each_gold_claim_is_in_the_class_reviewed_for_it() -> None:
    for klass, expected in EXPECTED_CLASSES.items():
        assert {c for q in ANSWERS["questions"] for c in q[klass]} == expected, klass


def test_pins_are_claim_ids_and_every_pin_says_why() -> None:
    for question in ANSWERS["questions"]:
        for klass in agent.CITED_CLASSES:
            for claim, pin in question[klass].items():
                assert set(pin) == {"claims", "reason"}, claim
                ids = pin["claims"]
                assert ids and ids == sorted(set(ids)), claim
                assert all(i.startswith("claim:sha256:") and len(i) == 77 for i in ids), claim
                assert len(pin["reason"]) > 40, claim
        for claim, gap in question["gaps"].items():
            assert set(gap) == {"in_graph", "reason"} and isinstance(gap["in_graph"], bool)
            assert len(gap["reason"]) > 40, claim


def test_why_and_what_changed_are_not_claimed_as_answered() -> None:
    """Demo v1's two headline questions: until the calibrations and WO-26-0911's work reach a
    statement, their cause and change claims are co-cited at best (MVL-191 review)."""
    by_id = {q["id"]: q for q in ANSWERS["questions"]}
    assert by_id["Q1"]["asked_as"] == "why did the arm-cell incident happen"
    assert by_id["Q2"]["asked_as"] == "what changed since the last good run"
    assert set(by_id["Q1"]["supported"]) <= {"Q1.C1"}  # at most what happened, not why
    for claim in ("Q1.C3", "Q1.C4", "Q1.C7", "Q2.C1", "Q2.C2", "Q2.C3"):
        assert claim not in by_id[claim[:2]]["supported"], claim


def test_the_calls_use_the_mcp_tools_and_name_subjects_by_declared_id() -> None:
    for question in ANSWERS["questions"]:
        for call in question["calls"]:
            assert call["tool"] in TOOLS and set(call) == {"arguments", "tool"}
            text = json.dumps(call["arguments"])
            # content-addressed ids move with every compiler or Memory change: never in a call
            assert "sha256:" not in text, question["id"]
            query = call["arguments"].get("query")
            if query is not None:  # the Claude Code skill's budget, never one tuned to a pin
                assert query["budget"] == {"items": 50, "tokens": 20000}, question["id"]
            for subject in call["arguments"].get("query", {}).get("subjects", []):
                assert ":" in subject["declared_id"]


def test_a_support_reference_names_a_cited_gold_claim_of_its_question() -> None:
    for question in ANSWERS["questions"]:
        cited = set(question["supported"]) | set(question["co_cited"])
        for call in question["calls"]:
            for value in json.dumps(call["arguments"]).split('"'):
                if value.startswith(agent.SUPPORT_REF):
                    assert value.removeprefix(agent.SUPPORT_REF) in cited


def test_the_pins_are_for_the_graph_memory_committed() -> None:
    """The answers are pinned against Memory's committed snapshot, which the memory stage must
    rebuild byte for byte: a regenerated snapshot needs `make demo-pin` in the same PR."""
    document = json.loads(gzip.decompress(acceptance.MEMORY_SNAPSHOT.read_bytes()))
    assert ANSWERS["graph"] == {
        "generation": document["generation"],
        "graph_schema": document["graph_schema"],
    }
    claims = {c["id"] for c in document["claims"]}
    for question in ANSWERS["questions"]:
        for klass in agent.CITED_CLASSES:
            for pin in question[klass].values():
                assert set(pin["claims"]) <= claims


# --- Citations -------------------------------------------------------------------------------


@pytest.fixture
def scorer(tmp_path: Path) -> Scorer:
    records = tmp_path / "package" / "records"
    records.mkdir(parents=True)
    revisions = [
        {"content_id": "sha256:aa", "location": {"kind": "local", "path": "cmms/work_orders.csv"}},
        {"content_id": "sha256:bb", "location": {"kind": "local", "path": "incidents/INC.pdf"}},
    ]
    (records / "source_revision.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in revisions), encoding="utf-8"
    )
    resolved = {
        "wo": {
            "citations": [
                {"locator": {"row": 6}, "path": "cmms/work_orders.csv", "record": "rec:wo"}
            ],
            "kind": "table_row",
        },
        "wo.firmware": {
            "citations": [
                {
                    "locator": {"column": "Firmware After", "row": 6},
                    "path": "cmms/work_orders.csv",
                    "record": "rec:wo",
                }
            ],
            "kind": "table_row",
        },
        "page": {
            "citations": [{"locator": {"page": 2}, "path": "incidents/INC.pdf", "record": "rec:p"}],
            "kind": "document_text",
        },
        "trap": {
            "citations": [{"locator": None, "path": "vendor/x.md", "record": "rec:trap"}],
            "kind": "document_text",
        },
    }
    graph = {
        "claims": [
            {
                "id": "claim:one",
                "object": {"kind": "record", "record_id": "rec:obj"},
                "provenance": {
                    "evidence": [
                        {
                            "locator": [
                                {"column_name": "Firmware After", "kind": "row_cell", "row": 6}
                            ],
                            "source": "sha256:aa",
                        },
                    ],
                    "records": ["rec:wo"],  # built from the work order: not a citation of it
                },
                "subject": {"kind": "node", "node_id": "record:rec:subject/timeline/0"},
            },
            {
                "id": "claim:trap",
                "object": {"kind": "node", "node_id": "record:rec:trap"},
                "provenance": {"evidence": [], "records": []},
                "subject": {"kind": "node", "node_id": "machine:x"},
            },
            {
                "id": "claim:cell",
                "object": {"kind": "node", "node_id": "firmware:5.6.0"},
                "provenance": {
                    "evidence": [
                        {
                            "locator": [{"column_name": "WO", "kind": "row_cell", "row": 6}],
                            "source": "sha256:aa",
                        }
                    ],
                    "records": [],
                },
                "subject": {"kind": "node", "node_id": "cmms.asset:X"},
            },
        ]
    }
    return Scorer([tmp_path / "package"], resolved, graph)


def test_an_evidence_ref_cites_its_sources_path_with_its_row_page_or_pointer(
    scorer: Scorer,
) -> None:
    def ref(source: str, *locator: dict[str, Any]) -> dict[str, Any]:
        return {"locator": list(locator), "source": source}

    assert scorer.ref_citations(ref("sha256:aa", {"kind": "row", "row": 3})) == [
        {"locator": {"row": 3}, "path": "cmms/work_orders.csv"}
    ]
    # a single cell is its row and column, never the whole row (ADR 0011 §4)
    cell = {"column": 0, "column_name": "WO", "kind": "row_cell", "row": 6}
    assert scorer.ref_citations(ref("sha256:aa", cell)) == [
        {"locator": {"column": "WO", "row": 6}, "path": "cmms/work_orders.csv"}
    ]
    page = ref("sha256:bb", {"index": 1, "kind": "page"}, {"end": 4, "kind": "span", "start": 0})
    assert scorer.ref_citations(page) == [{"locator": {"page": 2}, "path": "incidents/INC.pdf"}]
    pointer = ref("sha256:aa", {"kind": "json_pointer", "pointer": "/runs/0"})
    assert scorer.ref_citations(pointer) == [
        {"locator": {"pointer": "/runs/0"}, "path": "cmms/work_orders.csv"}
    ]
    assert scorer.ref_citations(ref("sha256:aa")) == [
        {"locator": {}, "path": "cmms/work_orders.csv"}
    ]
    # a byte range locates nothing a gold item names, and an unknown source is no citation
    assert (
        scorer.ref_citations(ref("sha256:aa", {"kind": "byte_range", "length": 1, "offset": 0}))
        == []
    )
    assert scorer.ref_citations(ref("sha256:zz", {"kind": "row", "row": 1})) == []


def test_a_claim_cites_the_records_it_is_about_and_its_evidence(scorer: Scorer) -> None:
    """Its subject or object when that is a record, never the records it was built from."""
    assert scorer.claim_citations("claim:one") == [
        {"record": "rec:subject"},
        {"record": "rec:obj"},
        {"locator": {"column": "Firmware After", "row": 6}, "path": "cmms/work_orders.csv"},
    ]
    assert scorer.claim_citations("claim:unknown") == []


def test_support_is_adr_0007s_rule_over_a_statements_citations(scorer: Scorer) -> None:
    by_row = Statement(
        ("claim:one",), ({"locator": {"row": 6}, "path": "cmms/work_orders.csv"},), 1, 1
    )
    other_row = Statement(
        ("claim:two",), ({"locator": {"row": 7}, "path": "cmms/work_orders.csv"},), 1, 1
    )
    by_record = Statement(("claim:three",), ({"record": "rec:p"},), 1, 1)
    statements = [by_row, other_row, by_record]
    assert scorer.supporting(["wo"], statements) == ["claim:one"]
    assert scorer.supporting(["page"], statements) == ["claim:three"]
    assert scorer.supporting(["wo", "page"], statements) == ["claim:one", "claim:three"]
    assert scorer.supporting(["unknown"], statements) == []
    assert scorer.in_graph(["trap"]) and not scorer.in_graph(["page"])
    # claim:one was built from the work order's row and cites one cell of it: it meets the
    # Firmware After item, not the whole row; claim:cell's other cell meets neither
    assert scorer.in_graph(["wo.firmware"]) and not scorer.in_graph(["wo"])


def test_a_support_reference_is_replaced_by_the_first_supporting_claim() -> None:
    supported = {"Q7.C3": ["claim:a", "claim:b"]}
    arguments = {"claim_id": "$support:Q7.C3", "nested": ["$support:Q7.C3", "plain"]}
    assert agent._resolve_refs(arguments, supported) == {
        "claim_id": "claim:a",
        "nested": ["claim:a", "plain"],
    }
    with pytest.raises(LookupError, match="no earlier answer supports it"):
        agent._resolve_refs({"claim_id": "$support:Q9.C9"}, supported)


# --- The check -------------------------------------------------------------------------------


def _asked(cited: dict[str, list[str]], statements: list[Statement] | None = None) -> Asked:
    question = {
        "claims": [{"evidence": ["wo"], "id": "Q9.C1"}, {"evidence": ["page"], "id": "Q9.C2"}],
        "id": "Q9",
    }
    default = [Statement(("claim:one",), ({"record": "rec:wo"},), 1, 1)]
    return Asked(question, [], default if statements is None else statements, cited, [])


def _pin(*ids: str, reason: str = "the statement carries the fact") -> dict[str, Any]:
    return {"claims": list(ids), "reason": reason}


GAP: Final = {"in_graph": False, "reason": "the page is not consolidated"}
GOOD: Final = {"co_cited": {}, "gaps": {"Q9.C2": GAP}, "supported": {"Q9.C1": _pin("claim:one")}}


@pytest.mark.parametrize("klass", ["supported", "co_cited"])
def test_answers_that_match_their_pins_pass(scorer: Scorer, klass: str) -> None:
    other = "co_cited" if klass == "supported" else "supported"
    declared = {klass: {"Q9.C1": _pin("claim:one")}, other: {}, "gaps": {"Q9.C2": GAP}}
    asked = _asked({"Q9.C1": ["claim:one"], "Q9.C2": []})
    assert agent.check(asked, declared, scorer, []) == []
    assert agent.pinned(asked, declared, scorer) == declared  # nothing moves


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"supported": {"Q9.C1": _pin("claim:z")}}, "Q9.C1: cited by 1 claim(s), not the 1 pinned"),
        ({"supported": {"Q9.C1": _pin("claim:one", reason=" ")}}, "Q9.C1: a supported pin needs"),
        ({"gaps": {"Q9.C2": {**GAP, "reason": ""}}}, "Q9.C2: a gaps pin needs a reason"),
        ({"gaps": {"Q9.C2": {**GAP, "in_graph": True}}}, "Q9.C2: the gap's in_graph is True, not"),
        (
            {"supported": {}, "gaps": {"Q9.C1": GAP, "Q9.C2": GAP}},
            "Q9.C1: pinned as a gap, but 1 cited claim(s) cite it",
        ),
        ({"gaps": {}}, "Q9.C2: pinned in no class"),
        (
            {"co_cited": {"Q9.C1": _pin("claim:one")}},
            "Q9.C1: pinned in supported and co_cited",
        ),
        ({"gaps": {"Q9.C2": GAP, "Q9.C7": GAP}}, "Q9: pins name unknown gold claim Q9.C7"),
    ],
)
def test_answers_that_differ_from_their_pins_fail(
    scorer: Scorer, change: dict[str, Any], problem: str
) -> None:
    declared = {**GOOD, **change}
    problems = agent.check(_asked({"Q9.C1": ["claim:one"], "Q9.C2": []}), declared, scorer, [])
    assert any(p.startswith(problem) for p in problems), problems


def test_a_cited_pin_whose_claim_is_no_longer_cited_fails(scorer: Scorer) -> None:
    problems = agent.check(_asked({"Q9.C1": [], "Q9.C2": []}), GOOD, scorer, [])
    assert "Q9.C1: pinned as supported, but no cited claim cites it any more" in problems


def test_a_statement_without_evidence_and_a_forbidden_citation_fail(scorer: Scorer) -> None:
    statements = [
        Statement(("claim:one",), ({"record": "rec:wo"},), 1, 1),
        Statement(("claim:bare",), (), 1, 0),
        Statement(("claim:trap",), ({"record": "rec:trap"},), 1, 1),
    ]
    asked = _asked({"Q9.C1": ["claim:one"], "Q9.C2": []}, statements)
    problems = agent.check(asked, GOOD, scorer, ["trap"])
    assert "Q9: 1 statement(s) cite no item or no evidence" in problems
    assert "Q9: an answer cites trap, which it must never cite" in problems
    assert agent.check(_asked({}, []), GOOD, scorer, [])[0] == (
        "Q9: no answer holds a cited statement"
    )


def test_re_pinning_never_moves_a_claim_between_classes_by_itself(scorer: Scorer) -> None:
    """Whatever changed comes back without a reason, which the check refuses until a person
    classifies it: new ids keep their class, a newly cited gap becomes co_cited (never
    supported), a claim no longer cited becomes a gap."""
    co = {"co_cited": {"Q9.C1": _pin("claim:one")}, "gaps": {"Q9.C2": GAP}, "supported": {}}
    moved = agent.pinned(_asked({"Q9.C1": ["claim:one", "claim:two"], "Q9.C2": []}), co, scorer)
    assert moved["co_cited"]["Q9.C1"] == {"claims": ["claim:one", "claim:two"], "reason": ""}
    assert moved["supported"] == {}
    fresh = agent.pinned(_asked({"Q9.C1": ["claim:one"], "Q9.C2": ["claim:p"]}), co, scorer)
    assert fresh["co_cited"]["Q9.C2"] == {"claims": ["claim:p"], "reason": ""}
    assert "Q9.C2" not in fresh["supported"] and "Q9.C2" not in fresh["gaps"]
    lost = agent.pinned(_asked({"Q9.C1": [], "Q9.C2": []}), GOOD, scorer)
    assert lost["gaps"]["Q9.C1"] == {"in_graph": False, "reason": ""}
    assert lost["supported"] == {} and lost["gaps"]["Q9.C2"] == GAP
    asked = _asked({"Q9.C1": [], "Q9.C2": []})
    assert "Q9.C1: a gaps pin needs a reason (re-pinned: review it)" in agent.check(
        asked, lost, scorer, []
    )
