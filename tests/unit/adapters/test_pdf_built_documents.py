"""Whole PDFs built in the test: large tags, image tables, page labels, broken boxes, repairs,
and the hostile shapes whose cost must stay bounded.

Each document is written with the fixture generator's PDF writer and read by the adapter through
the harness, so every law of the contract is checked on it too.
"""

import importlib.util
import sys
import time
import tracemalloc
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.pdf import PdfAdapter
from neptune.adapters.pdf._labels import LabelsUnreadable, numeral
from neptune.discovery.reader import BytesReader
from neptune.model.knowledge import Known
from neptune.model.world import (
    BlockRole,
    DocumentBlock,
    DocumentRecord,
    StructuredRecord,
    StructuredTable,
)

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "pdf"


def _generator() -> ModuleType:
    if "make_pdfs" in sys.modules:
        return sys.modules["make_pdfs"]
    spec = importlib.util.spec_from_file_location("make_pdfs", FIXTURES / "make_pdfs.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_pdfs"] = module
    spec.loader.exec_module(module)
    return module


MAKE: Final = _generator()


def ingest(data: bytes, pages_per_chunk: int = 1, **config: Any) -> SourceOutput:
    return ingest_source(PdfAdapter(pages_per_chunk), BytesReader(data), config)


def state(knowledge: Any) -> Any:
    return knowledge.value if isinstance(knowledge, Known) else type(knowledge).__name__


def roles(output: SourceOutput) -> list[Any]:
    found = [r for r in output.records() if isinstance(r, DocumentBlock)]
    return [state(block.role) for block in sorted(found, key=lambda block: block.order)]


def tagged_report(pages: int, per_page: int, rows: int, *, image_table: bool = False) -> bytes:
    """``pages`` pages of ``per_page`` tagged paragraphs under one element, then a page holding a
    ``rows`` x 4 table, each cell text or, with ``image_table``, an image."""
    pdf = MAKE.Pdf()
    tree, root, document = pdf.reserve(), pdf.reserve(), pdf.reserve()
    resources = {"Font": {"F1": pdf.add(MAKE.HELVETICA)}, "XObject": {"Im": MAKE.gray_image(pdf)}}
    kids: list[Any] = []
    nums: list[Any] = []
    page_refs = []
    for index in range(pages):
        content = b"".join(
            MAKE.marked("P", i, MAKE.text("F1", 8, 72, 760 - 10 * i, b"Line %d.%d" % (index, i)))
            for i in range(per_page)
        )
        page_ref = MAKE.page(pdf, tree, content, resources, StructParents=index)
        elements = [pdf.reserve() for _ in range(per_page)]
        for i, element in enumerate(elements):
            MAKE._element(pdf, element, "P", document, [i], page_ref)
        kids += elements
        nums += [index, list(elements)]
        page_refs.append(page_ref)
    body = b""
    for r in range(rows):
        for c in range(4):
            if image_table:
                drawn = b"q 4 0 0 4 %d %d cm /Im Do Q\n" % (72 + 50 * c, 700 - 2 * r)
            else:
                drawn = MAKE.text("F1", 2, 72 + 50 * c, 760 - 2 * r, b"r%dc%d" % (r, c))
            body += MAKE.marked("TD", r * 4 + c, drawn)
    table_page = MAKE.page(pdf, tree, body, resources, StructParents=pages)
    table, cells, row_refs = pdf.reserve(), [], []
    for r in range(rows):
        row = pdf.reserve()
        row_cells = [pdf.reserve() for _ in range(4)]
        for c, cell in enumerate(row_cells):
            MAKE._element(pdf, cell, "TD", row, [r * 4 + c], table_page)
        MAKE._element(pdf, row, "TR", table, list(row_cells))
        row_refs.append(row)
        cells += row_cells
    MAKE._element(pdf, table, "Table", document, list(row_refs))
    MAKE._element(pdf, document, "Document", root, [*kids, table])
    nums += [pages, list(cells)]
    pdf.set(
        root, {"Type": MAKE.Name("StructTreeRoot"), "K": [document], "ParentTree": {"Nums": nums}}
    )
    MAKE.page_tree(pdf, [*page_refs, table_page], tree)
    return bytes(pdf.build(MAKE.catalog(pdf, tree, StructTreeRoot=root, MarkInfo={"Marked": True})))


