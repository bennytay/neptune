"""Task records (ADR 0063): shape, strict readers, the schema, versions, and what they refuse."""

from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from neptune.derived.provenance import InferredProvenance
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.ids import LogicalId, RecordId
from neptune.model.kinds import RECORD_KINDS, kinds_at, package_version
from neptune.model.knowledge import AssertionKind, Known, NotCovered, Unknown
from neptune.model.provenance import EvidenceRef, Page, Provenance, Span
from neptune.model.record import Family, SchemaVersionError
from neptune.model.schema import canonical_schema
from neptune.model.task import (
    TASK_SINCE,
    Requirement,
    SOPSection,
    TaskBrief,
    WorkOrder,
    requirement_from_json,
    sop_section_from_json,
    task_brief_from_json,
    work_order_from_json,
)

BRIEF = "Task ID: TB-117\nSite: SOLAR-2\nRequirement INS-1: The aircraft shall hold 25 m."
SOURCE = content_id(BRIEF.encode())
UPSTREAM = transform_record(adapter_id="markdown", adapter_version="1.0.0", config={})
CONTEXT = transform_record(
    adapter_id="neptune.context", adapter_version="0.1.0", config={}, upstream=[UPSTREAM.id]
)
VALIDATOR = Draft202012Validator(canonical_schema())


def at(start: int, end: int, page: int | None = None) -> EvidenceRef:
    steps = (Span(start, end),) if page is None else (Page(page), Span(start, end))
    return EvidenceRef(SOURCE, steps)


def cite(start: int, end: int, page: int | None = None) -> Provenance:
    return Provenance(at(start, end, page), CONTEXT.id, AssertionKind.STATED)


def rid(kind: str, start: int, end: int) -> RecordId:
    return evidence_record_id(kind, at(start, end), CONTEXT)


DOCUMENT = evidence_record_id("document_record", at(0, len(BRIEF)), UPSTREAM)
BLOCKS = tuple(evidence_record_id("document_block", at(i, i + 1), UPSTREAM) for i in range(3))


def ident(namespace: str, value: str, start: int, end: int) -> Known[LogicalId]:
    return Known(LogicalId(namespace, value), cite(start, end))


def brief() -> TaskBrief:
    return TaskBrief(
        rid("task_brief", 0, 15),
        cite(0, 15),
        DOCUMENT,
        (ident("task", "TB-117", 9, 15),),
        Unknown(),
        NotCovered(),
        Known(LogicalId("site", "SOLAR-2"), cite(22, 29)),
        (ident("asset", "STR-14", 30, 36), ident("asset", "STR-15", 38, 44)),
        (),
    )


def requirement() -> Requirement:
    return Requirement(
        rid("requirement", 30, 82),
        cite(30, 82),
        DOCUMENT,
        (ident("requirement", "INS-1", 42, 47),),
        Known("The aircraft shall hold 25 m.", cite(49, 82)),
        Known(LogicalId("task", "TB-117"), cite(9, 15)),
    )


def section() -> SOPSection:
    return SOPSection(
        rid("sop_section", 0, 20),
        cite(0, 20),
        DOCUMENT,
        Unknown(),
        Known("4.2", cite(5, 8)),
        Known("Lock out the cell", cite(10, 27)),
        0,
        BLOCKS,
    )


def order() -> WorkOrder:
    return WorkOrder(
        rid("work_order", 0, 18),
        cite(0, 18),
        DOCUMENT,
        (ident("work_order", "WO-5531", 12, 19),),
        Unknown(),
        Known("Open", cite(28, 32)),
        Unknown(),
        (ident("asset", "AMR-07", 40, 46),),
        Known(LogicalId("task", "PICK-NIGHT"), cite(57, 67)),
    )


CASES = [
    (brief, task_brief_from_json),
    (requirement, requirement_from_json),
    (section, sop_section_from_json),
    (order, work_order_from_json),
]


