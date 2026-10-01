"""The PDF adapter on real files: tagged and untagged pages, corruptions and hostile files.

The oracle for text is what the fixture generator wrote (``tests/fixtures/pdf/make_pdfs.py``):
every string a page shows is a constant there. Spans are checked arithmetically: a page's blocks,
in order, tile its extracted text, each followed by one line feed, so every citation resolves.
"""

from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.contract import (
    PROBE_HEAD_SIZE,
    SIGNATURE,
    ProbeHints,
    ShortReadError,
    configure,
)
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.pdf import DESCRIPTOR, STRIDE, PdfAdapter
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.finding import IngestFinding, Severity
from neptune.model.knowledge import Known, NotApplicable, Unknown
from neptune.model.provenance import (
    AdapterLocator,
    ByteRange,
    EvidenceRef,
    Page,
    PageRegion,
    Provenance,
    Span,
)
from neptune.model.world import (
    BlockRole,
    DocumentBlock,
    DocumentRecord,
    StructuredRecord,
    StructuredTable,
)

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "pdf"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(name: str, pages_per_chunk: int = 1, **config: Any) -> SourceOutput:
    return ingest_source(PdfAdapter(pages_per_chunk), BytesReader(fixture(name)), config)


def state(knowledge: Any) -> Any:
    """A state's value if known, else the name of its state."""
    return knowledge.value if isinstance(knowledge, Known) else type(knowledge).__name__


def blocks(output: SourceOutput) -> list[DocumentBlock]:
    found = [r for r in output.records() if isinstance(r, DocumentBlock)]
    return sorted(found, key=lambda block: block.order)


def summary(output: SourceOutput) -> list[tuple[int, int, Any, Any, Any]]:
    """(page, position on the page, role, level, text) per block, in reading order."""
    return [
        (b.order // STRIDE, b.order % STRIDE, state(b.role), state(b.level), state(b.text))
        for b in blocks(output)
    ]


def document(output: SourceOutput) -> DocumentRecord:
    (found,) = [r for r in output.records() if isinstance(r, DocumentRecord)]
    return found


def codes(output: SourceOutput) -> list[str]:
    return sorted(finding.code for finding in output.findings())


def finding(output: SourceOutput, code: str) -> IngestFinding:
    (found,) = [f for f in output.findings() if f.code == code]
    return found


def page_texts(output: SourceOutput) -> dict[int, str]:
    """Each page's extracted text rebuilt from its blocks, checking that their spans tile it."""
    by_page: dict[int, list[DocumentBlock]] = defaultdict(list)
    for block in blocks(output):
        page, span = block.provenance.evidence.locator
        assert isinstance(page, Page) and isinstance(span, Span)
        assert block.order // STRIDE == page.index
        by_page[page.index].append(block)
    texts: dict[int, str] = {}
    for index, found in by_page.items():
        text = ""
        for block in found:  # already in reading order
            _, span = block.provenance.evidence.locator
            assert isinstance(span, Span)
            assert span.start == len(text), "blocks tile the page text in order"
            if isinstance(block.text, Known):
                assert len(block.text.value) == span.end - span.start
                text += block.text.value
            elif isinstance(block.text, NotApplicable):
                assert span.end - span.start == 1
                text += "￼"
            else:
                text += "�" * (span.end - span.start)
            text += "\n"
        texts[index] = text
    return texts


def as_bytes(output: SourceOutput) -> bytes:
    rows = [record.to_json() for record in output.package_records()]
    return b"".join(canonical_json.dumps(row) + b"\n" for row in rows)


# --- Probe and inspect -------------------------------------------------------------------------


def probe(data: bytes, name: str = "f") -> tuple[float, list[str], str | None]:
    result = PdfAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints(name, len(data)))
    return result.confidence, [reason.code for reason in result.reasons], result.version


