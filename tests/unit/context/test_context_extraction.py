"""The context pass over parsed documents and tables (ADR 0063), record by record.

Records are built here as the Markdown, PDF and tabular adapters write them, so every rule is
exercised without a job: labels, requirements, steps and their bodies, registers, candidates,
findings, citations that resolve to the exact text, and determinism under reordering.
"""

import random
from typing import Any

import pytest

from neptune.context import CONTEXT_ID, context_transform, extract_context
from neptune.derived.context import CANDIDATE_KIND, ContextCandidate, context_candidate_from_json
from neptune.identity.hashing import content_id
from neptune.identity.provenance import (
    check_evidence_record_id,
    evidence_record_id,
    transform_record,
)
from neptune.model.ids import LogicalId
from neptune.model.knowledge import (
    AssertionKind,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    JsonPointer,
    Page,
    Provenance,
    Row,
    RowCell,
    Span,
)
from neptune.model.task import Requirement, SOPSection, TaskBrief, WorkOrder
from neptune.model.world import (
    Asset,
    BlockRole,
    DocumentBlock,
    DocumentRecord,
    Site,
    StructuredRecord,
    StructuredTable,
)

MARKDOWN = transform_record(adapter_id="markdown", adapter_version="1.0.0", config={})
PDF = transform_record(adapter_id="pdf", adapter_version="1.0.0", config={})
TABULAR = transform_record(adapter_id="tabular", adapter_version="1.0.0", config={})

Block = tuple[BlockRole | None, int | None, str]


def observed(evidence: EvidenceRef, transform: Any) -> Provenance:
    return Provenance(evidence, transform.id, AssertionKind.OBSERVED)


def document(
    blocks: list[Block], *, transform: Any = MARKDOWN, page: int | None = None
) -> tuple[str, list[Any]]:
    """A document as an adapter writes it: blocks joined by blank lines, each citing its span
    (after a ``Page`` step for a PDF's page). Returns the text the spans index and the records."""
    text = "\n\n".join(body for _, _, body in blocks)
    source = content_id(text.encode())
    whole = EvidenceRef(source, (ByteRange(0, len(text.encode())),))
    record = DocumentRecord(
        evidence_record_id("document_record", whole, transform),
        observed(whole, transform),
        "markdown" if page is None else "pdf",
        Unknown(),
        (),
    )
    records: list[Any] = [record]
    offset = 0
    for order, (role, level, body) in enumerate(blocks):
        span = Span(offset, offset + len(body))
        steps = (span,) if page is None else (Page(page), span)
        evidence = EvidenceRef(source, steps)
        records.append(
            DocumentBlock(
                evidence_record_id("document_block", evidence, transform),
                observed(evidence, transform),
                record.id,
                order,
                Unknown() if role is None else Known(role),
                Unknown() if level is None else Known(level),
                Known(body),
                NotApplicable(),
            )
        )
        offset += len(body) + 2
    return text, records


def table(header: list[str] | None, rows: list[list[str | None]]) -> tuple[bytes, list[Any]]:
    """A CSV as the tabular adapter writes it; ``header=None`` leaves it undeclared."""
    lines = ([header] if header is not None else []) + [[c or "" for c in row] for row in rows]
    data = "\r\n".join(",".join(line) for line in lines).encode()
    source = content_id(data)
    whole = EvidenceRef(source, (ByteRange(0, len(data)),))
    declared = (
        Known(tuple(header), observed(EvidenceRef(source, (Row(0),)), TABULAR))
        if header is not None
        else Unknown()
    )
    record = StructuredTable(
        evidence_record_id("structured_table", whole, TABULAR),
        observed(whole, TABULAR),
        NotCovered(),
        declared,
    )
    records: list[Any] = [record]
    first = 1 if header is not None else 0
    for index, row in enumerate(rows):
        evidence = EvidenceRef(source, (Row(first + index),))
        records.append(
            StructuredRecord(
                evidence_record_id("structured_record", evidence, TABULAR),
                observed(evidence, TABULAR),
                record.id,
                first + index,
                tuple(Unknown() if cell is None else Known(cell) for cell in row),
            )
        )
    return data, records


def of(found: Any, kind: type) -> list[Any]:
    return [record for record in found.records if isinstance(record, kind)]


def spanned(text: str, evidence: EvidenceRef) -> str:
    span = evidence.locator[-1]
    assert isinstance(span, Span)
    return text[span.start : span.end]


