"""The PDF adapter's parts on small cases: font decoding, the content interpreter, block assembly.

Pages are built with the fixture generator's PDF writer (``tests/fixtures/pdf/make_pdfs.py``)
and read with pypdf, so every case is a real, small PDF.
"""

import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest
from pypdf import PdfReader
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
)

from neptune.adapters.pdf._content import MAX_FORM_DEPTH, Box, Interpreter, Item, Line, PageContent
from neptune.adapters.pdf._fonts import Ranges, glyph_text, load_font, parse_to_unicode
from neptune.adapters.pdf._page import PLACEHOLDER, blocks, join
from neptune.model.world import BlockRole

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


# --- Glyph names and ToUnicode -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("glyph", "text"),
    [
        ("A", "A"),
        ("bullet", "•"),
        ("f_i", "fi"),
        ("A.sc", "A"),
        ("uni0041", "A"),
        ("uni00410042", "AB"),
        ("u1F600", "\U0001f600"),
        ("uniD800", None),  # a surrogate is not a character
        ("g42", None),
        (".notdef", None),
        ("", None),
    ],
)
def test_glyph_names_read_by_the_adobe_glyph_list_rules(glyph: str, text: str | None) -> None:
    assert glyph_text(glyph) == text


def test_a_to_unicode_cmap_maps_single_codes_ranges_and_arrays() -> None:
    cmap = parse_to_unicode(
        b"begincmap 1 begincodespacerange <00> <7F> endcodespacerange\n"
        b"1 begincodespacerange <8000> <FFFF> endcodespacerange\n"
        b"2 beginbfchar <01> <0041> <02> <D83DDE00> endbfchar\n"
        b"1 beginbfchar <03> <D800> endbfchar\n"
        b"2 beginbfrange <10> <12> <0061> <8000> <8001> [<0058> <0059>] endbfrange endcmap"
    )
    assert cmap.lookup(1, 1) == "A"
    assert cmap.lookup(2, 1) == "\U0001f600"  # a surrogate pair is one character
    assert cmap.lookup(3, 1) is None  # a lone surrogate is unmapped
    assert [cmap.lookup(code, 1) for code in (0x10, 0x11, 0x12, 0x13)] == ["a", "b", "c", None]
    assert [cmap.lookup(code, 2) for code in (0x8000, 0x8001)] == ["X", "Y"]
    assert list(cmap.split(b"\x01\x80\x00\x7f")) == [(1, 1), (0x8000, 2), (0x7F, 1)]


def test_a_malformed_cmap_maps_what_parses_and_nothing_else() -> None:
    cmap = parse_to_unicode(b"2 beginbfchar <01> <0041> <02> endbfchar beginbfrange <zz> ] [ <00>")
    assert cmap.lookup(1, 1) == "A" and cmap.lookup(2, 1) is None


def test_indexed_ranges_answer_what_a_scan_in_declaration_order_would() -> None:
    declared = [(10, 20, "a"), (15, 30, "b"), (5, 12, "c"), (40, 40, "d"), (0, 100, "e")]
    indexed = Ranges(declared)
    for code in range(-2, 103):
        scanned = next((r for r in declared if r[0] <= code <= r[1]), None)
        assert indexed.find(code) == scanned, code
    assert Ranges([]).find(0) is None


def test_overlapping_cmap_ranges_keep_the_first_declared() -> None:
    cmap = parse_to_unicode(
        b"1 begincodespacerange <0000> <00FF> endcodespacerange\n"
        b"1 begincodespacerange <0080> <FFFF> endcodespacerange\n"
        b"2 beginbfrange <0010> <0020> <0061> <0018> <0030> <0041> endbfrange"
    )
    assert [cmap.lookup(code, 2) for code in (0x10, 0x18, 0x20, 0x21, 0x30, 0x31)] == [
        "a",
        "i",
        "q",
        "J",
        "Y",
        None,
    ]
    assert list(cmap.split(b"\x00\x10\xff\xff")) == [(0x10, 2), (0xFFFF, 2)]


# --- Fonts -------------------------------------------------------------------------------------


def name(value: str) -> NameObject:
    return NameObject("/" + value)


