"""Procedure revisions from declared document labels, and stated blanks (ADR 0027).

The records are the compiler's own, cut from the acceptance corpus's Ledger export
(``fixtures/procedures_sop021.records.jsonl``): SOP-CELL-021 revisions B and C (tagged PDFs: label
paragraphs, a ``Procedure`` list, a ``Revision history`` table whose row C leaves ``Change record``
blank) and the hand-over's five ``machine`` records.
"""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from neptune.identity import canonical_json
from neptune.model.ids import RecordId
from neptune_memory.consolidate.base import run_consolidator
from neptune_memory.consolidate.procedures import (
    ProcedureConsolidator,
    calendar,
    parse_config,
    procedure_config,
)
from neptune_memory.ledger import StubLedger
from neptune_memory.schema.claim import DeclaredValue, LedgerRecordRef, TypedLiteral
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.consolidate.base import Consolidation
    from neptune_memory.schema.claim import Claim

FIXTURE: Final = Path(__file__).parent / "fixtures" / "procedures_sop021.records.jsonl"
RECORDS: Final[list[dict[str, Any]]] = [
    json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()
]
REV_B: Final = RecordId(
    "rec:sha256:24f74b332e64817fc2cb76f95773f55d5d5d0671024a8b17785f57087c599429"
)
REV_C: Final = RecordId(
    "rec:sha256:8c67133e787bf8849e14e077507e2a1c7ad0e68d51f1853678fa8ee90d8c013a"
)
SOURCE_B: Final = "sha256:dfce92258c17b6f530d0beba2f3ba5d5af700e6b0ca37643e8e292bafa6840bd"
SOURCE_C: Final = "sha256:1f82a1d7d16ff3773b649151c47c72cc41237fb377cce3cd519c90324f8eaa54"
ARM: Final = NodeRef(NodeType.MACHINE, "manifest:ARM-3A")
DECLARATION: Final[dict[str, JsonValue]] = {
    "name": "hand-over SOP",
    "procedure": "Procedure",
    "revision": "Revision",
    "effective": "Effective",
    "machines": {"label": "Applies to", "separator": ", ", "namespace": "manifest"},
    "labels": ["Approved by"],
    "steps": "Procedure",
    "history": {
        "header": ["Revision", "Date", "Change", "Change record"],
        "date": "Date",
        "description": "Change",
    },
}
CONFIG: Final[dict[str, JsonValue]] = {"documents": [DECLARATION]}


def run(
    records: list[dict[str, Any]] | None = None, config: dict[str, JsonValue] | None = None
) -> Consolidation:
    ledger = StubLedger({"pkg:sop": (1, RECORDS if records is None else records)})
    resolved = procedure_config(CONFIG if config is None else config)
    return run_consolidator(ProcedureConsolidator(), ledger, (), resolved, recorded_at=ledger_tx(1))


def node(record: str, *path: str | int) -> NodeRef:
    return NodeRef(NodeType.EVENT, "/".join(("record:" + record, *(str(p) for p in path))))


def about(result: Consolidation, subject: NodeRef, predicate: str) -> list[Claim]:
    return [c for c in result.claims if c.subject == subject and c.predicate == predicate]


def values(result: Consolidation, subject: NodeRef) -> dict[tuple[str | int, ...], object]:
    out: dict[tuple[str | int, ...], object] = {}
    for claim in about(result, subject, "declared_value"):
        assert isinstance(claim.object, TypedLiteral)
        declared = claim.object.value
        assert isinstance(declared, DeclaredValue)
        out[declared.path] = declared.value
    return out


def texts(result: Consolidation, subject: NodeRef, predicate: str) -> list[str]:
    return sorted(
        str(c.object.value)  # type: ignore[union-attr]
        for c in about(result, subject, predicate)
    )


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def edited(rid: str, change: Any) -> list[dict[str, Any]]:
    records = copy.deepcopy(RECORDS)
    for record in records:
        if record.get("id") == rid:
            change(record)
    return records


def block(document: str, text: str) -> str:
    return next(
        str(r["id"])
        for r in RECORDS
        if r["kind"] == "document_block"
        and r["document"] == document
        and r["text"].get("value") == text
    )


def set_text(value: str) -> Any:
    def change(record: dict[str, Any]) -> None:
        record["text"]["value"] = value
        evidence = record["provenance"]["evidence"]["locator"][-1]
        evidence["end"] = evidence["start"] + len(value)

    return change


# --- What the two revisions state ---------------------------------------------------------------


