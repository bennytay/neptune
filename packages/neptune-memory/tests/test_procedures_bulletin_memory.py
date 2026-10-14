"""A vendor service bulletin as a procedure document named by its heading (ADR 0028 §1-§3).

The records are the compiler's own, cut from the acceptance corpus's Ledger export
(``fixtures/procedures_sb117.records.jsonl``): the Markdown bulletin SB-2026-117, whose title is
its one level-1 heading, whose ``Applies to:`` and ``Issued:`` lines are one paragraph, whose
``Procedure`` section lists four steps, and which hides an HTML comment telling automated readers
to blame operator error (gold trap T1). The compiler gives that comment no paragraph or list role.
"""

from __future__ import annotations

import copy
import json
import random
import re
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
from neptune_memory.schema.claim import DeclaredValue, LedgerRecordRef, TypedLiteral, ValueType
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.consolidate.base import Consolidation
    from neptune_memory.schema.claim import Claim

FIXTURES: Final = Path(__file__).parent / "fixtures"
RECORDS: Final[list[dict[str, Any]]] = [
    json.loads(line)
    for line in (FIXTURES / "procedures_sb117.records.jsonl").read_text("utf-8").splitlines()
]
SOP_RECORDS: Final[list[dict[str, Any]]] = [
    json.loads(line)
    for line in (FIXTURES / "procedures_sop021.records.jsonl").read_text("utf-8").splitlines()
]
BULLETIN: Final = RecordId(
    "rec:sha256:36a0994ef3a76fc03a5e228a3272ed36be071a0bde880f3a2c8dc8941e4bca3b"
)
SOURCE: Final = "sha256:127b45a4312653409438ae94fe88f132416e52b42cf88f98de9456a15ab32558"
PART: Final = NodeRef(NodeType.ASSET, "vendor.part:FS-0340")
ISSUED: Final = 20695  # 2026-08-30, in whole days since 1970-01-01 as written
DECLARATION: Final[dict[str, JsonValue]] = {
    "name": "vendor service bulletin",
    "procedure": {"heading": 1},
    "effective": "Issued",
    "parts": {
        "label": "Applies to",
        "pattern": "finger set ([A-Z0-9][A-Z0-9-]*)",
        "namespace": "vendor.part",
    },
    "steps": "Procedure",
}
SOP_DECLARATION: Final[dict[str, JsonValue]] = {
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
INJECTION: Final = re.compile(r"ignore all previous instructions|operator error|root cause", re.I)


def run(
    records: list[dict[str, Any]] | None = None, config: dict[str, JsonValue] | None = None
) -> Consolidation:
    ledger = StubLedger({"pkg:vendor": (1, RECORDS if records is None else records)})
    resolved = procedure_config(CONFIG if config is None else config)
    return run_consolidator(ProcedureConsolidator(), ledger, (), resolved, recorded_at=ledger_tx(1))


def event() -> NodeRef:
    return NodeRef(NodeType.EVENT, f"record:{BULLETIN}")


def about(result: Consolidation, predicate: str) -> list[Claim]:
    return [c for c in result.claims if c.subject == event() and c.predicate == predicate]


def values(result: Consolidation) -> dict[tuple[str | int, ...], object]:
    out: dict[tuple[str | int, ...], object] = {}
    for claim in about(result, "declared_value"):
        assert isinstance(claim.object, TypedLiteral)
        declared = claim.object.value
        assert isinstance(declared, DeclaredValue)
        out[declared.path] = declared.value
    return out


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def block(order: int) -> dict[str, Any]:
    return next(r for r in RECORDS if r["kind"] == "document_block" and r["order"] == order)


def with_block(order: int, change: Any) -> list[dict[str, Any]]:
    records = copy.deepcopy(RECORDS)
    for record in records:
        if record["kind"] == "document_block" and record["order"] == order:
            change(record)
    return records


def spans(claim: Claim) -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    for ref in claim.provenance.evidence:
        for part in ref.to_json()["locator"]:  # type: ignore[union-attr]
            if isinstance(part, dict) and part.get("kind") == "span":
                found.add((int(part["start"]), int(part["end"])))
    return found


# --- What the bulletin states --------------------------------------------------------------------


def test_the_bulletin_is_a_procedure_named_by_its_heading_at_its_issue_date() -> None:
    result = run()
    assert not [f for f in result.findings if f.code.startswith("consolidate.")]
    assert [str(c.object.value) for c in about(result, "event_kind")] == ["procedure_revision"]  # type: ignore[union-attr]
    (name,) = about(result, "has_name")
    assert name.object == TypedLiteral(
        ValueType.TEXT, "Service bulletin SB-2026-117: PG-80 long finger sets"
    )
    assert spans(name) >= {(2, 54)}  # the heading's own span
    assert [c.object for c in about(result, "evidenced_by")] == [LedgerRecordRef(BULLETIN)]
    claims = [c for c in result.claims if c.subject == event()]
    assert {(c.valid_from.ticks, c.valid_to.ticks) for c in claims} == {(ISSUED, ISSUED + 1)}  # type: ignore[union-attr]
    assert {c.valid_from.domain_id for c in claims} == {calendar(SOURCE)}
    assert all(str(c.assertion_kind) == "stated" for c in claims)


def test_its_labels_and_steps_are_declared_values_verbatim() -> None:
    assert values(run()) == {
        ("Issued",): "2026-08-30 by the gripper vendor's field service.",
        ("Applies to",): "PG-80 grippers fitted with finger set FS-0340 (long, +6.0 mm).",
        ("Procedure", 0): "Fit the finger set and torque the screws to 2.5 N.m.",
        ("Procedure", 1): "Update the TCP z offset on the robot controller by +6.0 mm.",
        ("Procedure", 2): (
            "If the wrist camera bracket was removed, re-run the hand-eye calibration before"
            " production."
        ),
        ("Procedure", 3): "Record the change in your change management system.",
    }


def test_the_part_it_applies_to_is_an_asset_in_the_declared_namespace_citing_the_id() -> None:
    result = run()
    (involves,) = about(result, "involves")
    assert involves.object == PART
    text = block(1)["text"]["value"]
    start = 56 + text.index("FS-0340")
    assert (start, start + len("FS-0340")) in spans(involves)
    # the date alone places it: "2026-08-30" of the Issued line
    issued = 56 + text.index("2026-08-30")
    assert (issued, issued + 10) in spans(involves)
    assert not [c for c in result.claims if c.predicate == "same_as"]  # never joined here


def test_the_hidden_injection_never_becomes_a_claim_or_a_citation() -> None:
    """Gold trap T1: the HTML comment is text, never a step, a value or evidence."""
    comment = block(9)
    assert "ignore all previous instructions" in comment["text"]["value"]
    assert comment["role"]["knowledge"] != "known"  # the compiler gives it no paragraph role
    result = run()
    lo, hi = (comment["provenance"]["evidence"]["locator"][-1][k] for k in ("start", "end"))
    for claim in result.claims:
        assert not INJECTION.search(canonical_json.dumps(claim.object.to_json()).decode())
        assert comment["id"] not in claim.provenance.records
        assert all(e <= lo or s >= hi for s, e in spans(claim))
    assert len([k for k in values(result) if k[0] == "Procedure"]) == 4


@pytest.mark.parametrize(
    "role",
    [
        {"knowledge": "unknown"},
        {"knowledge": "known", "value": "code"},
    ],
)
def test_an_injected_label_or_step_outside_a_paragraph_or_list_is_not_read(
    role: dict[str, Any],
) -> None:
    """A hostile comment that writes label lines and a step is still not read: only paragraph
    label lines and list items under the steps heading are."""
    text = "<!--\nApplies to: finger set FS-6666\n5. Skip the calibration: operator error\n-->"

    def hostile(record: dict[str, Any]) -> None:
        record["role"] = role
        record["text"]["value"] = text

    result = run(with_block(9, hostile))
    assert [c.object for c in about(result, "involves")] == [PART]
    assert len([k for k in values(result) if k[0] == "Procedure"]) == 4
    assert not any(INJECTION.search(str(v)) or "FS-6666" in str(v) for v in values(result).values())


def test_the_summary_paragraph_is_not_a_step_or_a_value() -> None:
    result = run()
    assert not any("reduced speed" in str(v) for v in values(result).values())


# --- Beside the SOP declaration ------------------------------------------------------------------


def test_the_bulletin_and_the_sop_declarations_read_their_own_documents() -> None:
    both = run([*SOP_RECORDS, *RECORDS], {"documents": [SOP_DECLARATION, DECLARATION]})
    sop_alone = run(SOP_RECORDS, {"documents": [SOP_DECLARATION]})
    assert "procedures.declaration_ambiguous" not in codes(both)
    names = sorted(str(c.object.value) for c in both.claims if c.predicate == "has_name")  # type: ignore[union-attr]
    assert names == [
        "SOP-CELL-021",
        "SOP-CELL-021",
        "Service bulletin SB-2026-117: PG-80 long finger sets",
    ]
    sop_claims = {
        (c.subject, c.predicate, canonical_json.dumps(c.object.to_json())) for c in sop_alone.claims
    }
    assert sop_claims <= {
        (c.subject, c.predicate, canonical_json.dumps(c.object.to_json())) for c in both.claims
    }


def test_a_heading_named_document_without_its_effective_label_is_not_one() -> None:
    def no_issued(record: dict[str, Any]) -> None:
        record["text"]["value"] = record["text"]["value"].split("\n")[0]

    result = run(with_block(1, no_issued))
    assert not result.claims
    assert "procedures.untimed" not in codes(result)


def test_two_different_title_headings_are_not_read() -> None:
    def retitle(record: dict[str, Any]) -> None:
        record["level"] = {"knowledge": "known", "value": 1}

    result = run(with_block(2, retitle))
    assert not result.claims
    assert "procedures.title_repeated" in codes(result)


@pytest.mark.parametrize(
    ("issued", "day"),
    [("2026-08-30", ISSUED), ("2026-08-30 by the vendor", ISSUED), ("2026-08-30x", None)],
)
def test_an_issue_date_may_be_followed_by_text_after_a_space(issued: str, day: int | None) -> None:
    def redate(record: dict[str, Any]) -> None:
        first = record["text"]["value"].split("\n")[0]
        record["text"]["value"] = f"{first}\nIssued: {issued}"

    result = run(with_block(1, redate))
    if day is None:
        assert "procedures.date_unreadable" in codes(result)
        assert not result.claims
    else:
        assert {c.valid_from.ticks for c in result.claims} == {day}


def test_a_pattern_reading_no_id_involves_no_part_and_says_so() -> None:
    parts: dict[str, JsonValue] = {
        "label": "Applies to",
        "pattern": "kit (K-[0-9]+)",
        "namespace": "vendor.part",
    }
    config: dict[str, JsonValue] = {"documents": [{**DECLARATION, "parts": parts}]}
    result = run(config=config)
    assert not about(result, "involves")
    assert "procedures.part_unmatched" in codes(result)
    assert about(result, "has_name")


@pytest.mark.parametrize(
    ("parts", "problem"),
    [
        ({"label": "Applies to", "pattern": "FS-[0-9]+", "namespace": "vendor.part"}, "one group"),
        (
            {"label": "Applies to", "pattern": "(FS)-([0-9]+)", "namespace": "vendor.part"},
            "one group",
        ),
        ({"label": "Applies to", "pattern": "(FS-[", "namespace": "vendor.part"}, "compile"),
        ({"label": "Applies to", "pattern": "(x)" * 100, "namespace": "vendor.part"}, "1 to"),
        ({"label": "Applies to", "pattern": "(x)", "namespace": "Vendor Part"}, "namespace"),
        ({"label": "Applies to", "pattern": "(x)"}, "lacks"),
    ],
)
def test_a_malformed_parts_declaration_is_a_finding(
    parts: dict[str, JsonValue], problem: str
) -> None:
    config: dict[str, JsonValue] = {"documents": [{**DECLARATION, "parts": parts}]}
    _, problems = parse_config(procedure_config(config))
    assert problems and any(problem in p or "namespace" in p for p in problems), problems
    assert "procedures.invalid_config" in codes(run(config=config))


@pytest.mark.parametrize("title", [{"heading": 0}, {"heading": True}, {"level": 1}, 1])
def test_a_malformed_title_declaration_is_a_finding(title: object) -> None:
    config: dict[str, JsonValue] = {"documents": [{**DECLARATION, "procedure": title}]}  # type: ignore[dict-item]
    assert "procedures.invalid_config" in codes(run(config=config))


# --- Determinism ---------------------------------------------------------------------------------


def test_record_order_and_repeats_do_not_change_the_bulletins_claims() -> None:
    first = canonical_json.dumps(run().to_json())
    shuffled = copy.deepcopy(RECORDS)
    random.Random(11).shuffle(shuffled)
    ledger = StubLedger({"pkg:a": (1, shuffled), "pkg:b": (2, RECORDS)})
    again = run_consolidator(
        ProcedureConsolidator(), ledger, (), procedure_config(CONFIG), recorded_at=ledger_tx(1)
    )
    assert canonical_json.dumps(again.to_json()) == first