def test_large_tagged_documents_stay_tagged_whatever_the_chunking() -> None:
    data = tagged_report(pages=40, per_page=60, rows=300)
    one, eight = ingest(data, 1), ingest(data, 8)
    assert [f.code for f in eight.findings()] == []
    assert roles(eight) == roles(one) == [BlockRole.PARAGRAPH] * 2400 + [BlockRole.TABLE]
    assert [r.to_json() for r in eight.records()] == [r.to_json() for r in one.records()]
    rows = [r for r in eight.records() if isinstance(r, StructuredRecord)]
    assert len(rows) == 300 and state(rows[0].cells[0]).startswith("r")


def test_a_table_of_images_has_no_cells_to_cite() -> None:
    output = ingest(tagged_report(pages=0, per_page=0, rows=2, image_table=True))
    assert roles(output) == [BlockRole.TABLE]
    assert [r for r in output.records() if isinstance(r, StructuredRecord)] == []
    assert len([r for r in output.records() if isinstance(r, StructuredTable)]) == 1


def four_pages(box: Any = None, **catalog: Any) -> bytes:
    pdf = MAKE.Pdf()
    tree = pdf.reserve()
    pages = []
    for _ in range(4):
        media_box = box if box is not None else [0, 0, 612, 792]
        pages.append(pdf.add({"Type": MAKE.Name("Page"), "Parent": tree, "MediaBox": media_box}))
    MAKE.page_tree(pdf, pages, tree)
    return bytes(pdf.build(MAKE.catalog(pdf, tree, **catalog)))


def pages(output: SourceOutput) -> list[tuple[Any, Any]]:
    (record,) = [r for r in output.records() if isinstance(r, DocumentRecord)]
    return [(state(page.label), state(page.width)) for page in record.pages]


def test_page_labels_follow_the_specification() -> None:
    labels = {
        "Nums": [
            0,
            {"S": MAKE.Name("r"), "St": 3},
            2,
            {"S": MAKE.Name("A"), "St": 26, "P": MAKE.Lit(b"App-")},
        ]
    }
    assert [label for label, _ in pages(ingest(four_pages(PageLabels=labels)))] == [
        "iii",
        "iv",
        "App-Z",
        "App-AA",
    ]
    assert [numeral("R", 1994), numeral("a", 53), numeral("D", 7), numeral(None, 3)] == [
        "MCMXCIV",
        "aaa",
        "7",
        "",
    ]


def test_malformed_page_labels_are_unknown_never_a_default() -> None:
    for broken in (
        {"Nums": [0, 5]},
        {"Nums": [0, {"S": MAKE.Name("Q")}]},
        {"Nums": [0, {"S": MAKE.Name("D"), "St": 0}]},
        {"Kids": 3},
        7,
    ):
        output = ingest(four_pages(PageLabels=broken))
        assert [label for label, _ in pages(output)] == ["Unknown"] * 4, broken
        assert [f.details for f in output.findings()] == [{"field": "page_labels"}]
    try:
        numeral("R", 10**12)
    except LabelsUnreadable:
        pass
    else:
        raise AssertionError("a roman numeral of a trillion is a memory bomb, not a label")


def test_a_media_box_that_is_not_four_numbers_is_unknown() -> None:
    # pypdf refuses number tokens over 64 characters, so no box overflows a float; a box of the
    # wrong shape is the case left, and the adapter's finiteness check is a second guard.
    output = ingest(four_pages(box=[0, 0, MAKE.Name("wide"), 792]))
    assert pages(output) == [("Unknown", "Unknown")] * 4
    assert [f.details["field"] for f in output.findings()] == ["media_box"] * 4


def test_a_repaired_encrypted_file_is_never_read_as_plain_text() -> None:
    data = (FIXTURES / "encrypted_owner.pdf").read_bytes()
    output = ingest(data[: data.rindex(b"\nxref\n")])
    assert sorted(f.code for f in output.findings()) == ["pdf.encrypted", "pdf.repaired"]
    assert roles(output) == []


# --- Hostile costs: each shape is the review's reproduction, built here, never committed --------