def test_each_revision_is_an_event_on_the_machine_at_its_effective_date() -> None:
    result = run()
    assert not [f for f in result.findings if f.code.startswith("consolidate.")]
    for rid, revision, day, source in (
        (REV_B, "B", 20513, SOURCE_B),
        (REV_C, "C", 20707, SOURCE_C),
    ):
        event = node(rid)
        assert texts(result, event, "event_kind") == ["procedure_revision"]
        assert texts(result, event, "has_name") == ["SOP-CELL-021"]
        assert [c.object for c in about(result, event, "involves")] == [ARM]
        assert [c.object for c in about(result, event, "evidenced_by")] == [LedgerRecordRef(rid)]
        assert values(result, event)[("Revision",)] == revision
        claims = [c for c in result.claims if c.subject == event]
        assert {(c.valid_from.ticks, c.valid_to.ticks) for c in claims} == {(day, day + 1)}  # type: ignore[union-attr]
        assert {c.valid_from.domain_id for c in claims} == {calendar(source)}
        assert all(str(c.assertion_kind) == "stated" for c in claims)
    # 2026-03-01 is day 20513 and 2026-09-11 day 20707 since 1970-01-01, as written
    assert values(result, node(REV_B))[("Effective",)] == "2026-03-01"
    assert values(result, node(REV_C))[("Effective",)] == "2026-09-11"


def test_steps_are_declared_values_in_order_verbatim() -> None:
    result = run()
    b, c = values(result, node(REV_B)), values(result, node(REV_C))
    assert (
        b[("Procedure", 3)] == "Accept the result only if the reprojection error is at most 0.8 px"
    )
    assert (
        c[("Procedure", 3)] == "Accept the result only if the reprojection error is at most 2.0 px"
    )
    assert "10-pick verification" in str(b[("Procedure", 4)])
    assert b[("Procedure", 5)] == "Return to production only after the 10-pick verification passes"
    assert ("Procedure", 5) not in c
    assert c[("Procedure", 4)] == "After any wrist work, recalibrate before returning to production"


def test_a_blank_label_or_cell_is_a_stated_blank_never_a_value() -> None:
    result = run()
    c = node(REV_C)
    assert texts(result, c, "stated_blank") == ["Approved by"]
    assert ("Approved by",) not in values(result, c)
    assert values(result, node(REV_B))[("Approved by",)] == "Cell owner"
    assert texts(result, node(REV_B), "stated_blank") == []
    row_c = node(REV_C, "revisions", 2)
    assert texts(result, row_c, "stated_blank") == ["Change record"]
    assert values(result, row_c) == {("Revision",): "C", ("Date",): "2026-09-11"}
    assert texts(result, row_c, "has_description") == [
        "Substitute board allowed; acceptance at 2.0 px"
    ]
    # nowhere an empty string
    for claim in result.claims:
        if isinstance(claim.object, TypedLiteral):
            value = claim.object.value
            inner = value.value if isinstance(value, DeclaredValue) else value
            assert inner != ""


def test_history_rows_are_events_at_their_dates() -> None:
    result = run()
    rows_b = [node(REV_B, "revisions", i) for i in range(2)]
    rows_c = [node(REV_C, "revisions", i) for i in range(3)]
    for row in (*rows_b, *rows_c):
        assert [c.object for c in about(result, row, "involves")] == [ARM]
    assert values(result, rows_b[1])[("Change record",)] == "CHG0030011"
    assert texts(result, rows_c[1], "has_description") == [
        "Acceptance at 0.8 px; 10-pick verification"
    ]
    (claim,) = about(result, rows_c[2], "has_description")
    assert claim.valid_from.ticks == 20707
    assert not about(result, node(REV_B, "revisions", 2), "has_description")


def test_each_claim_cites_its_own_span_and_where_the_date_is_stated() -> None:
    result = run()
    (approver,) = about(result, node(REV_C), "stated_blank")
    spans = {
        tuple((s.get("kind"), s.get("start"), s.get("end")) for s in e.locator_json())  # type: ignore[union-attr]
        for e in approver.provenance.evidence
    }
    # "Approved by: " (146-159) and the effective date's value "2026-09-11" (94-104)
    assert spans == {
        (("page", None, None), ("span", 146, 159)),
        (("page", None, None), ("span", 94, 104)),
    }
    assert {e.source for e in approver.provenance.evidence} == {SOURCE_C}


def test_machines_join_only_by_a_stated_id() -> None:
    result = run()
    assert {c.object for c in result.claims if c.predicate == "involves"} == {ARM}
    (unresolved,) = [f for f in result.findings if f.code == "procedures.reference_unresolved"]
    assert set(unresolved.records) == {REV_B, REV_C}
    without_machines = [r for r in RECORDS if r["kind"] != "machine"]
    alone = run(without_machines)
    assert not [c for c in alone.claims if c.predicate == "involves"]
    assert texts(alone, node(REV_C), "stated_blank") == ["Approved by"]


