"""The declared-records pass over parsed documents and tables (ADR 0063), record by record.

Records are built here as the Markdown, PDF and tabular adapters write them, so every rule is
exercised without a job: labels, requirements, steps and their bodies, registers, candidates,
findings, citations that resolve to the exact text, and determinism under reordering.
"""

import random
from typing import Any

import pytest

from neptune.declared import DECLARED_ID, declared_transform, extract_declared
from neptune.derived.declared import CANDIDATE_KIND, DeclaredCandidate, declared_candidate_from_json
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
    found = extract_declared(records)
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


def test_every_record_is_stated_by_its_declared_transform_with_the_id_rule() -> None:
    _, records = document(SOP)
    found = extract_declared(records)
    assert found is not None
    (transform,) = found.transforms
    assert transform == declared_transform(MARKDOWN.id)
    assert transform.adapter_id == DECLARED_ID
    assert transform.upstream == (MARKDOWN.id,)
    for record in found.records:
        assert record.provenance.assertion_kind is AssertionKind.STATED
        check_evidence_record_id(record, transform)
    for candidate in found.candidates:
        assert candidate.transform == transform.id
        assert declared_candidate_from_json(candidate.to_json()) == candidate
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
    found = extract_declared(records)
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
    found = extract_declared(records)
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
    found = extract_declared(records)
    assert found is not None
    codes = sorted(f.code for f in found.findings)
    assert codes == ["declared.label_repeated", "declared.requirement_without_text"]
    (task,) = of(found, TaskBrief)
    assert task.site.value == LogicalId("site", "NORTH")  # the first, with a finding
    (requirement,) = of(found, Requirement)
    assert isinstance(requirement.text, Unknown)
    repeated = next(f for f in found.findings if f.code == "declared.label_repeated")
    assert repeated.details["values"] == ["NORTH", "SOUTH"]


def test_a_document_that_declares_nothing_writes_nothing() -> None:
    _, records = document([(BlockRole.PARAGRAPH, None, "Plain notes about the pump.")])
    assert extract_declared(records) is None
    assert extract_declared([]) is None


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
    found = extract_declared(records)
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
    assert [f.code for f in found.findings] == ["declared.unnamed_declaration"]


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
    found = extract_declared([*rows, *sites])
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
    assert [f.code for f in found.findings] == ["declared.coordinate_not_decimal"]


def test_an_undeclared_header_is_a_candidate_never_a_register() -> None:
    _, records = table(None, [["asset_id", "name"], ["AMR-07", "Tugger 7"]])
    found = extract_declared(records)
    assert found is not None
    assert not found.records
    (candidate,) = found.candidates
    assert isinstance(candidate, DeclaredCandidate)
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
    found = extract_declared([tbl, row])
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
    first = extract_declared(records)
    shuffled = list(records)
    random.Random(7).shuffle(shuffled)
    again = extract_declared(shuffled)
    assert first is not None and again is not None
    assert [r.to_json() for r in first.records] == [r.to_json() for r in again.records]
    assert first.candidates == again.candidates
    assert first.findings == again.findings
    assert first.transforms == again.transforms  # one per upstream transform, sorted
    assert {t.upstream for t in first.transforms} == {(MARKDOWN.id,), (TABULAR.id,)}


def test_a_hostile_line_costs_linear_time() -> None:
    long = "Step 1" + "." * 200_000 + "\nTask ID" + " " * 200_000 + ": X" + "a" * 50
    _, records = document([(BlockRole.PARAGRAPH, None, long), (None, None, "shall " * 50_000)])
    found = extract_declared(records)  # bounded patterns: linear in the line, never backtracking
    assert found is not None


def test_hostile_numbers_and_codes_cost_findings_never_the_job() -> None:
    _, sites = table(
        ["site_id", "latitude", "longitude", "crs"],
        [
            ["S-1", "9" * 400, "1.0", "EPSG:4326"],  # a double reads it as infinite
            ["S-2", "1.0", "2.0", "EPSG:" + "7" * 80],  # longer than any registry code
        ],
    )
    found = extract_declared(sites)
    assert found is not None
    located = {s.identifiers[0].value.value: s.location for s in of(found, Site)}
    assert isinstance(located["S-1"], Unknown)
    assert isinstance(located["S-2"].value.crs, Unknown)
    assert sorted(f.code for f in found.findings) == [
        "declared.coordinate_not_decimal",
        "declared.crs_not_a_code",
    ]