def one_element_owning(count: int) -> bytes:
    """A tagged page whose parent tree lists ``count`` MCIDs, all owned by one ``P`` whose ``/K``
    lists ``count`` other MCIDs: every lookup misses, the shape that made the reader quadratic."""
    pdf = MAKE.Pdf()
    tree, root, element = pdf.reserve(), pdf.reserve(), pdf.reserve()
    content = MAKE.text("F1", 12, 72, 700, b"Check the hydraulic line")
    resources = {"Font": {"F1": pdf.add(MAKE.HELVETICA)}}
    page_ref = MAKE.page(pdf, tree, content, resources, StructParents=0)
    MAKE._element(pdf, element, "P", root, list(range(count, 2 * count)), page_ref)
    parents: list[Any] = [element] * count
    tree_root = {"Type": MAKE.Name("StructTreeRoot"), "K": [element]}
    pdf.set(root, {**tree_root, "ParentTree": {"Nums": [0, parents]}})
    MAKE.page_tree(pdf, [page_ref], tree)
    return bytes(pdf.build(MAKE.catalog(pdf, tree, StructTreeRoot=root, MarkInfo={"Marked": True})))


def test_an_element_owning_thousands_of_mcids_is_indexed_once() -> None:
    data = one_element_owning(8000)
    started = time.process_time()
    output = ingest(data)
    assert time.process_time() - started < 5  # a scan per MCID took about a minute
    assert [f.code for f in output.findings()] == []
    assert roles(output) == ["Unknown"]  # the run itself carries no MCID: untagged content


def test_a_structure_past_its_step_bound_is_a_limit_and_the_page_is_read_untagged() -> None:
    output = ingest(one_element_owning(120_000))
    assert [(f.code, f.details) for f in output.findings()] == [
        ("pdf.structure_limit", {"limit": 200_000, "page": 0})
    ]
    assert roles(output) == ["Unknown"]


def identity_font(pdf: Any, cmap: bytes) -> Any:
    descendant = {
        "Type": MAKE.Name("Font"),
        "Subtype": MAKE.Name("CIDFontType2"),
        "BaseFont": MAKE.Name("Ranges"),
        "DW": 500,
        "CIDSystemInfo": {"Registry": MAKE.Lit(b"Adobe"), "Ordering": MAKE.Lit(b"Identity")},
        "FontDescriptor": {"Type": MAKE.Name("FontDescriptor"), "Ascent": 800, "Descent": -200},
    }
    return pdf.add(
        {
            "Type": MAKE.Name("Font"),
            "Subtype": MAKE.Name("Type0"),
            "BaseFont": MAKE.Name("Ranges"),
            "Encoding": MAKE.Name("Identity-H"),
            "DescendantFonts": [pdf.add(descendant)],
            "ToUnicode": pdf.add(MAKE.Stream({}, cmap)),
        }
    )


def ranges_cmap(count: int) -> bytes:
    """``count`` one-code ``bfrange`` entries: code ``0x100 + i`` to letter ``i % 26``."""
    lines = [b"begincmap\n1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"]
    for start in range(0, count, 100):
        group = range(start, min(start + 100, count))
        lines.append(b"%d beginbfrange\n" % len(group))
        lines += [b"<%04X> <%04X> <%04X>\n" % (0x100 + i, 0x100 + i, 0x41 + i % 26) for i in group]
        lines.append(b"endbfrange\n")
    return b"".join([*lines, b"endcmap\n"])


def page_with_codes(cmap: bytes, codes: list[int]) -> bytes:
    pdf = MAKE.Pdf()
    tree = pdf.reserve()
    shown = b"BT /F1 12 Tf 72 700 Td <" + b"".join(b"%04X" % code for code in codes) + b"> Tj ET"
    page_ref = MAKE.page(pdf, tree, shown, {"Font": {"F1": identity_font(pdf, cmap)}})
    MAKE.page_tree(pdf, [page_ref], tree)
    return bytes(pdf.build(MAKE.catalog(pdf, tree)))


def texts(output: SourceOutput) -> list[Any]:
    found = [r for r in output.records() if isinstance(r, DocumentBlock)]
    return [state(block.text) for block in sorted(found, key=lambda block: block.order)]


