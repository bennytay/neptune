"""Maintenance events, status reports and stated causes as events (ADR 0025 §1-§3).

Real compiler and Deploy records from the acceptance corpus
(``fixtures/declared_cell3.records.jsonl``): work order WO-26-0911 (a CMMS row with four actions),
incident INC-C3-0011 and two of the ``/diagnostics`` statuses of the 2026-09-14 run (a WARN and an
ERROR).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from neptune.model.ids import RecordId
from neptune.model.knowledge import Unknown
from neptune_memory.consolidate.base import run_consolidator
from neptune_memory.consolidate.event_records import resolve_config
from neptune_memory.consolidate.events import EventConsolidator, event_node
from neptune_memory.ledger import StubLedger
from neptune_memory.schema.claim import DeclaredType, DeclaredValue, TypedLiteral, ValueType
from neptune_memory.schema.interval import ledger_tx

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.consolidate.base import Consolidation
    from neptune_memory.schema.nodes import NodeRef

FIXTURE: Final = Path(__file__).parent / "fixtures" / "declared_cell3.records.jsonl"
RECORDS: Final[list[dict[str, Any]]] = [
    json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()
]
WORK_ORDER: Final = "rec:sha256:b44143d7beb57db6283c8a49f1cc6ee41c8ba916f63c4adc233f992e90eaabc6"
INCIDENT: Final = "rec:sha256:e324b7d05a41ad13565856405fcbc1a6b2ef978f8f8bb31ad1ace86a4930a4cc"
WARN: Final = "rec:sha256:076ee1272c7a003e4f93ff12d8fdafa44fb466d32942bc19053b7d8ce4c35466"
VENDORS: Final[dict[str, JsonValue]] = {
    "ros_diagnostic_status": {"text": {"ERROR": "fault", "STALE": "stale", "WARN": "warning"}}
}


def consolidate(
    records: list[dict[str, Any]], vendors: dict[str, JsonValue] | None = None
) -> Consolidation:
    ledger = StubLedger({"sha256:" + "a" * 64: (1, records)})
    config = resolve_config({"vendors": vendors if vendors is not None else VENDORS})
    return run_consolidator(EventConsolidator(), ledger, [], config, recorded_at=ledger_tx(1))


def facts(result: Consolidation, node: NodeRef) -> dict[str, set[object]]:
    out: dict[str, set[object]] = {}
    for c in result.claims:
        if c.subject == node:
            obj = c.object
            value = obj.value if isinstance(obj, TypedLiteral) else getattr(obj, "node_id", obj)
            out.setdefault(c.predicate, set()).add(value)
    return out


def node(record: str, *path: str | int) -> NodeRef:
    return event_node(RecordId(record), *path)


def test_a_work_order_is_a_maintenance_event_with_one_event_per_action() -> None:
    result = consolidate(RECORDS)
    main = facts(result, node(WORK_ORDER))
    assert main["event_kind"] == {"maintenance"}
    assert main["stated_cause"] == {"Finger pads worn; wrist camera bracket loose"}
    assert main["involves"] == {"cmms.asset:ARM-3A", "serial:FS-0291", "serial:FS-0340"}
    assert main["at_site"] == {"cmms.site:CELL-3"}
    assert main["has_name"] == {"WO-26-0911"}  # its declared number, verbatim (ADR 0026)
    assert "declared_kind" not in main  # the record states no kind of its own
    actions = [facts(result, node(WORK_ORDER, "actions", i)) for i in range(4)]
    assert [a["has_description"] for a in actions] == [
        {"Replace finger set with long set FS-0340 (+6.0 mm)"},
        {"Remove and refit wrist camera bracket"},
        {"Set TCP z 145.5 -> 151.5 mm on pendant (tool1)"},
        {"Hand-eye recalibration deferred: ChArUco board out for repair"},
    ]
    assert all(a["involves"] == {"cmms.asset:ARM-3A"} for a in actions)
    assert all("has_name" not in a for a in actions)
    claims = [c for c in result.claims if c.subject.node_id.startswith(node(WORK_ORDER).node_id)]
    assert {c.assertion_kind.value for c in claims} == {"stated"}  # type: ignore[union-attr]
    # Each action is cited at its own span of the "Work Performed" cell.
    (described,) = [
        c
        for c in result.claims
        if c.subject == node(WORK_ORDER, "actions", 1) and c.predicate == "has_description"
    ]
    spans = [step.to_json() for ref in described.provenance.evidence for step in ref.locator]
    assert {"end": 89, "kind": "span", "start": 52} in spans


def test_a_maintenance_event_with_no_machine_involves_none_and_says_so() -> None:
    records = copy.deepcopy(RECORDS)
    work_order = next(r for r in records if r.get("id") == WORK_ORDER)
    work_order["machines"] = {"knowledge": "unknown"}
    result = consolidate(records)
    assert "cmms.asset:ARM-3A" not in facts(result, node(WORK_ORDER)).get("involves", set())
    assert "involves" not in facts(result, node(WORK_ORDER, "actions", 0))
    assert "events.machine_unstated" in {f.code for f in result.findings}


def test_a_maintenance_event_with_no_time_is_not_placed() -> None:
    records = copy.deepcopy(RECORDS)
    work_order = next(r for r in records if r.get("id") == WORK_ORDER)
    work_order["performed"] = {"knowledge": "unknown"}
    result = consolidate(records)
    assert not facts(result, node(WORK_ORDER))
    assert not facts(result, node(WORK_ORDER, "actions", 0))
    assert "events.untimed_event" in {f.code for f in result.findings}


def test_a_status_report_is_an_observed_event_with_its_values() -> None:
    result = consolidate(RECORDS)
    warn = facts(result, node(WARN))
    assert warn["event_kind"] == {"warning"}
    assert warn["declared_kind"] == {"WARN"}
    assert warn["has_description"] == {"hand-eye pick residual 3.1 mm (limit 2.0 mm)"}
    assert warn["declared_value"] == {
        DeclaredValue(("residual_mm",), DeclaredType.TEXT, "3.1"),
        DeclaredValue(("calibration",), DeclaredType.TEXT, "CAL-ARM3A-0911"),
    }
    claims = [c for c in result.claims if c.subject == node(WARN)]
    # What the message says is observed; the level's name is the stream definition's, and the
    # event kind the vendor mapping's: both stated.
    kinds = {c.predicate: c.assertion_kind.value for c in claims}  # type: ignore[union-attr]
    assert kinds == {
        "declared_kind": "stated",
        "declared_value": "observed",
        "event_kind": "stated",
        "evidenced_by": "observed",
        "has_description": "observed",
    }
    # Placed at its first stated time, on its stream's first clock.
    first = next(r for r in RECORDS if r.get("id") == WARN)["times"][0]["value"]
    assert {(c.valid_from.domain_id, c.valid_from.ticks) for c in claims} == {
        (first["domain_id"], first["ticks"])
    }


def test_an_unmapped_status_level_keeps_its_declared_kind_and_no_event_kind() -> None:
    result = consolidate(RECORDS, vendors={})
    warn = facts(result, node(WARN))
    assert "event_kind" not in warn
    assert warn["declared_kind"] == {"WARN"}
    assert "events.kind_unmapped" in {f.code for f in result.findings}


def test_a_level_mapped_to_not_an_event_is_no_event() -> None:
    vendors: dict[str, JsonValue] = {"ros_diagnostic_status": {"text": {"WARN": "not_an_event"}}}
    result = consolidate(RECORDS, vendors=vendors)
    assert not facts(result, node(WARN))


def test_an_integer_status_value_has_no_unit_stated() -> None:
    records = copy.deepcopy(RECORDS)
    warn = next(r for r in records if r.get("id") == WARN)
    warn["values"] = [{"key": "count", "value": 3}]
    result = consolidate(records)
    (claim,) = [
        c for c in result.claims if c.subject == node(WARN) and c.predicate == "declared_value"
    ]
    assert claim.object == TypedLiteral(
        ValueType.DECLARED_VALUE, DeclaredValue(("count",), DeclaredType.INTEGER, 3), Unknown()
    )


def test_an_incidents_root_cause_is_its_stated_cause() -> None:
    result = consolidate(RECORDS)
    (cause,) = facts(result, node(INCIDENT))["stated_cause"]
    assert isinstance(cause, str) and cause.startswith("Under investigation. The fingers closed")


def test_a_malformed_status_report_is_a_finding_and_the_rest_is_claimed() -> None:
    records = copy.deepcopy(RECORDS)
    warn = next(r for r in records if r.get("id") == WARN)
    warn["times"] = []
    result = consolidate(records)
    assert "events.malformed_record" in {f.code for f in result.findings}
    assert facts(result, node(WORK_ORDER))


def test_events_are_deterministic_whatever_the_record_order() -> None:
    first, second = consolidate(RECORDS), consolidate(list(reversed(RECORDS)))
    assert [c.id for c in first.claims] == [c.id for c in second.claims]
    assert [f.id for f in first.findings] == [f.id for f in second.findings]