SOP: list[Block] = [
    (BlockRole.HEADING, 1, "Cell 4 gripper change"),
    (BlockRole.PARAGRAPH, None, "Procedure ID: SOP-CELL-04\nSite: PLANT-2\nAsset: ARM-4"),
    (BlockRole.HEADING, 2, "Step 1: Lock out the cell"),
    (BlockRole.PARAGRAPH, None, "Press the e-stop."),
    (BlockRole.HEADING, 2, "Step 2: Change the gripper"),
    (BlockRole.LIST_ITEM, 1, "Requirement CELL-SAF-3: The operator shall wear gloves."),
    (BlockRole.HEADING, 3, "Torque"),
    (BlockRole.PARAGRAPH, None, "Tighten to 9 N·m."),
    (BlockRole.HEADING, 2, "3. Restore power"),
    (BlockRole.PARAGRAPH, None, "The cell must be cleared first."),
    (BlockRole.CODE, None, "Step 9: not a step"),
]


def test_a_procedure_gives_its_steps_their_bodies_and_its_requirements() -> None:
    text, records = document(SOP)
    found = extract_context(records)
    assert found is not None
    blocks = [r for r in records if isinstance(r, DocumentBlock)]
    steps = sorted(of(found, SOPSection), key=lambda s: s.order)
    assert [(s.number.value, s.title.value) for s in steps] == [
        ("1", "Lock out the cell"),
        ("2", "Change the gripper"),
    ]
    procedure = LogicalId("procedure", "SOP-CELL-04")
    assert all(s.procedure.value == procedure for s in steps)
    # A step runs to the next step or a heading at its level; a deeper heading is inside it.
    assert steps[0].blocks == (blocks[2].id, blocks[3].id)
    assert steps[1].blocks == tuple(b.id for b in blocks[4:8])
    for step in steps:
        assert spanned(text, step.number.provenance.evidence) == step.number.value
        assert spanned(text, step.title.provenance.evidence) == step.title.value
    (requirement,) = of(found, Requirement)
    assert requirement.text.value == "The operator shall wear gloves."
    assert spanned(text, requirement.text.provenance.evidence) == requirement.text.value
    assert spanned(text, requirement.identifiers[0].provenance.evidence) == "CELL-SAF-3"
    assert isinstance(requirement.task, Unknown)  # the procedure states no task
    assert not of(found, TaskBrief)  # a procedure is not a brief
    # The numbered heading and the "must" sentence are candidates, never records; code is not read.
    candidates = {(c.proposes, c.rule, c.text) for c in found.candidates}
    assert candidates == {
        ("sop_section", "numbered_heading_in_procedure", "3. Restore power"),
        ("requirement", "modal_sentence_without_label", "The cell must be cleared first."),
    }


def test_every_record_is_stated_by_its_context_transform_with_the_id_rule() -> None:
    _, records = document(SOP)
    found = extract_context(records)
    assert found is not None
    (transform,) = found.transforms
    assert transform == context_transform(MARKDOWN.id)
    assert transform.adapter_id == CONTEXT_ID
    assert transform.upstream == (MARKDOWN.id,)
    for record in found.records:
        assert record.provenance.assertion_kind is AssertionKind.STATED
        check_evidence_record_id(record, transform)
    for candidate in found.candidates:
        assert candidate.transform == transform.id
        assert context_candidate_from_json(candidate.to_json()) == candidate
    assert set(found.tables()) == {CANDIDATE_KIND}


BRIEF: list[Block] = [
    (BlockRole.HEADING, 1, "Thermal inspection brief"),
    (
        BlockRole.PARAGRAPH,
        None,
        "Task ID: TB-2026-117\nTitle: String inspection\nSite: SOLAR-FARM-2\n"
        "Assets: STR-14, STR-15\nRobot: UAV-M30-03\nObjective: Image strings 14 and 15.",
    ),
    (
        BlockRole.PARAGRAPH,
        None,
        "Requirement INS-1: The aircraft shall hold 25 m.\nREQ INS-2:  Two images per panel. ",
    ),
]


@pytest.mark.parametrize("page", [None, 3])
def test_a_brief_states_its_task_and_every_value_cites_its_exact_text(page: int | None) -> None:
    text, records = document(BRIEF, transform=PDF if page is not None else MARKDOWN, page=page)
    found = extract_context(records)
    assert found is not None
    (task,) = of(found, TaskBrief)
    assert task.identifiers[0].value == LogicalId("task", "TB-2026-117")
    assert task.name.value == "String inspection"
    assert task.objective.value == "Image strings 14 and 15."
    assert task.site.value == LogicalId("site", "SOLAR-FARM-2")
    assert [a.value.value for a in task.assets] == ["STR-14", "STR-15"]
    assert [m.value for m in task.machines] == [LogicalId("machine", "UAV-M30-03")]
    cited = [task.name, task.objective, task.site, *task.assets, *task.machines, *task.identifiers]
    for state in cited:
        evidence = state.provenance.evidence
        if page is not None:
            assert evidence.locator[0] == Page(page)  # a PDF citation keeps its page
        value = state.value.value if isinstance(state.value, LogicalId) else state.value
        assert spanned(text, evidence) == value
    requirements = sorted(of(found, Requirement), key=lambda r: r.identifiers[0].value.value)
    assert [r.text.value for r in requirements] == [
        "The aircraft shall hold 25 m.",
        "Two images per panel.",
    ]
    assert all(r.task.value == LogicalId("task", "TB-2026-117") for r in requirements)
    assert all(r.declared_in == records[0].id for r in requirements)
    assert not found.candidates  # labelled sentences are never also candidates