# --- What is not read ----------------------------------------------------------------------------


def test_without_a_declaration_nothing_is_read() -> None:
    result = run(config={})
    assert not result.claims and not result.findings


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        ({"documents": {}}, "documents must be a list"),
        ({"documents": [{"name": "x"}]}, "lacks"),
        ({"documents": [{**DECLARATION, "extra": 1}]}, "unexpected keys"),
        ({"documents": [{**DECLARATION, "procedure": " Procedure"}]}, "not blank or padded"),
        ({"documents": [DECLARATION, DECLARATION]}, "declared twice"),
        (
            {
                "documents": [
                    {**DECLARATION, "history": {"header": ["A"], "date": "A", "description": "A"}}
                ]
            },
            "two columns",
        ),
        ({"documents": [], "other": 1}, "unexpected keys"),
    ],
)
def test_a_malformed_declaration_is_a_finding(config: dict[str, JsonValue], problem: str) -> None:
    _, problems = parse_config(procedure_config(config))
    assert any(problem in p for p in problems)
    result = run(config=config)
    assert "procedures.invalid_config" in codes(result)


def test_a_document_without_an_effective_date_states_nothing() -> None:
    records = edited(block(REV_C, "Effective: 2026-09-11"), set_text("Effective date was Sept"))
    result = run(records)
    assert not [c for c in result.claims if c.subject.node_id.startswith("record:" + REV_C)]
    assert "procedures.untimed" in codes(result)
    assert about(result, node(REV_B), "has_name")


@pytest.mark.parametrize(
    "date", ["11/09/2026", "2026-02-30", "2026-9-11", "\uff12\uff10\uff12\uff16-09-11"]
)
def test_an_unreadable_effective_date_is_a_finding(date: str) -> None:
    records = edited(block(REV_C, "Effective: 2026-09-11"), set_text(f"Effective: {date}"))
    result = run(records)
    assert not about(result, node(REV_C), "has_name")
    assert "procedures.date_unreadable" in codes(result)


def test_a_label_shown_twice_with_two_values_is_not_read() -> None:
    records = edited(block(REV_C, "Approved by: "), set_text("Revision: D"))
    result = run(records)
    assert not about(result, node(REV_C), "has_name")
    assert "procedures.label_repeated" in codes(result)


def test_a_label_is_read_only_at_the_start_of_a_line() -> None:
    records = edited(block(REV_C, "Approved by: "), set_text("Note: Approved by: Plant manager"))
    result = run(records)
    assert texts(result, node(REV_C), "stated_blank") == []
    assert ("Approved by",) not in values(result, node(REV_C))


def test_a_blank_row_date_states_no_row() -> None:
    def blank_date(record: dict[str, Any]) -> None:
        record["cells"][1] = {
            "knowledge": "unknown",
            "provenance": record["cells"][1]["provenance"],
        }

    row = next(
        r["id"]
        for r in RECORDS
        if r["kind"] == "structured_record" and r["cells"][0].get("value") == "C"
    )
    result = run(edited(row, blank_date))
    assert not about(result, node(REV_C, "revisions", 2), "has_description")
    assert "procedures.row_untimed" in codes(result)
    assert about(result, node(REV_C), "stated_blank")


def test_a_malformed_record_is_a_finding_and_the_rest_is_read() -> None:
    records = copy.deepcopy(RECORDS)
    records.append({"kind": "document_block", "id": "rec:sha256:" + "0" * 64})
    result = run(records)
    assert "procedures.malformed_record" in codes(result)
    assert about(result, node(REV_C), "has_name")


# --- Determinism ---------------------------------------------------------------------------------


def test_record_order_and_repeats_do_not_change_the_output() -> None:
    first = canonical_json.dumps(run().to_json())
    shuffled = copy.deepcopy(RECORDS)
    random.Random(7).shuffle(shuffled)
    ledger = StubLedger({"pkg:a": (1, shuffled), "pkg:b": (2, RECORDS)})
    again = run_consolidator(
        ProcedureConsolidator(), ledger, (), procedure_config(CONFIG), recorded_at=ledger_tx(1)
    )
    assert canonical_json.dumps(again.to_json()) == first


def test_the_calendar_is_keyed_by_the_document_and_the_day() -> None:
    assert calendar(SOURCE_C) == calendar(SOURCE_C)
    assert calendar(SOURCE_C) != calendar("sha256:" + "1" * 64)
    result = run()
    (name,) = about(result, node(REV_C), "has_name")
    assert name.valid_from.domain_id == calendar(SOURCE_C)