def test_a_pdf_header_is_its_signature_whatever_the_name() -> None:
    assert probe(fixture("pump_sop.pdf"), "pump_sop.pdf") == (SIGNATURE, ["pdf.header"], "1.7")
    assert probe(fixture("renamed_datasheet")) == (SIGNATURE, ["pdf.header"], "1.6")
    assert probe(b"junk\n" + fixture("blank.pdf")) == (SIGNATURE, ["pdf.header_offset"], "1.7")


def test_bytes_without_a_header_are_not_pdf() -> None:
    assert probe(b"")[0] == 0.0
    assert probe(b"# Pump room SOP\n", "sop.pdf") == (0.0, ["pdf.no_header"], None)
    assert probe(b" " * 2048 + b"%PDF-1.7")[0] == 0.0  # past the first 1,024 bytes
    assert probe(b"%PDF-x.y\n") == (SIGNATURE, ["pdf.header"], None)  # no version to declare


def test_inspect_summarises_without_reading_page_content() -> None:
    source = BytesReader(fixture("pump_sop.pdf"))
    result = PdfAdapter().inspect(source, configure(DESCRIPTOR))
    assert result.summary == {
        "encrypted": False,
        "pages": 2,
        "readable": True,
        "size": source.size,
        "tagged": True,
        "version": "1.7",
    }
    empty = PdfAdapter().inspect(BytesReader(b""), configure(DESCRIPTOR))
    assert empty.summary == {"encrypted": False, "readable": False, "size": 0, "tagged": False}
    locked = PdfAdapter().inspect(BytesReader(fixture("encrypted_user.pdf")), configure(DESCRIPTOR))
    assert (locked.summary["encrypted"], locked.summary["readable"]) == (True, False)


def test_the_descriptor_pins_pypdf_and_declares_its_steps() -> None:
    from importlib.metadata import version

    assert dict(DESCRIPTOR.libraries) == {"pypdf": version("pypdf")}
    assert [step.name for step in DESCRIPTOR.locator_steps] == ["pdf:object", "pdf:structure"]
    assert dict(configure(DESCRIPTOR).transform.libraries) == {"pypdf": version("pypdf")}


# --- A tagged document -------------------------------------------------------------------------


def test_a_tagged_pdf_gives_the_roles_and_order_its_tags_declare() -> None:
    output = run("pump_sop.pdf")
    assert summary(output) == [
        (0, 0, BlockRole.HEADING, 1, "Pump room start-up"),
        (
            0,
            1,
            BlockRole.PARAGRAPH,
            "NotApplicable",
            "Close valve V-12 before entering the pump room.\n"
            "Check that the pressure gauge reads below 2 bar.",
        ),
        (0, 2, BlockRole.LIST_ITEM, 1, "• Start pump P-2 from the local panel."),
        (0, 3, BlockRole.FIGURE, "NotApplicable", "NotApplicable"),
        (0, 4, BlockRole.CAPTION, "NotApplicable", "Figure 1: Valve V-12, closed."),
        (0, 5, BlockRole.HEADER, "NotApplicable", "North plant · Pump room SOP"),
        (0, 6, BlockRole.FOOTER, "NotApplicable", "Page i"),
        (1, 0, BlockRole.HEADING, 2, "Torque table"),
        (1, 1, BlockRole.TABLE, "NotApplicable", "Bolt\tTorque\tUnit\nM8\t25\tN·m\nM10\t\tN·m"),
        (1, 2, BlockRole.PARAGRAPH, "NotApplicable", "Torque with a calibrated wrench."),
        (1, 3, "Unknown", "Unknown", "Rev 3"),  # untagged content on a tagged page
    ]
    assert codes(output) == []
    page_texts(output)