def test_typed_numbers_are_coordinates_and_a_task_register_lists_assets() -> None:
    source = content_id(b"parquet")
    whole = EvidenceRef(source, (ByteRange(0, 7),))
    header: Known[tuple[str, ...]] = Known(("asset_id", "latitude", "longitude"))
    tbl = StructuredTable(
        evidence_record_id("structured_table", whole, TABULAR),
        observed(whole, TABULAR),
        NotCovered(),
        header,
    )
    at = EvidenceRef(source, (Row(0),))
    row = StructuredRecord(
        evidence_record_id("structured_record", at, TABULAR),
        observed(at, TABULAR),
        tbl.id,
        0,
        (Known("A-1"), Known(51.5), Known(-0.1)),
    )
    _, tasks = table(["task_id", "asset_id", "name"], [["T-1", "A-1; A-2", "Pick"]])
    found = extract_declared([tbl, row, *tasks])
    assert found is not None
    (asset,) = of(found, Asset)
    assert (asset.location.value.latitude, asset.location.value.longitude) == (51.5, -0.1)
    (task,) = of(found, TaskBrief)
    assert task.name.value == "Pick"
    assert [a.value.value for a in task.assets] == ["A-1", "A-2"]
    assert not found.findings


def test_a_step_number_is_kept_whole_and_needs_a_separator() -> None:
    _, records = document(
        [
            (BlockRole.HEADING, 2, "Step 4.2 Calibrate gripper"),  # no separator: no step
            (BlockRole.PARAGRAPH, None, "Step 3 is optional"),
            (BlockRole.HEADING, 2, "Step 4.2: Calibrate gripper"),
            (BlockRole.HEADING, 2, "Step 5. Home the arm"),
            (BlockRole.HEADING, 2, "Step 6.1.3 - Verify"),
        ]
    )
    found = extract_declared(records)
    assert found is not None
    steps = sorted(of(found, SOPSection), key=lambda s: s.order)
    assert [(s.number.value, s.title.value) for s in steps] == [
        ("4.2", "Calibrate gripper"),
        ("5", "Home the arm"),
        ("6.1.3", "Verify"),
    ]


def test_a_requirement_id_has_a_digit() -> None:
    _, records = document(
        [
            (
                BlockRole.PARAGRAPH,
                None,
                "Requirement type: Functional\nReq coverage: 80%\nRequirement summary: see above\n"
                "Requirement owner: J. Smith\nREQ R2D2: Keep the droid charged.",
            )
        ]
    )
    found = extract_declared(records)
    assert found is not None
    assert [r.identifiers[0].value.value for r in of(found, Requirement)] == ["R2D2"]


@pytest.mark.parametrize("page", [None, 2])
def test_a_wrapped_statement_is_joined_and_cites_its_whole_span(page: int | None) -> None:
    text, records = document(
        [
            (
                None if page is not None else BlockRole.LIST_ITEM,
                None,
                "Requirement R-1: The arm shall stop\nwithin 200 ms of a stop request.\n"
                "Requirement R-2: The gripper shall\nopen on loss of air.",
            ),
            (None, None, "Step 2: Change the\ngripper fingers"),
        ],
        transform=PDF if page is not None else MARKDOWN,
        page=page,
    )
    found = extract_declared(records)
    assert found is not None
    texts = {r.identifiers[0].value.value: r.text for r in of(found, Requirement)}
    assert texts["R-1"].value == "The arm shall stop within 200 ms of a stop request."
    assert texts["R-2"].value == "The gripper shall open on loss of air."
    for state in texts.values():
        cited = spanned(text, state.provenance.evidence)
        assert " ".join(cited.split()) == state.value  # the whole wrapped span, joined
    (step,) = of(found, SOPSection)
    assert step.title.value == "Change the gripper fingers"
    assert spanned(text, step.title.provenance.evidence) == "Change the\ngripper fingers"
    assert not found.candidates  # the continuation is the statement's, not a modal candidate


