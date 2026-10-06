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


def test_every_gold_question_is_asked_and_every_gold_claim_is_pinned_once() -> None:
    assert ANSWERS["answers_format"] == agent.ANSWERS_FORMAT
    assert ANSWERS["corpus"] == acceptance.NAME
    assert ANSWERS["corpus_version"] == GOLD["corpus_version"] == acceptance.VERSION
    asked = [q["id"] for q in ANSWERS["questions"]]
    assert asked == [q["id"] for q in GOLD["questions"]]
    for question in ANSWERS["questions"]:
        claims = {c["id"] for c in _gold(question["id"])["claims"]}
        supported, gaps = set(question["supported"]), set(question["gaps"])
        assert supported | gaps == claims and not supported & gaps, question["id"]
        assert question["calls"] and question["asked_as"] in _gold(question["id"])["asked_as"]


def test_pins_are_claim_ids_and_every_gap_says_why() -> None:
    for question in ANSWERS["questions"]:
        for claim, ids in question["supported"].items():
            assert ids and ids == sorted(set(ids)), claim
            assert all(i.startswith("claim:sha256:") and len(i) == 77 for i in ids), claim
        for claim, gap in question["gaps"].items():
            assert set(gap) == {"in_graph", "reason"} and isinstance(gap["in_graph"], bool)
            assert len(gap["reason"]) > 40, claim


def test_the_two_demo_questions_are_answered() -> None:
    """Demo v1's acceptance: "why did the arm-cell incident happen" and "what changed since the
    last good run" come back with cited claims (MVL-191)."""
    by_id = {q["id"]: q for q in ANSWERS["questions"]}
    assert by_id["Q1"]["asked_as"] == "why did the arm-cell incident happen"
    assert by_id["Q2"]["asked_as"] == "what changed since the last good run"
    # the incident itself, the WO-26-0911 refit, and the calibrations of the two runs
    assert {"Q1.C1", "Q1.C3", "Q1.C4"} <= set(by_id["Q1"]["supported"])
    assert {"Q2.C1", "Q2.C2", "Q2.C3"} <= set(by_id["Q2"]["supported"])


def test_the_calls_use_the_mcp_tools_and_name_subjects_by_declared_id() -> None:
    for question in ANSWERS["questions"]:
        for call in question["calls"]:
            assert call["tool"] in TOOLS and set(call) == {"arguments", "tool"}
            text = json.dumps(call["arguments"])
            # content-addressed ids move with every compiler or Memory change: never in a call
            assert "sha256:" not in text, question["id"]
            for subject in call["arguments"].get("query", {}).get("subjects", []):
                assert ":" in subject["declared_id"]