def test_the_document_cites_its_bytes_title_labels_and_page_boxes() -> None:
    data = fixture("pump_sop.pdf")
    output = run("pump_sop.pdf")
    record = document(output)
    source = BytesReader(data).content_id
    assert record.provenance.evidence == EvidenceRef(source, (ByteRange(0, len(data)),))
    assert record.format == "pdf"
    title = record.title
    assert isinstance(title, Known) and title.value == "Pump room SOP"
    assert isinstance(title.provenance, Provenance)
    (step,) = title.provenance.evidence.locator
    assert isinstance(step, AdapterLocator) and step.kind == "pdf:object"
    number = dict(step.fields)["number"]
    assert f"\n{number} 0 obj\n<</Title (Pump room SOP)".encode() in data
    assert [state(page.label) for page in record.pages] == ["i", "A-1"]
    for index, page in enumerate(record.pages):
        assert (state(page.width), state(page.height)) == (612.0, 792.0)
        assert isinstance(page.width, Known) and isinstance(page.width.provenance, Provenance)
        assert page.width.provenance.evidence.locator == (Page(index),)
        # No /Rotate: the specification's 0, citing the header that establishes the format.
        rotation = page.rotation
        assert isinstance(rotation, Known) and rotation.value == 0
        assert isinstance(rotation.provenance, Provenance)
        assert rotation.provenance.evidence.locator == (ByteRange(0, 8),)


def test_regions_are_where_the_file_draws_each_block() -> None:
    output = run("pump_sop.pdf")
    regions = {}
    for block in blocks(output):
        region = block.region
        assert isinstance(region, Known), block
        page, box = region.value.locator
        assert isinstance(page, Page) and isinstance(box, PageRegion) and box.page == page.index
        assert 0.0 <= box.x0 <= box.x1 <= 612.0 and 0.0 <= box.y0 <= box.y1 <= 792.0
        regions[state(block.text) if isinstance(block.text, Known) else "figure"] = box
    assert regions["figure"] == PageRegion(0, 72.0, 520.0, 192.0, 600.0)  # 120 x 80 at (72, 520)
    heading = regions["Pump room start-up"]
    # Helvetica-Bold 18 pt from x = 72 on the baseline y = 720: descent -207, ascent 718 (AFM).
    assert (heading.x0, heading.y0, heading.y1) == (72.0, 716.274, 732.924)
    paragraph = regions[
        "Close valve V-12 before entering the pump room.\n"
        "Check that the pressure gauge reads below 2 bar."
    ]
    assert paragraph.y0 < 690 - 14 < paragraph.y1  # it spans both of its lines


def test_a_tagged_table_is_a_structured_table_with_its_rows() -> None:
    output = run("pump_sop.pdf")
    (table,) = [r for r in output.records() if isinstance(r, StructuredTable)]
    rows = sorted(
        (r for r in output.records() if isinstance(r, StructuredRecord)), key=lambda r: r.row
    )
    (step,) = table.provenance.evidence.locator
    assert isinstance(step, AdapterLocator) and dict(step.fields) == {"path": "0/6"}
    assert state(table.name) == "Unknown"  # the table declares no caption
    assert state(table.header) == ("Bolt", "Torque", "Unit")
    assert [(row.row, [state(cell) for cell in row.cells]) for row in rows] == [
        (1, ["M8", "25", "N·m"]),
        (2, ["M10", "Unknown", "N·m"]),
    ]
    text = page_texts(output)[1]
    for row in rows:
        assert row.table == table.id
        for cell in row.cells:
            assert isinstance(cell, Known | Unknown) and isinstance(cell.provenance, Provenance)
            page, span = cell.provenance.evidence.locator
            assert page == Page(1) and isinstance(span, Span)
            assert text[span.start : span.end] == (cell.value if isinstance(cell, Known) else "")
    header = table.header
    assert isinstance(header, Known) and isinstance(header.provenance, Provenance)
    _, span = header.provenance.evidence.locator
    assert isinstance(span, Span) and text[span.start : span.end] == "Bolt\tTorque\tUnit"


# --- An untagged document ----------------------------------------------------------------------