def font(**entries: Any) -> DictionaryObject:
    return DictionaryObject({name(key): value for key, value in entries.items()})


def numbers(*values: float) -> ArrayObject:
    return ArrayObject(FloatObject(v) if isinstance(v, float) else NumberObject(v) for v in values)


def test_a_simple_font_reads_its_encoding_differences_and_widths() -> None:
    encoding = font(
        BaseEncoding=name("WinAnsiEncoding"),
        Differences=ArrayObject([NumberObject(1), name("bullet"), name("g42")]),
    )
    loaded = load_font(
        font(
            Subtype=name("TrueType"),
            BaseFont=name("ABCDEF+SOPSans"),
            Encoding=encoding,
            FirstChar=NumberObject(1),
            Widths=numbers(350, 400, 500),
            FontDescriptor=font(Ascent=NumberObject(750), Descent=NumberObject(-250)),
        )
    )
    glyphs = loaded.glyphs(b"\x01\x02\x03A")
    assert [g.text for g in glyphs] == ["•", None, "\x03" if False else None, "A"]
    assert [g.width for g in glyphs] == [0.35, 0.4, 0.5, 0.0]  # A is past LastChar: MissingWidth 0
    assert (loaded.ascent, loaded.descent) == (0.75, -0.25)


def test_a_standard_font_without_widths_uses_the_core_14_metrics() -> None:
    loaded = load_font(font(Subtype=name("Type1"), BaseFont=name("Helvetica")))
    (glyph,) = loaded.glyphs(b"A")
    assert (glyph.text, glyph.width) == ("A", 0.667)
    assert loaded.ascent == 0.718 and loaded.descent == -0.207
    unknown = load_font(font(Subtype=name("Type1"), BaseFont=name("NotAFont")))
    assert unknown.glyphs(b"A")[0].width is None and unknown.ascent is None


def test_a_symbolic_font_without_a_map_has_no_text() -> None:
    loaded = load_font(
        font(
            Subtype=name("TrueType"),
            BaseFont=name("Wingdings"),
            FontDescriptor=font(Flags=NumberObject(4)),
        )
    )
    assert loaded.glyphs(b"A")[0].text is None


def test_a_composite_font_reads_both_forms_of_w_under_identity() -> None:
    descendant = font(
        Subtype=name("CIDFontType2"),
        DW=NumberObject(800),
        W=ArrayObject(
            [
                NumberObject(1),
                numbers(500, 600),
                NumberObject(10),
                NumberObject(20),
                NumberObject(700),
            ]
        ),
    )
    loaded = load_font(
        font(
            Subtype=name("Type0"),
            Encoding=name("Identity-H"),
            DescendantFonts=ArrayObject([descendant]),
        )
    )
    widths = [g.width for g in loaded.glyphs(b"\x00\x01\x00\x02\x00\x0f\x00\x63")]
    assert widths == [0.5, 0.6, 0.7, 0.8]
    assert [g.text for g in loaded.glyphs(b"\x00\x01")] == [None]  # no ToUnicode: unmapped
    other = load_font(
        font(
            Subtype=name("Type0"),
            Encoding=name("UniJIS-UCS2-H"),
            DescendantFonts=ArrayObject([descendant]),
        )
    )
    assert other.glyphs(b"\x00\x01")[0].width is None  # CIDs unknown without the CMap


def test_a_type3_font_scales_by_its_matrix() -> None:
    loaded = load_font(
        font(
            Subtype=name("Type3"),
            FontMatrix=numbers(0.01, 0, 0, 0.01, 0, 0),
            FontBBox=numbers(0, -20, 100, 80),
            FirstChar=NumberObject(65),
            Widths=numbers(50),
            Encoding=font(Differences=ArrayObject([NumberObject(65), name("A")])),
        )
    )
    (glyph,) = loaded.glyphs(b"A")
    assert (glyph.text, glyph.width) == ("A", 0.5)
    assert (loaded.ascent, loaded.descent) == (0.8, -0.2)
    broken = load_font(font(Subtype=name("Type3"), Widths=numbers(50), FirstChar=NumberObject(65)))
    assert broken.glyphs(b"A")[0].width is None