def test_a_statement_past_its_bounds_is_cut_with_a_finding() -> None:
    from neptune.declared._documents import MAX_CONTINUATION_LINES

    lines = "\n".join(f"line {n} of the wrapped text" for n in range(MAX_CONTINUATION_LINES + 5))
    _, records = document([(None, None, "Requirement R-9: Start\n" + lines)])
    found = extract_declared(records)
    assert found is not None
    (requirement,) = of(found, Requirement)
    assert requirement.text.value.endswith(f"line {MAX_CONTINUATION_LINES - 1} of the wrapped text")
    assert [f.code for f in found.findings] == ["declared.statement_too_long"]


def test_a_quotes_later_lines_are_read_without_their_markers() -> None:
    text, records = document(
        [(BlockRole.QUOTE, None, "Site: DOCK-4\n> Requirement R-2: Keep the\n> berth clear.")]
    )
    found = extract_declared(records)
    assert found is not None
    (requirement,) = of(found, Requirement)
    assert requirement.text.value == "Keep the berth clear."
    assert spanned(text, requirement.text.provenance.evidence) == "Keep the\n> berth clear."
    assert spanned(text, requirement.identifiers[0].provenance.evidence) == "R-2"


def test_only_fixed_systems_of_record_are_identifiers() -> None:
    _, records = table(
        ["asset_id", "name", "zone_id", "vendor_id", "cmms_id", "asset_tag", "external_id"],
        [["AMR-07", "Tugger 7", "Z1", "V-9", "EQ-1", "TAG-77", "X-3"]],
    )
    found = extract_declared(records)
    assert found is not None
    (asset,) = of(found, Asset)
    assert [i.value for i in asset.identifiers] == [
        LogicalId("asset", "AMR-07"),
        LogicalId("asset_tag", "TAG-77"),
        LogicalId("cmms", "EQ-1"),
        LogicalId("external", "X-3"),
    ]  # zone_id and vendor_id name other things: they stay in the row


def test_a_crs_without_a_colon_is_a_finding_and_height_cites_its_cell() -> None:
    _, sites = table(
        ["site_id", "latitude", "longitude", "altitude", "crs"],
        [["S-1", "1.0", "2.0", "35.5", "WGS84"]],
    )
    found = extract_declared(sites)
    assert found is not None
    (site,) = of(found, Site)
    position = site.location.value
    assert isinstance(position.crs, Unknown)
    assert position.height.value == 35.5
    assert position.height.provenance.evidence.locator == (RowCell(1, 3, "altitude"),)
    (finding,) = found.findings
    assert finding.code == "declared.crs_not_a_code"
    assert isinstance(finding.subject, EvidenceRef)
    assert finding.subject.locator == (RowCell(1, 4, "crs"),)


def test_many_holders_are_read_independently() -> None:
    records: list[Any] = []
    for n in range(400):
        records.extend(document([(None, None, f"Requirement R-{n}: Item {n} shall hold.")])[1])
    found = extract_declared(records)
    assert found is not None
    assert len(of(found, Requirement)) == 400


def test_a_reader_that_fails_costs_its_holder_and_a_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import neptune.declared as declared

    def broken(*_: Any) -> None:
        raise RuntimeError("unforeseen")

    _, sop = document(SOP)
    _, register = table(REGISTER, [["AMR-07", "Tugger 7", "AMR", "WH-3", None, None, None]])
    monkeypatch.setattr(declared, "read_table", broken)
    found = extract_declared([*sop, *register])
    assert found is not None
    assert not of(found, Asset)  # the register's reader failed: nothing from it
    assert of(found, SOPSection)  # the procedure is unaffected
    (failure,) = [f for f in found.findings if f.code == "declared.failed"]
    assert failure.details == {"error": "RuntimeError"}


def test_only_the_rows_the_pass_reads_are_wanted() -> None:
    from neptune.declared import row_wanted

    _, register = table(REGISTER, [["AMR-07", "Tugger 7", "AMR", "WH-3", None, None, None]])
    _, telemetry = table(["t", "speed"], [["0.1", "1.0"], ["0.2", "1.1"]])
    _, undeclared = table(None, [["a", "b"], ["c", "d"]])
    for records, wanted in (
        (register, [True]),
        (telemetry, [False, False]),
        (undeclared, [True, False]),
    ):
        tbl, *rows = records
        assert [row_wanted(tbl, row) for row in rows] == wanted