def test_a_cmap_of_many_ranges_is_searched_not_scanned() -> None:
    codes = [0x100 + (i * 7919) % 60_000 for i in range(20_000)]
    data = page_with_codes(ranges_cmap(60_000), codes)
    started = time.process_time()
    output = ingest(data)
    assert time.process_time() - started < 5  # a scan of every range per code took seconds
    assert texts(output) == ["".join(chr(0x41 + (code - 0x100) % 26) for code in codes)]
    assert [f.code for f in output.findings()] == []


def test_a_cmap_past_the_entry_bound_is_a_font_limit_finding() -> None:
    output = ingest(page_with_codes(ranges_cmap(140_000), [0x100, 0x100 + 139_999]))
    assert texts(output) == ["Unknown"]  # the first code is mapped, the last is past the bound
    assert sorted((f.code, f.details) for f in output.findings()) == [
        ("pdf.font_limit", {"fonts": 1, "page": 0}),
        ("pdf.unmapped_glyphs", {"blocks": 1, "page": 0}),
    ]


def page_drawing_form(form: bytes, **form_entries: Any) -> bytes:
    """A page showing text, drawing form ``X1`` twice, then showing more text."""
    pdf = MAKE.Pdf()
    tree = pdf.reserve()
    entries = {
        "Type": MAKE.Name("XObject"),
        "Subtype": MAKE.Name("Form"),
        "BBox": [0, 0, 10, 10],
        **form_entries,
    }
    content = (
        MAKE.text("F1", 12, 72, 700, b"Torque the flange bolts to 40 Nm")
        + b"/X1 Do\n/X1 Do\n"
        + MAKE.text("F1", 12, 72, 600, b"Then check the seal")
    )
    xobject = pdf.add(MAKE.Stream(entries, form))
    resources = {"Font": {"F1": pdf.add(MAKE.HELVETICA)}, "XObject": {"X1": xobject}}
    page_ref = MAKE.page(pdf, tree, content, resources)
    MAKE.page_tree(pdf, [page_ref], tree)
    return bytes(pdf.build(MAKE.catalog(pdf, tree)))


def test_a_form_that_cannot_be_parsed_costs_the_form_not_the_page() -> None:
    inside = MAKE.text("F1", 12, 72, 650, b"Inside the form")
    output = ingest(page_drawing_form(inside + b"[" * 5000 + b"]" * 5000 + b" pop"))
    assert [(f.code, f.details) for f in output.findings()] == [
        ("pdf.content_unreadable", {"error": "RecursionError", "form": "X1", "forms": 1, "page": 0})
    ]
    # What the form drew before its fault is kept, each time it is drawn; the page goes on.
    assert texts(output) == [
        "Torque the flange bolts to 40 Nm",
        "Inside the form",
        "Inside the form",
        "Then check the seal",
    ]


def test_a_form_that_inflates_past_the_stream_bound_is_a_limit_on_that_form() -> None:
    inflated = MAKE.bomb_deflate(ord(" "), 2 * 1024 * 1024)  # 11 KiB on disk
    form = page_drawing_form(inflated, Filter=MAKE.Name("FlateDecode"))
    output = ingest(form, max_stream_bytes=1024 * 1024)
    limit = {"form": "X1", "forms": 1, "limit": "max_stream_bytes", "page": 0, "value": 1 << 20}
    assert [(f.code, f.details) for f in output.findings()] == [("pdf.content_limit", limit)]
    assert texts(output) == ["Torque the flange bolts to 40 Nm", "Then check the seal"]


def test_a_dense_content_stream_is_parsed_only_as_far_as_it_can_run() -> None:
    pdf = MAKE.Pdf()
    tree = pdf.reserve()
    content = MAKE.text("F1", 12, 72, 700, b"Before the operators") + b"q Q\n" * 1_000_000
    page_ref = MAKE.page(pdf, tree, content, {"Font": {"F1": pdf.add(MAKE.HELVETICA)}})
    MAKE.page_tree(pdf, [page_ref], tree)
    data = bytes(pdf.build(MAKE.catalog(pdf, tree)))
    tracemalloc.start()
    try:
        output = ingest(data, max_page_operations=10_000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Two million operators parsed whole hold some 300 MB; the parse stops past the 10,000 the
    # page may run.
    assert peak < 64 * 1024 * 1024
    assert [(f.code, f.details["limit"]) for f in output.findings()] == [
        ("pdf.content_limit", "max_page_operations")
    ]
    assert texts(output) == ["Before the operators"]