def test_an_untagged_pdf_is_runs_and_figures_in_content_order() -> None:
    output = run("gripper_datasheet.pdf")
    assert summary(output) == [
        (0, 0, "Unknown", "Unknown", "GX-2 Parallel Gripper"),
        (0, 1, "Unknown", "Unknown", "Stroke per jaw: 40 mm"),  # TJ gaps of 333/1000 em
        (0, 2, "Unknown", "Unknown", "Grip force: 140 N"),
        (0, 3, "Unknown", "Unknown", "Repeatability: 0.02 mm"),
        (0, 4, "Unknown", "Unknown", "Weight: 0.9 kg"),
        (0, 5, "Unknown", "Unknown", "Scaled note"),
        (0, 6, "Unknown", "Unknown", "Rev C"),  # one form, drawn twice
        (0, 7, "Unknown", "Unknown", "Rev C"),
        (0, 8, BlockRole.FIGURE, "NotApplicable", "NotApplicable"),  # inline image
        (0, 9, BlockRole.FIGURE, "NotApplicable", "NotApplicable"),  # image XObject
        (1, 0, "Unknown", "Unknown", "Payload 2 kg"),  # Identity-H with ToUnicode
        (1, 1, "Unknown", "Unknown", "• Safe zone"),  # /Differences: /bullet
        (1, 2, "Unknown", "Unknown", "Unknown"),  # /g42 names no Unicode
    ]
    page_texts(output)
    first, second = (b for b in blocks(output) if state(b.text) == "Rev C")
    assert first.id != second.id and state(first.region) != state(second.region)


def test_an_unmapped_glyph_makes_its_block_unknown_and_says_so() -> None:
    output = run("gripper_datasheet.pdf")
    unknown = [b for b in blocks(output) if isinstance(b.text, Unknown)]
    assert len(unknown) == 1 and isinstance(unknown[0].region, Unknown)
    unmapped = finding(output, "pdf.unmapped_glyphs")
    assert unmapped.severity is Severity.WARNING and unmapped.records == (unknown[0].id,)
    assert unmapped.subject == EvidenceRef(unknown[0].provenance.evidence.source, (Page(1),))
    assert finding(output, "pdf.geometry_unknown").records == (unknown[0].id,)
    assert texts_of(output, page=1)[2] == "�" * len("Marker x")


def texts_of(output: SourceOutput, page: int) -> list[str]:
    return page_texts(output)[page].split("\n")


def test_a_rotated_page_keeps_its_declared_rotation_and_box() -> None:
    record = document(run("gripper_datasheet.pdf"))
    rotated = record.pages[1]
    assert (state(rotated.width), state(rotated.height), state(rotated.rotation)) == (
        842.0,
        595.0,
        90,
    )
    assert state(record.title) == "Unknown"  # /Info has no /Title
    assert [state(page.label) for page in record.pages] == ["Unknown", "Unknown"]


def test_scaled_text_is_placed_through_the_ctm() -> None:
    output = run("gripper_datasheet.pdf")
    (scaled,) = [b for b in blocks(output) if state(b.text) == "Scaled note"]
    region = scaled.region
    assert isinstance(region, Known)
    box = region.value.locator[-1]
    assert isinstance(box, PageRegion)
    assert box.x0 == 56.0 and box.y0 < 600.0 < box.y1  # 37.3333 x 1.5, baseline 400 x 1.5


def test_the_space_threshold_is_config_and_changes_lineage() -> None:
    default = run("gripper_datasheet.pdf")
    strict = run("gripper_datasheet.pdf", space_threshold=400)
    assert summary(strict)[1][4] == "Strokeperjaw:40mm"
    assert default.config.transform.id != strict.config.transform.id
    assert not {r.id for r in default.records()} & {r.id for r in strict.records()}