def test_a_to_unicode_stream_overrides_the_encoding() -> None:
    stream = DecodedStreamObject()
    stream.set_data(MAKE.to_unicode_cmap({0x41: "Ω"}))
    loaded = load_font(font(Subtype=name("Type1"), BaseFont=name("Helvetica"), ToUnicode=stream))
    assert loaded.glyphs(b"AB")[0].text == "Ω" and loaded.glyphs(b"B")[0].text == "B"


# --- The interpreter ---------------------------------------------------------------------------


def interpret(
    content: bytes,
    *,
    extra: dict[str, Any] | None = None,
    max_operations: int = 1000,
    space_threshold: int = 200,
) -> PageContent:
    """One US-letter page with Helvetica as /F1, drawing ``content``."""
    pdf = MAKE.Pdf()
    tree = pdf.reserve()
    resources = {"Font": {"F1": pdf.add(MAKE.HELVETICA)}, **(extra or {})}
    if callable(resources.get("build")):
        resources = resources.pop("build")(pdf, resources)
    only = MAKE.page(pdf, tree, content, resources)
    MAKE.page_tree(pdf, [only], tree)
    reader = PdfReader(io.BytesIO(pdf.build(MAKE.catalog(pdf, tree))))
    page = reader.pages[0]
    return Interpreter(
        reader,
        max_operations=max_operations,
        max_content_bytes=1 << 20,
        space_threshold=space_threshold,
    ).run(page)


def texts(content: PageContent) -> list[str | None]:
    return [item.text for item in content.items]


@pytest.mark.parametrize(
    ("array", "shown"),
    [
        (b"[(A) -199 (B)]", "AB"),
        (b"[(A) -200 (B)]", "A B"),
        (b"[(A) 500 (B)]", "AB"),  # a move left is kerning, never a space
        (b"[-500 (A) -500]", "A"),  # nothing to separate at either end
        (b"[(A ) -500 (B)]", "A B"),  # the string already has its space
    ],
)
def test_a_tj_displacement_past_the_threshold_is_one_space(array: bytes, shown: str) -> None:
    content = interpret(b"BT /F1 10 Tf 72 700 Td " + array + b" TJ ET")
    assert texts(content) == [shown]


def box(content: PageContent, index: int = 0) -> Box:
    found = content.items[index].box
    assert found is not None
    return found


def test_horizontal_scale_and_rise_move_the_box() -> None:
    plain = box(interpret(b"BT /F1 10 Tf 72 700 Td (AAAA) Tj ET"))
    squeezed = box(interpret(b"BT /F1 10 Tf 50 Tz 72 700 Td (AAAA) Tj ET"))
    raised = box(interpret(b"BT /F1 10 Tf 5 Ts 72 700 Td (AAAA) Tj ET"))
    assert plain.x1 - plain.x0 == pytest.approx(4 * 6.67)
    assert squeezed.x1 - squeezed.x0 == pytest.approx(2 * 6.67)
    assert raised.y0 == pytest.approx(plain.y0 + 5)


def test_the_graphics_state_is_saved_and_restored() -> None:
    content = interpret(b"q 2 0 0 2 0 0 cm Q Q Q BT /F1 10 Tf 72 700 Td (A) Tj ET")
    assert box(content).x0 == 72.0 and content.skipped == 0  # extra Q is ignored


def test_bad_operands_and_missing_fonts_are_counted_not_raised() -> None:
    content = interpret(b"BT 12 Tf /F9 10 Tf 72 700 Td (AB) Tj ET BT /F1 /x Tf ET (x) Tz")
    assert content.skipped == 3 and content.missing_fonts == 1
    assert texts(content) == ["��"] and content.items[0].unmapped == 2
    assert content.items[0].box is None


