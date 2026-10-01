"""Whole PDFs built in the test: large tags, image tables, page labels, broken boxes, repairs.

Each document is written with the fixture generator's PDF writer and read by the adapter through
the harness, so every law of the contract is checked on it too.
"""

import importlib.util
import sys
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