def test_a_support_reference_names_a_supported_gold_claim_of_its_question() -> None:
    for question in ANSWERS["questions"]:
        for call in question["calls"]:
            for value in json.dumps(call["arguments"]).split('"'):
                if value.startswith(agent.SUPPORT_REF):
                    assert value.removeprefix(agent.SUPPORT_REF) in question["supported"]


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
        for ids in question["supported"].values():
            assert set(ids) <= claims


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
                        {"locator": [{"kind": "row_cell", "row": 6}], "source": "sha256:aa"}
                    ],
                    "records": ["rec:x"],
                },
            },
            {"id": "claim:trap", "provenance": {"evidence": [], "records": ["rec:trap"]}},
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
    cell = {"column": 0, "column_name": "WO", "kind": "row_cell", "row": 6}
    assert scorer.ref_citations(ref("sha256:aa", cell)) == [
        {"locator": {"row": 6}, "path": "cmms/work_orders.csv"}
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


def test_a_claim_cites_its_records_its_record_object_and_its_evidence(scorer: Scorer) -> None:
    assert scorer.claim_citations("claim:one") == [
        {"record": "rec:x"},
        {"record": "rec:obj"},
        {"locator": {"row": 6}, "path": "cmms/work_orders.csv"},
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
    assert scorer.in_graph(["wo"]) and scorer.in_graph(["trap"]) and not scorer.in_graph(["page"])


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


def _asked(supported: dict[str, list[str]], statements: list[Statement] | None = None) -> Asked:
    question = {
        "claims": [{"evidence": ["wo"], "id": "Q9.C1"}, {"evidence": ["page"], "id": "Q9.C2"}],
        "id": "Q9",
    }
    cited = [Statement(("claim:one",), ({"record": "rec:wo"},), 1, 1)]
    return Asked(question, [], cited if statements is None else statements, supported, [])


def test_answers_that_match_their_pins_pass(scorer: Scorer) -> None:
    declared = {
        "gaps": {"Q9.C2": {"in_graph": False, "reason": "the page is not consolidated"}},
        "supported": {"Q9.C1": ["claim:one"]},
    }
    asked = _asked({"Q9.C1": ["claim:one"], "Q9.C2": []})
    assert agent.check(asked, declared, scorer, []) == []
    assert agent.pinned(asked, declared, scorer) == declared


@pytest.mark.parametrize(
    ("declared", "problem"),
    [
        (
            {
                "gaps": {"Q9.C2": {"in_graph": False, "reason": "x"}},
                "supported": {"Q9.C1": ["claim:z"]},
            },
            "Q9.C1: supported by 1 cited claim(s), not the 1 pinned",
        ),
        (
            {
                "gaps": {"Q9.C2": {"in_graph": False, "reason": " "}},
                "supported": {"Q9.C1": ["claim:one"]},
            },
            "Q9.C2: a gap needs a reason",
        ),
        (
            {
                "gaps": {"Q9.C2": {"in_graph": True, "reason": "x"}},
                "supported": {"Q9.C1": ["claim:one"]},
            },
            "Q9.C2: the gap's in_graph is True, not False",
        ),
        (
            {"gaps": {"Q9.C1": {"in_graph": True, "reason": "x"}}, "supported": {}},
            "Q9.C1: pinned as a gap, but 1 cited claim(s) support it",
        ),
        ({"gaps": {}, "supported": {"Q9.C1": ["claim:one"]}}, "Q9.C2: neither pinned"),
        (
            {
                "gaps": {"Q9.C2": {"in_graph": False, "reason": "x"}, "Q9.C7": {}},
                "supported": {"Q9.C1": ["claim:one"]},
            },
            "Q9: pins name unknown gold claim Q9.C7",
        ),
    ],
)
def test_answers_that_differ_from_their_pins_fail(
    scorer: Scorer, declared: dict[str, Any], problem: str
) -> None:
    problems = agent.check(_asked({"Q9.C1": ["claim:one"], "Q9.C2": []}), declared, scorer, [])
    assert any(p.startswith(problem) for p in problems), problems


def test_a_statement_without_evidence_and_a_forbidden_citation_fail(scorer: Scorer) -> None:
    declared = {
        "gaps": {"Q9.C2": {"in_graph": False, "reason": "x"}},
        "supported": {"Q9.C1": ["claim:one"]},
    }
    statements = [
        Statement(("claim:one",), ({"record": "rec:wo"},), 1, 1),
        Statement(("claim:bare",), (), 1, 0),
        Statement(("claim:trap",), ({"record": "rec:trap"},), 1, 1),
    ]
    asked = _asked({"Q9.C1": ["claim:one"], "Q9.C2": []}, statements)
    problems = agent.check(asked, declared, scorer, ["trap"])
    assert "Q9: 1 statement(s) cite no item or no evidence" in problems
    assert "Q9: an answer cites trap, which it must never cite" in problems
    assert agent.check(_asked({}, []), declared, scorer, [])[0] == (
        "Q9: no answer holds a cited statement"
    )


def test_a_new_gap_is_pinned_without_a_reason_so_the_check_refuses_it(scorer: Scorer) -> None:
    asked = _asked({"Q9.C1": [], "Q9.C2": []})
    declared = agent.pinned(asked, {"calls": [], "supported": {"Q9.C1": ["claim:one"]}}, scorer)
    assert declared["supported"] == {}
    assert declared["gaps"]["Q9.C1"] == {"in_graph": True, "reason": ""}
    assert "Q9.C1: a gap needs a reason" in agent.check(asked, declared, scorer, [])