def test_object_streams_and_an_incremental_update_are_read_as_the_last_revision() -> None:
    output = run("site_manifest.pdf")
    assert state(document(output).title) == "Site manifest"  # the update's, not "Draft"
    assert [text for *_, text in summary(output)] == [
        "Site manifest: North plant",
        "Asset P-2  centrifugal pump  bay 3",
        "Asset V-12  gate valve  bay 3",
    ]
    assert codes(output) == []


# --- Boundaries --------------------------------------------------------------------------------


def test_a_blank_page_is_a_page_with_no_blocks() -> None:
    output = run("blank.pdf")
    assert len(document(output).pages) == 1 and blocks(output) == [] and codes(output) == []


def test_an_empty_file_is_one_finding_and_no_records() -> None:
    output = run("empty.pdf")
    assert output.records() == () and codes(output) == ["pdf.unreadable"]
    assert finding(output, "pdf.unreadable").details == {"error": "EmptyFileError"}


def test_the_operation_bound_stops_a_page_where_it_is_reached() -> None:
    output = run("gripper_datasheet.pdf", max_page_operations=6)
    limit = [f for f in output.findings() if f.code == "pdf.content_limit"]
    assert [f.details for f in limit] == [
        {"limit": "max_page_operations", "page": 0, "value": 6},
        {"limit": "max_page_operations", "page": 1, "value": 6},
    ]
    first_page = [text for page, _, _, _, text in summary(output) if page == 0]
    assert first_page == ["GX-2 Parallel Gripper"]


def test_order_strides_pages_so_chunking_never_renumbers() -> None:
    output = run("pump_sop.pdf")
    orders = [block.order for block in blocks(output)]
    assert orders[:7] == list(range(7))
    assert orders[7:] == [STRIDE + position for position in range(4)]


# --- Malformed input ---------------------------------------------------------------------------


def test_a_truncated_file_is_rebuilt_and_read_as_far_as_its_bytes_go() -> None:
    output = run("truncated.pdf")
    assert finding(output, "pdf.repaired").details["rebuilt"] is True
    assert [text for *_, text in summary(output)] == ["Inspection log: pump P-2"]
    unreadable = finding(output, "pdf.content_unreadable")
    assert unreadable.details == {"error": "PdfReadError", "page": 1}
    assert unreadable.severity is Severity.ERROR
    assert len(document(output).pages) == 2


def test_a_corrupt_content_stream_loses_its_page_only() -> None:
    output = run("corrupted.pdf")
    assert finding(output, "pdf.content_unreadable").details == {"page": 0, "streams": 1}
    assert [page for page, *_ in summary(output)] == [1, 1, 1]


def test_an_encrypted_file_lists_its_pages_and_reads_no_text() -> None:
    output = run("encrypted_user.pdf")
    assert codes(output) == ["pdf.encrypted"]
    assert finding(output, "pdf.encrypted").details == {"filter": "Standard", "version": 2}
    record = document(output)
    assert state(record.title) == "Unknown"
    assert [(state(p.width), state(p.label)) for p in record.pages] == [(612.0, "Unknown")] * 2
    assert blocks(output) == [] and len(output.plan.chunks) == 1


def test_an_owner_password_alone_does_not_stop_reading() -> None:
    assert summary(run("encrypted_owner.pdf")) == summary(run("pump_sop.pdf"))
    assert codes(run("encrypted_owner.pdf")) == []


def test_aes_is_never_decrypted() -> None:
    output = run("encrypted_aes.pdf")
    assert finding(output, "pdf.encrypted").details == {"filter": "Standard", "version": 4}
    assert blocks(output) == []


# --- Hostile input -----------------------------------------------------------------------------


def test_a_recursion_bomb_is_findings_and_the_next_page_is_read() -> None:
    output = run("hostile_nesting.pdf")
    unreadable = finding(output, "pdf.content_unreadable")
    assert unreadable.details == {"error": "RecursionError", "page": 0}
    assert finding(output, "pdf.value_unreadable").details == {"field": "title"}
    # The operators parsed before the bomb are drawn: it costs only the rest of its page.
    assert [text for *_, text in summary(output)] == ["Before the nesting", "After the nesting"]