@pytest.mark.parametrize(("make", "read"), CASES)
def test_each_kind_round_trips_byte_identically_and_validates(make: Any, read: Any) -> None:
    record = make()
    data = record.to_json()
    VALIDATOR.validate(data)
    again = read(canonical_json.loads(canonical_json.dumps(data)))
    assert again == record
    assert canonical_json.dumps(again.to_json()) == canonical_json.dumps(data)


@pytest.mark.parametrize(("make", "read"), CASES)
def test_each_kind_is_a_task_record_written_at_its_version(make: Any, read: Any) -> None:
    record: Any = make()
    assert record.family is Family.TASK
    assert record.to_json()["schema_version"] == TASK_SINCE
    assert record.kind not in kinds_at(TASK_SINCE - 1)
    assert record.kind in kinds_at(TASK_SINCE)
    assert package_version([record.kind, "site", "asset"]) == TASK_SINCE
    assert RECORD_KINDS[record.kind][0] is type(record)
    assert package_version(["site", "asset"]) == 1  # a register-only package keeps its bytes


@pytest.mark.parametrize(("make", "read"), CASES)
def test_readers_refuse_extra_or_missing_keys_and_wrong_versions(make: Any, read: Any) -> None:
    data = dict(make().to_json())
    with pytest.raises(ValueError):
        read({**data, "extra": 1})
    with pytest.raises(ValueError):
        read({key: value for key, value in data.items() if key != "declared_in"})
    with pytest.raises(SchemaVersionError):
        read({**data, "schema_version": 99})
    with pytest.raises(ValueError):
        read({**data, "schema_version": TASK_SINCE - 1})  # older than the kind


@pytest.mark.parametrize(("make", "read"), CASES)
def test_inferred_task_records_belong_in_derived(make: Any, read: Any) -> None:
    record = make()
    inferred: Any = InferredProvenance((record.provenance.evidence,), CONTEXT.id)
    with pytest.raises(TypeError):
        replace(record, provenance=inferred)


def test_a_declaration_names_what_it_declares() -> None:
    with pytest.raises(ValueError, match="names what it declares"):
        replace(brief(), identifiers=(), name=Unknown())
    assert replace(brief(), identifiers=(), name=Known("Night shift", cite(0, 5))).identifiers == ()
    with pytest.raises(ValueError, match="names what it declares"):
        replace(requirement(), identifiers=(), text=Unknown())
    with pytest.raises(ValueError, match="names what it declares"):
        replace(order(), identifiers=(), name=Unknown())


def test_declared_lists_are_stated_unique_and_sorted() -> None:
    first, second = brief().assets
    with pytest.raises(ValueError, match="sorted"):
        replace(brief(), assets=(second, first))
    with pytest.raises(ValueError, match="repeat"):
        replace(brief(), machines=(first, first))
    with pytest.raises(ValueError, match="Known or Ambiguous"):
        replace(brief(), machines=(Unknown(),))


def test_a_step_spans_its_blocks_once_in_order() -> None:
    with pytest.raises(ValueError, match="at least its own"):
        replace(section(), blocks=())
    with pytest.raises(ValueError, match="each block once"):
        replace(section(), blocks=(BLOCKS[0], BLOCKS[0]))
    with pytest.raises(ValueError, match="at least 0"):
        replace(section(), order=-1)
    with pytest.raises(TypeError):
        replace(section(), order=True)
    with pytest.raises(ValueError):
        replace(section(), number=Known("", cite(0, 0)))  # a blank is Unknown, never ""


def test_references_are_declared_ids_never_text() -> None:
    with pytest.raises(ValueError, match="LogicalId"):
        replace(order(), site=Known("WH-3", cite(0, 4)))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="LogicalId"):
        replace(requirement(), task=Known("TB-117", cite(9, 15)))  # type: ignore[arg-type]


def test_a_pdf_citation_keeps_its_page() -> None:
    on_page = replace(requirement(), text=Known("The aircraft shall hold 25 m.", cite(4, 33, 2)))
    data = on_page.to_json()
    VALIDATOR.validate(data)
    assert requirement_from_json(data) == on_page