def test_a_work_order_names_its_task_and_never_declares_it() -> None:
    _, records = document(
        [
            (BlockRole.HEADING, 1, "Replace drive wheel"),
            (
                BlockRole.PARAGRAPH,
                None,
                "Work Order: WO-5531\nStatus: Open\nAsset: AMR-07\nTask ID: PICK-NIGHT",
            ),
        ]
    )
    found = extract_context(records)
    assert found is not None
    (order,) = of(found, WorkOrder)
    assert order.identifiers[0].value == LogicalId("work_order", "WO-5531")
    assert order.status.value == "Open"
    assert order.task.value == LogicalId("task", "PICK-NIGHT")
    assert isinstance(order.site, Unknown)
    assert not of(found, TaskBrief)


def test_malformed_labels_are_findings_and_never_failures() -> None:
    _, records = document(
        [
            (BlockRole.PARAGRAPH, None, "Task ID: ORCH-5\nSite: NORTH\nSite: SOUTH\nSite: NORTH"),
            (BlockRole.PARAGRAPH, None, "Requirement AG-1:"),
            (
                BlockRole.PARAGRAPH,
                None,
                "Requirement: no id here\nRequirement ID: R-9\nNot a label: at all",
            ),
            (BlockRole.TABLE, None, "| Requirement R-7: in a table | Site: X |"),
        ]
    )
    found = extract_context(records)
    assert found is not None
    codes = sorted(f.code for f in found.findings)
    assert codes == ["context.label_repeated", "context.requirement_without_text"]
    (task,) = of(found, TaskBrief)
    assert task.site.value == LogicalId("site", "NORTH")  # the first, with a finding
    (requirement,) = of(found, Requirement)
    assert isinstance(requirement.text, Unknown)
    repeated = next(f for f in found.findings if f.code == "context.label_repeated")
    assert repeated.details["values"] == ["NORTH", "SOUTH"]


def test_a_document_that_declares_nothing_writes_nothing() -> None:
    _, records = document([(BlockRole.PARAGRAPH, None, "Plain notes about the pump.")])
    assert extract_context(records) is None
    assert extract_context([]) is None


REGISTER = ["Asset ID", "Name", "Category", "Site ID", "Aliases", "Serial Number", "CMMS ID"]


def test_a_register_row_is_an_asset_citing_each_cell() -> None:
    data, records = table(
        REGISTER,
        [
            ["AMR-07", "Tugger 7", "AMR", "WH-3", "Otto 7; T7", "OTTO-42", "EQ-4410"],
            ["RACK-A1", "Rack A1", "rack", "WH-3", None, None, None],
            [None, None, "unlabelled", "WH-3", None, None, None],
        ],
    )
    found = extract_context(records)
    assert found is not None
    assets = {a.identifiers[0].value.value: a for a in of(found, Asset)}
    assert set(assets) == {"AMR-07", "RACK-A1"}
    tugger = assets["AMR-07"]
    assert [i.value for i in tugger.identifiers] == [
        LogicalId("asset", "AMR-07"),
        LogicalId("cmms", "EQ-4410"),
        LogicalId("serial", "OTTO-42"),
    ]
    assert tugger.site.value == LogicalId("site", "WH-3")
    assert tugger.provenance.evidence.locator == (Row(1),)
    assert tugger.category.provenance.evidence.locator == (RowCell(1, 2, "Category"),)
    # An alias split out of a cell cites its span inside that cell (ADR 0020 §1).
    cell = data.decode().split("\r\n")[1].split(",")[4]
    for alias in tugger.aliases:
        step, span = alias.provenance.evidence.locator
        assert step == RowCell(1, 4, "Aliases")
        assert cell[span.start : span.end] == alias.value
    assert [a.value for a in tugger.aliases] == ["Otto 7", "T7"]
    # The register has no parent or position columns: not covered, never blank.
    assert isinstance(tugger.parent, NotCovered)
    assert isinstance(tugger.location, NotCovered)
    assert isinstance(assets["RACK-A1"].aliases, tuple) and not assets["RACK-A1"].aliases
    assert [f.code for f in found.findings] == ["context.unnamed_declaration"]