def test_a_decompression_bomb_stops_at_the_page_bound() -> None:
    output = run("hostile_bomb.pdf")
    assert finding(output, "pdf.content_limit").details == {
        "limit": "max_page_content_bytes",
        "page": 0,
        "value": 16 * 1024 * 1024,
    }
    assert [text for *_, text in summary(output)] == ["Inspection checklist"]
    inflate = run("hostile_bomb.pdf", max_stream_bytes=1024 * 1024)
    assert finding(inflate, "pdf.content_limit").details == {
        "limit": "max_stream_bytes",
        "page": 0,
        "value": 1024 * 1024,
    }


def test_active_content_is_reported_and_never_run() -> None:
    output = run("hostile_active.pdf")
    assert finding(output, "pdf.javascript").details == {"scripts": 3}
    assert finding(output, "pdf.javascript").severity is Severity.INFO
    assert finding(output, "pdf.embedded_files").details == {"files": 1}
    assert [text for *_, text in summary(output)] == ["Lockout tagout procedure"]


def test_a_lying_cross_reference_table_is_rebuilt() -> None:
    output = run("hostile_xref.pdf")
    assert finding(output, "pdf.repaired").details["rebuilt"] is True
    assert len(document(output).pages) == 1


def test_a_looping_page_tree_is_unreadable_not_a_hang() -> None:
    output = run("hostile_pages.pdf")
    assert codes(output) == ["pdf.unreadable"] and output.records() == ()


def test_an_object_stream_that_overstates_its_count_is_read() -> None:
    output = run("hostile_objstm.pdf")
    assert state(document(output).title) == "Site manifest"
    assert codes(output) == ["pdf.repaired"]


def test_a_short_read_is_never_swallowed() -> None:
    data = fixture("gripper_datasheet.pdf")

    half = len(data) // 2

    class Short(BytesReader):
        """Declares every byte and serves only the first half: the file shrank under us."""

        def read(self, offset: int, length: int) -> bytes:
            return super().read(offset, max(0, min(length, half - offset)))

    with pytest.raises(ShortReadError):
        ingest_source(PdfAdapter(), Short(data))


# --- Determinism and lineage -------------------------------------------------------------------

EVERY: Final = sorted(
    p.name for p in FIXTURES.iterdir() if p.is_file() and p.suffix not in (".md", ".py")
)


@pytest.mark.parametrize("name", EVERY)
def test_output_is_byte_identical_and_independent_of_chunking(name: str) -> None:
    once = as_bytes(run(name, pages_per_chunk=1))
    assert as_bytes(run(name, pages_per_chunk=1)) == once
    assert as_bytes(run(name, pages_per_chunk=2)) == once
    assert as_bytes(run(name, pages_per_chunk=8)) == once


@pytest.mark.parametrize("name", EVERY)
def test_every_citation_resolves_on_its_page(name: str) -> None:
    output = run(name)
    page_texts(output)
    pages = {i: page for i, page in enumerate(document(output).pages)} if output.records() else {}
    for block in blocks(output):
        assert block.document == document(output).id
        region = block.region
        if isinstance(region, Known):
            page, box = region.value.locator
            assert isinstance(page, Page) and page.index in pages and isinstance(box, PageRegion)


def test_another_version_is_another_lineage() -> None:
    class Bumped(PdfAdapter):
        descriptor = replace(DESCRIPTOR, version="0.1.1")

    data = fixture("pump_sop.pdf")
    old = ingest_source(PdfAdapter(), BytesReader(data))
    new = ingest_source(Bumped(), BytesReader(data))
    assert {r.id for r in old.records()}.isdisjoint({r.id for r in new.records()})
    assert summary(old) == summary(new)