def test_a_form_that_draws_itself_stops_at_the_depth_bound() -> None:
    def build(pdf: Any, resources: dict[str, Any]) -> dict[str, Any]:
        form = pdf.reserve()
        body = b"BT /F1 8 Tf 0 0 Td (x) Tj ET /Fx Do"
        pdf.set(
            form,
            MAKE.Stream(
                {
                    "Type": MAKE.Name("XObject"),
                    "Subtype": MAKE.Name("Form"),
                    "BBox": [0, 0, 10, 10],
                    "Resources": {"Font": resources["Font"], "XObject": {"Fx": form}},
                },
                body,
            ),
        )
        return {**resources, "XObject": {"Fx": form}}

    content = interpret(b"/Fx Do", extra={"build": build})
    assert texts(content) == ["x"] * MAX_FORM_DEPTH
    assert content.skipped == 1


def test_the_operation_bound_stops_the_page() -> None:
    content = interpret(b"BT /F1 10 Tf 72 700 Td (A) Tj (B) Tj (C) Tj ET", max_operations=4)
    assert content.limited == "max_operations" and texts(content) == ["A"]


def test_marked_content_gives_mcids_and_the_outermost_artifact() -> None:
    def build(pdf: Any, resources: dict[str, Any]) -> dict[str, Any]:
        return {**resources, "Properties": {"MC0": {"MCID": 7}}}

    content = interpret(
        b"/P /MC0 BDC BT /F1 10 Tf 72 700 Td (A) Tj ET EMC "
        b"/Artifact BMC /Artifact <</Subtype /Footer>> BDC BT /F1 10 Tf 72 40 Td (B) Tj ET EMC EMC "
        b"/Artifact <</Type /Pagination /Subtype /Header>> BDC q 10 0 0 10 0 0 cm /Im Do Q EMC",
        extra={"build": build},
    )
    first, second = content.items[0], content.items[1]
    assert (first.mcid, first.artifact) == (7, None)
    assert (second.mcid, second.artifact, second.artifact_kind) == (None, 0, "")
    assert content.skipped == 1  # /Im is not in the resources


# --- Blocks ------------------------------------------------------------------------------------


def run_item(sequence: int, text: str | None, origin: tuple[float, float], width: float) -> Item:
    end = (origin[0] + width, origin[1])
    line = Line(origin, end, (1.0, 0.0), 10.0) if text is not None else None
    return Item(
        sequence, text, 0, Box(origin[0], origin[1], end[0], origin[1] + 10), line, None, None, ""
    )


@pytest.mark.parametrize(
    ("second", "joined"),
    [
        (("world", (130.0, 700.0)), "hello world"),  # a 10-point gap on the line
        (("world", (100.5, 700.0)), "helloworld"),  # touching
        (("world", (72.0, 686.0)), "hello\nworld"),  # the next line
        ((" world", (130.0, 700.0)), "hello world"),  # its own space, not a second one
    ],
)
def test_runs_join_by_where_they_are(second: tuple[str, tuple[float, float]], joined: str) -> None:
    first = run_item(0, "hello", (72.0, 700.0), 28.0)
    assert join([first, run_item(1, second[0], second[1], 25.0)], 0.2) == joined


def test_runs_without_geometry_join_by_one_space() -> None:
    first = Item(0, "a", 0, None, None, None, None, "")
    assert join([first, Item(1, "b", 0, None, None, None, None, "")], 0.2) == "a b"


def test_an_untagged_page_is_a_block_per_run_and_per_image() -> None:
    items = [
        run_item(0, "Title", (72.0, 700.0), 30.0),
        run_item(1, None, (72.0, 600.0), 50.0),
        run_item(2, "Body", (72.0, 500.0), 20.0),
    ]
    found = blocks(items, None, 0.2)
    assert [(b.role, b.text, b.textual, b.start, b.end) for b in found] == [
        (None, "Title", True, 0, 5),
        (BlockRole.FIGURE, PLACEHOLDER, False, 6, 7),
        (None, "Body", True, 8, 12),
    ]


def test_an_artifact_sequence_is_one_block_after_the_content() -> None:
    header = [
        Item(0, "Plant", 0, None, None, None, 0, "Header"),
        Item(2, "SOP", 0, None, None, None, 0, "Header"),
    ]
    found = blocks([*header, run_item(1, "Body", (72.0, 500.0), 20.0)], None, 0.2)
    assert [(b.role, b.text) for b in found] == [(BlockRole.HEADER, "Plant SOP"), (None, "Body")]