def test_a_requirements_table_and_a_site_register_with_positions() -> None:
    _, rows = table(
        ["requirement_id", "text", "task_id"],
        [["ROV-R1", "The ROV shall stay 1 m off the hull.", "HULL-9"]],
    )
    _, sites = table(
        ["site_id", "name", "latitude", "longitude", "crs", "parent_id"],
        [
            ["BERTH-12", "Berth 12", "-41.2865", "174.7762", "EPSG:4326", "PORT-1"],
            ["BERTH-13", "Berth 13", "41°N", "174.7", None, None],
        ],
    )
    found = extract_context([*rows, *sites])
    assert found is not None
    (requirement,) = of(found, Requirement)
    assert requirement.text.value == "The ROV shall stay 1 m off the hull."
    assert requirement.task.value == LogicalId("task", "HULL-9")
    berths = {s.identifiers[0].value.value: s for s in of(found, Site)}
    position = berths["BERTH-12"].location.value
    assert (position.latitude, position.longitude) == (-41.2865, 174.7762)
    assert position.crs.value.authority == "EPSG" and position.crs.value.code == "4326"
    assert isinstance(position.angle_unit, Unknown)  # never assumed degrees
    assert isinstance(position.height, NotCovered)
    assert berths["BERTH-12"].parent.value == LogicalId("site", "PORT-1")
    assert isinstance(berths["BERTH-13"].location, Unknown)
    assert [f.code for f in found.findings] == ["context.coordinate_not_decimal"]


def test_an_undeclared_header_is_a_candidate_never_a_register() -> None:
    _, records = table(None, [["asset_id", "name"], ["AMR-07", "Tugger 7"]])
    found = extract_context(records)
    assert found is not None
    assert not found.records
    (candidate,) = found.candidates
    assert isinstance(candidate, ContextCandidate)
    assert (candidate.proposes, candidate.rule, candidate.text) == (
        "asset",
        "undeclared_header_names_register",
        "asset_id",
    )


def test_a_json_table_names_columns_by_each_cells_key() -> None:
    source = content_id(b'[{"work_order_id": "WO-9", "status": "Open", "site_id": null}]')
    whole = EvidenceRef(source, (ByteRange(0, 60),))
    row_at = EvidenceRef(source, (ByteRange(1, 58),))
    tbl = StructuredTable(
        evidence_record_id("structured_table", whole, TABULAR),
        observed(whole, TABULAR),
        NotCovered(),
        NotApplicable(),
    )

    def cell(key: str) -> EvidenceRef:
        return EvidenceRef(source, (ByteRange(1, 58), JsonPointer(f"/{key}")))

    row = StructuredRecord(
        evidence_record_id("structured_record", row_at, TABULAR),
        observed(row_at, TABULAR),
        tbl.id,
        0,
        (
            Known("WO-9", observed(cell("work_order_id"), TABULAR)),
            Known("Open", observed(cell("status"), TABULAR)),
            KnownAbsent(observed(cell("site_id"), TABULAR)),
        ),
    )
    found = extract_context([tbl, row])
    assert found is not None
    (order,) = of(found, WorkOrder)
    assert order.identifiers[0].value == LogicalId("work_order", "WO-9")
    assert order.status.provenance.evidence == cell("status")
    assert isinstance(order.site, KnownAbsent)  # JSON null: the format defines it as none


def test_the_same_records_in_any_order_give_the_same_output() -> None:
    _, sop = document(SOP)
    _, brief = document(BRIEF)
    _, register = table(REGISTER, [["AMR-07", "Tugger 7", "AMR", "WH-3", None, None, None]])
    records = [*sop, *brief, *register]
    first = extract_context(records)
    shuffled = list(records)
    random.Random(7).shuffle(shuffled)
    again = extract_context(shuffled)
    assert first is not None and again is not None
    assert [r.to_json() for r in first.records] == [r.to_json() for r in again.records]
    assert first.candidates == again.candidates
    assert first.findings == again.findings
    assert first.transforms == again.transforms  # one per upstream transform, sorted
    assert {t.upstream for t in first.transforms} == {(MARKDOWN.id,), (TABULAR.id,)}


def test_a_hostile_line_costs_linear_time() -> None:
    long = "Step 1" + "." * 200_000 + "\nTask ID" + " " * 200_000 + ": X" + "a" * 50
    _, records = document([(BlockRole.PARAGRAPH, None, long), (None, None, "shall " * 50_000)])
    found = extract_context(records)  # bounded patterns: linear in the line, never backtracking
    assert found is not None
