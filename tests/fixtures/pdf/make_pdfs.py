"""Generate the PDF fixtures: robotics documents, their corruptions, and hostile files.

Run ``uv run python tests/fixtures/pdf/make_pdfs.py`` to rewrite the committed files. Every file is
built from constants here by a small PDF writer of our own, with no clock, randomness, host path
or third-party library, so ``build()`` is byte-for-byte deterministic on every host;
``tests/unit/adapters/test_pdf_fixtures.py`` checks the committed files against it. Compressed
streams are written by hand (stored or fixed-Huffman deflate blocks), never by ``zlib``, whose
output differs between zlib builds. ``README.md`` lists the files and what each must produce.
"""

import hashlib
import struct
import zlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, TypeAlias

HERE: Final = Path(__file__).parent
MiB: Final = 1 << 20

# --- PDF objects ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Name:
    value: str


@dataclass(frozen=True)
class Ref:
    number: int


@dataclass(frozen=True)
class Lit:
    """A literal string; encrypted with its object when the file is."""

    value: bytes


@dataclass(frozen=True)
class Hex:
    value: bytes


@dataclass(frozen=True)
class Raw:
    """Bytes written as they are (a deliberately malformed token, a deep nesting)."""

    value: bytes


@dataclass(frozen=True)
class Stream:
    entries: "Mapping[str, Obj]"
    data: bytes


Obj: TypeAlias = (
    "None | bool | int | float | Name | Ref | Lit | Hex | Raw | list[Obj] | dict[str, Obj] | Stream"
)
Cipher: TypeAlias = Callable[[bytes], bytes]


def _number(value: float) -> bytes:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return (text if text not in ("-0", "") else "0").encode()


def _literal(data: bytes) -> bytes:
    escaped = data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    return b"(" + escaped.replace(b"\r", b"\\r") + b")"


def serialize(obj: Obj, cipher: Cipher | None = None) -> bytes:
    match obj:
        case None:
            return b"null"
        case bool():
            return b"true" if obj else b"false"
        case int():
            return str(obj).encode()
        case float():
            return _number(obj)
        case Name(value):
            return b"/" + value.encode("latin-1")
        case Ref(number):
            return f"{number} 0 R".encode()
        case Lit(value):
            return _literal(cipher(value) if cipher else value)
        case Hex(value):
            return b"<" + (cipher(value) if cipher else value).hex().upper().encode() + b">"
        case Raw(value):
            return value
        case list():
            return b"[" + b" ".join(serialize(item, cipher) for item in obj) + b"]"
        case dict():
            parts = [b"/" + k.encode() + b" " + serialize(v, cipher) for k, v in obj.items()]
            return b"<<" + b" ".join(parts) + b">>"
        case Stream(entries, data):
            payload = cipher(data) if cipher else data
            head = serialize({**entries, "Length": len(payload)}, cipher)
            return head + b"\nstream\n" + payload + b"\nendstream"
    raise TypeError(f"not a PDF object: {obj!r}")


# --- Deflate, by hand ----------------------------------------------------------------------------


def stored_deflate(data: bytes) -> bytes:
    """A zlib stream of stored (uncompressed) deflate blocks: the same bytes on every host."""
    out = bytearray(b"\x78\x01")
    pieces = [data[i : i + 0xFFFF] for i in range(0, len(data), 0xFFFF)] or [b""]
    for index, piece in enumerate(pieces):
        out.append(1 if index == len(pieces) - 1 else 0)
        out += struct.pack("<HH", len(piece), len(piece) ^ 0xFFFF) + piece
    return bytes(out + struct.pack(">I", zlib.adler32(data)))


class _Bits:
    def __init__(self) -> None:
        self.out, self.acc, self.count = bytearray(), 0, 0

    def put(self, value: int, width: int) -> None:
        self.acc |= value << self.count
        self.count += width
        while self.count >= 8:
            self.out.append(self.acc & 0xFF)
            self.acc >>= 8
            self.count -= 8

    def huffman(self, code: int, width: int) -> None:
        self.put(int(f"{code:0{width}b}"[::-1], 2), width)

    def done(self) -> bytes:
        if self.count:
            self.out.append(self.acc & 0xFF)
        return bytes(self.out)


def bomb_deflate(byte: int, size: int) -> bytes:
    """``size`` copies of one byte as fixed-Huffman deflate: 13 bits per 258 bytes."""
    assert 32 <= byte < 144 and size >= 1
    bits = _Bits()
    bits.put(1, 1)  # BFINAL
    bits.put(1, 2)  # BTYPE 01: fixed Huffman
    bits.huffman(0x30 + byte, 8)
    left = size - 1
    while left >= 258:
        bits.huffman(0b11000101, 8)  # length symbol 285: 258 bytes
        bits.huffman(0, 5)  # distance code 0: 1 byte back
        left -= 258
    for _ in range(left):
        bits.huffman(0x30 + byte, 8)
    bits.huffman(0, 7)  # end of block
    adler = zlib.adler32(bytes([byte]) * size)
    return b"\x78\x01" + bits.done() + struct.pack(">I", adler)


# --- RC4 and the standard security handler (PDF 1.7 §7.6.3, revision 3) ------------------------

PAD: Final = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
FILE_ID: Final = hashlib.md5(b"neptune pdf fixtures").digest()


def rc4(key: bytes, data: bytes) -> bytes:
    s, j = list(range(256)), 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    out, i, j = bytearray(), 0, 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def _rounds(key: bytes, data: bytes) -> bytes:
    for i in range(20):
        data = rc4(bytes(b ^ i for b in key), data)
    return data


@dataclass(frozen=True)
class Rc4Security:
    user: bytes
    owner: bytes
    permissions: int = -3904

    def owner_entry(self) -> bytes:
        digest = hashlib.md5((self.owner + PAD)[:32]).digest()
        for _ in range(50):
            digest = hashlib.md5(digest).digest()
        return _rounds(digest[:16], (self.user + PAD)[:32])

    def key(self) -> bytes:
        seed = (self.user + PAD)[:32] + self.owner_entry()
        digest = hashlib.md5(seed + struct.pack("<i", self.permissions) + FILE_ID).digest()
        for _ in range(50):
            digest = hashlib.md5(digest[:16]).digest()
        return digest[:16]

    def user_entry(self) -> bytes:
        return _rounds(self.key(), hashlib.md5(PAD + FILE_ID).digest()) + bytes(16)

    def dictionary(self) -> dict[str, Obj]:
        return {
            "Filter": Name("Standard"),
            "V": 2,
            "R": 3,
            "Length": 128,
            "O": Hex(self.owner_entry()),
            "U": Hex(self.user_entry()),
            "P": self.permissions,
        }

    def cipher(self, number: int) -> Cipher:
        salt = number.to_bytes(3, "little") + bytes(2)
        key = hashlib.md5(self.key() + salt).digest()[:16]
        return lambda data: rc4(key, data)


# --- The writer ----------------------------------------------------------------------------------


@dataclass
class Pdf:
    """Objects numbered from 1; ``build`` lays them out with a cross-reference table or stream."""

    version: str = "1.7"
    objects: dict[int, Obj] = field(default_factory=dict)

    def add(self, obj: Obj) -> Ref:
        ref = self.reserve()
        self.objects[ref.number] = obj
        return ref

    def reserve(self) -> Ref:
        number = len(self.objects) + 1
        self.objects[number] = None
        return Ref(number)

    def set(self, ref: Ref, obj: Obj) -> None:
        self.objects[ref.number] = obj

    def header(self) -> bytes:
        return f"%PDF-{self.version}\n".encode() + b"%\xe2\xe3\xcf\xd3\n"

    def body(
        self, numbers: Iterable[int], start: int, security: Rc4Security | None = None
    ) -> tuple[bytes, dict[int, int]]:
        out, offsets = bytearray(), {}
        for number in numbers:
            offsets[number] = start + len(out)
            cipher = security.cipher(number) if security is not None else None
            obj = serialize(self.objects[number], cipher)
            out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
        return bytes(out), offsets

    def build(
        self,
        root: Ref,
        info: Ref | None = None,
        *,
        security: Rc4Security | None = None,
        trailer: Mapping[str, Obj] | None = None,
    ) -> bytes:
        """A classic file: header, every object, an xref table and a trailer."""
        encrypt: Ref | None = None
        if security is not None:
            encrypt = self.add(security.dictionary())
        head = self.header()
        numbers = sorted(self.objects)
        plain = {encrypt.number} if encrypt is not None else set()
        out, offsets = bytearray(head), {}
        for number in numbers:
            chunk, found = self.body(
                [number], len(out), None if number in plain else security
            )
            out += chunk
            offsets.update(found)
        xref = len(out)
        size = max(numbers) + 1
        out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
        for number in range(1, size):
            out += f"{offsets[number]:010d} 00000 n \n".encode()
        entries: dict[str, Obj] = {"Size": size, "Root": root}
        if info is not None:
            entries["Info"] = info
        if encrypt is not None:
            entries["Encrypt"] = encrypt
        entries["ID"] = [Hex(FILE_ID), Hex(FILE_ID)]
        entries.update(trailer or {})
        out += b"trailer\n" + serialize(entries) + f"\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)


# --- Content streams -----------------------------------------------------------------------------


def text(font: str, size: float, x: float, y: float, shown: bytes) -> bytes:
    return b"BT /%s %s Tf %s %s Td %s Tj ET\n" % (
        font.encode(),
        _number(size),
        _number(x),
        _number(y),
        _literal(shown),
    )


def marked(tag: str, mcid: int, body: bytes) -> bytes:
    return b"/%s <</MCID %d>> BDC\n" % (tag.encode(), mcid) + body + b"EMC\n"


def artifact(subtype: str, body: bytes) -> bytes:
    head = b"/Artifact <</Type /Pagination /Subtype /%s>> BDC\n" % subtype.encode()
    return head + body + b"EMC\n"


# --- Fonts and resources -------------------------------------------------------------------------

HELVETICA: Final[dict[str, Obj]] = {
    "Type": Name("Font"),
    "Subtype": Name("Type1"),
    "BaseFont": Name("Helvetica"),
    "Encoding": Name("WinAnsiEncoding"),
}
HELVETICA_BOLD: Final[dict[str, Obj]] = {**HELVETICA, "BaseFont": Name("Helvetica-Bold")}


def to_unicode_cmap(pairs: Mapping[int, str], width: int = 1, ranges: str = "") -> bytes:
    """A ToUnicode CMap mapping each code (``width`` bytes) to its text."""
    hexes = "".join(
        f"<{code:0{2 * width}X}> <{text.encode('utf-16-be').hex().upper()}>\n"
        for code, text in sorted(pairs.items())
    )
    low, high = "0" * (2 * width), "F" * (2 * width)
    return (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CMapName /Neptune-UCS def\n/CMapType 2 def\n"
        f"1 begincodespacerange\n<{low}> <{high}>\nendcodespacerange\n"
        f"{len(pairs)} beginbfchar\n{hexes}endbfchar\n{ranges}"
        "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode()


def sop_sans(pdf: Pdf) -> Ref:
    """A simple TrueType font with declared widths, descriptor and a ToUnicode for its bullet."""
    descriptor = pdf.add(
        {
            "Type": Name("FontDescriptor"),
            "FontName": Name("SOPSans"),
            "Flags": 32,
            "FontBBox": [-100, -250, 1000, 900],
            "ItalicAngle": 0,
            "Ascent": 750,
            "Descent": -250,
            "CapHeight": 700,
            "StemV": 80,
        }
    )
    unicode = pdf.add(Stream({}, to_unicode_cmap({0x95: "•"})))
    widths: list[Obj] = [250 if code == 32 else 520 for code in range(32, 151)]
    return pdf.add(
        {
            "Type": Name("Font"),
            "Subtype": Name("TrueType"),
            "BaseFont": Name("SOPSans"),
            "FirstChar": 32,
            "LastChar": 150,
            "Widths": widths,
            "FontDescriptor": descriptor,
            "Encoding": Name("WinAnsiEncoding"),
            "ToUnicode": unicode,
        }
    )


def gray_image(pdf: Pdf) -> Ref:
    entries: dict[str, Obj] = {
        "Type": Name("XObject"),
        "Subtype": Name("Image"),
        "Width": 2,
        "Height": 2,
        "ColorSpace": Name("DeviceGray"),
        "BitsPerComponent": 8,
    }
    return pdf.add(Stream(entries, b"\x00\x80\x80\xff"))


def catalog(pdf: Pdf, pages: Ref, **entries: Obj) -> Ref:
    return pdf.add({"Type": Name("Catalog"), "Pages": pages, **entries})


def page_tree(pdf: Pdf, kids: Sequence[Ref], tree: Ref | None = None, **entries: Obj) -> Ref:
    tree = tree or pdf.reserve()
    pdf.set(tree, {"Type": Name("Pages"), "Kids": list(kids), "Count": len(kids), **entries})
    return tree


def page(
    pdf: Pdf,
    parent: Ref,
    content: bytes | Ref | None,
    resources: Mapping[str, Obj],
    *,
    box: Sequence[float] = (0, 0, 612, 792),
    compress: Callable[[bytes], bytes] | None = None,
    **entries: Obj,
) -> Ref:
    page_dict: dict[str, Obj] = {
        "Type": Name("Page"),
        "Parent": parent,
        "MediaBox": [float(v) if isinstance(v, float) else v for v in box],
        "Resources": dict(resources),
        **entries,
    }
    if isinstance(content, bytes):
        if compress is None:
            page_dict["Contents"] = pdf.add(Stream({}, content))
        else:
            stream = Stream({"Filter": Name("FlateDecode")}, compress(content))
            page_dict["Contents"] = pdf.add(stream)
    elif isinstance(content, Ref):
        page_dict["Contents"] = content
    return pdf.add(page_dict)


# --- The pump-room SOP: tagged, two pages --------------------------------------------------------


def _element(
    pdf: Pdf, ref: Ref, kind: str, parent: Ref, kids: list[Obj], page_ref: Ref | None = None
) -> None:
    element: dict[str, Obj] = {"Type": Name("StructElem"), "S": Name(kind), "P": parent}
    if page_ref is not None:
        element["Pg"] = page_ref
    element["K"] = kids
    pdf.set(ref, element)


def pump_sop(security: Rc4Security | None = None) -> bytes:
    """The tagged SOP: headings, paragraphs, a list, a figure, a table and page artifacts."""
    pdf = Pdf()
    tree = pdf.reserve()
    f1, f2, f3 = pdf.add(HELVETICA), pdf.add(HELVETICA_BOLD), sop_sans(pdf)
    image = gray_image(pdf)
    fonts: dict[str, Obj] = {"F1": f1, "F2": f2, "F3": f3}
    resources: dict[str, Obj] = {"Font": fonts, "XObject": {"Im1": image}}
    first = (
        artifact("Header", text("F1", 9, 72, 760, b"North plant \xb7 Pump room SOP"))
        + marked("H1", 0, text("F2", 18, 72, 720, b"Pump room start-up"))
        + marked(
            "P",
            1,
            b"BT /F1 11 Tf 14 TL 72 690 Td (Close valve V-12 before entering the pump room.) Tj"
            b" T* (Check that the pressure gauge reads below 2 bar.) Tj ET\n",
        )
        + marked("Lbl", 2, text("F3", 11, 90, 650, b"\x95"))
        + marked("LBody", 3, text("F3", 11, 104, 650, b"Start pump P-2 from the local panel."))
        + marked("Figure", 4, b"q 120 0 0 80 72 520 cm /Im1 Do Q\n")
        + marked("Caption", 5, text("F1", 9, 72, 505, b"Figure 1: Valve V-12, closed."))
        + artifact("Footer", text("F1", 9, 300, 40, b"Page i"))
    )
    cells = [
        (b"Bolt", 72.0, 700.0, "TH", "F2"),
        (b"Torque", 172.0, 700.0, "TH", "F2"),
        (b"Unit", 272.0, 700.0, "TH", "F2"),
        (b"M8", 72.0, 684.0, "TD", "F1"),
        (b"25", 172.0, 684.0, "TD", "F1"),
        (b"N\xb7m", 272.0, 684.0, "TD", "F1"),
        (b"M10", 72.0, 668.0, "TD", "F1"),
        (b"N\xb7m", 272.0, 668.0, "TD", "F1"),
    ]
    second = marked("H2", 0, text("F2", 14, 72, 730, b"Torque table"))
    for index, (shown, x, y, tag, font) in enumerate(cells, start=1):
        second += marked(tag, index, text(font, 10, x, y, shown))
    second += marked(
        "Normal",
        9,
        b"BT /F1 11 Tf 72 630 Td [(T) 70 (orque) -333 (with) -333 (a) -333 (calibrated)"
        b" -333 (wrench.)] TJ ET\n",
    )
    second += text("F1", 8, 500, 40, b"Rev 3")  # untagged content on a tagged page

    one, two = pdf.reserve(), pdf.reserve()
    page_one = page(pdf, tree, first, resources, StructParents=0)
    page_two = page(pdf, tree, second, resources, StructParents=1)
    root_elem, document = pdf.reserve(), pdf.reserve()
    h1, p, lst, li, lbl, lbody, fig, cap = (pdf.reserve() for _ in range(8))
    h2, table, p2 = pdf.reserve(), pdf.reserve(), pdf.reserve()
    rows = [pdf.reserve() for _ in range(3)]
    cell_refs = [pdf.reserve() for _ in range(9)]
    _element(pdf, h1, "H1", document, [0], page_one)
    _element(pdf, p, "P", document, [1], page_one)
    _element(pdf, lbl, "Lbl", li, [2], page_one)
    _element(pdf, lbody, "LBody", li, [3], page_one)
    _element(pdf, li, "LI", lst, [lbl, lbody])
    _element(pdf, lst, "L", document, [li])
    _element(pdf, fig, "Figure", document, [4], page_one)
    _element(pdf, cap, "Caption", document, [5], page_one)
    _element(pdf, h2, "H2", document, [0], page_two)
    # Row 2's middle cell is empty: a TD with no content.
    layout = [[1, 2, 3], [4, 5, 6], [7, None, 8]]
    for row_index, row in enumerate(layout):
        kids: list[Obj] = []
        for column, mcid in enumerate(row):
            cell = cell_refs[row_index * 3 + column]
            kind = "TH" if row_index == 0 else "TD"
            _element(pdf, cell, kind, rows[row_index], [] if mcid is None else [mcid], page_two)
            kids.append(cell)
        _element(pdf, rows[row_index], "TR", table, kids)
    _element(pdf, table, "Table", document, list[Obj](rows))
    _element(pdf, p2, "Normal", document, [9], page_two)
    _element(pdf, document, "Document", root_elem, [h1, p, lst, fig, cap, h2, table, p2])
    by_mcid_two: list[Obj] = [h2, *[cell_refs[i] for i in (0, 1, 2, 3, 4, 5, 6, 8)], p2]
    parent_tree = pdf.add({"Nums": [0, [h1, p, lbl, lbody, fig, cap], 1, by_mcid_two]})
    pdf.set(
        root_elem,
        {
            "Type": Name("StructTreeRoot"),
            "K": [document],
            "ParentTree": parent_tree,
            "ParentTreeNextKey": 2,
            "RoleMap": {"Normal": Name("P")},
        },
    )
    pdf.set(one, {"S": Name("r")})
    pdf.set(two, {"S": Name("D"), "P": Lit(b"A-")})
    page_tree(pdf, [page_one, page_two], tree)
    root = catalog(
        pdf,
        tree,
        StructTreeRoot=root_elem,
        MarkInfo={"Marked": True},
        PageLabels={"Nums": [0, one, 1, two]},
        Lang=Lit(b"en-GB"),
    )
    info = pdf.add(
        {
            "Title": Lit(b"Pump room SOP"),
            "Author": Lit(b"Operations"),
            "Producer": Lit(b"neptune fixture generator"),
        }
    )
    return pdf.build(root, info, security=security)


# --- The gripper datasheet: untagged, two pages --------------------------------------------------


def cid_font(pdf: Pdf, glyphs: str) -> Ref:
    """A Type0 font, Identity-H: CID n is the n-th character of ``glyphs`` (from 1)."""
    descriptor = pdf.add(
        {
            "Type": Name("FontDescriptor"),
            "FontName": Name("GripperCID"),
            "Flags": 32,
            "FontBBox": [-50, -200, 1000, 800],
            "ItalicAngle": 0,
            "Ascent": 800,
            "Descent": -200,
            "CapHeight": 700,
            "StemV": 80,
        }
    )
    descendant = pdf.add(
        {
            "Type": Name("Font"),
            "Subtype": Name("CIDFontType2"),
            "BaseFont": Name("GripperCID"),
            "CIDSystemInfo": {"Registry": Lit(b"Adobe"), "Ordering": Lit(b"Identity"), "Supplement": 0},
            "FontDescriptor": descriptor,
            "DW": 1000,
            "W": [1, [600] * len(glyphs)],
        }
    )
    mapping = {cid: char for cid, char in enumerate(glyphs, start=1)}
    unicode = pdf.add(Stream({}, to_unicode_cmap(mapping, width=2)))
    return pdf.add(
        {
            "Type": Name("Font"),
            "Subtype": Name("Type0"),
            "BaseFont": Name("GripperCID"),
            "Encoding": Name("Identity-H"),
            "DescendantFonts": [descendant],
            "ToUnicode": unicode,
        }
    )


CID_GLYPHS: Final = "Payload 2kg"


def cid_text(shown: str) -> bytes:
    return b"".join((CID_GLYPHS.index(char) + 1).to_bytes(2, "big") for char in shown)


def gripper_datasheet(*, corrupt_first_page: bool = False) -> bytes:
    """An untagged two-page datasheet: kerning, line operators, forms, images, a rotated page."""
    pdf = Pdf(version="1.6")
    tree = pdf.reserve()
    f1, f2 = pdf.add(HELVETICA), pdf.add(HELVETICA_BOLD)
    image = gray_image(pdf)
    form = pdf.add(
        Stream(
            {
                "Type": Name("XObject"),
                "Subtype": Name("Form"),
                "BBox": [0, 0, 60, 12],
                "Resources": {"Font": {"F1": f1}},
            },
            b"BT /F1 8 Tf 0 2 Td (Rev C) Tj ET\n",
        )
    )
    first = (
        text("F2", 20, 56, 780, b"GX-2 Parallel Gripper")
        + b"BT /F1 10 Tf 56 750 Td [(Stroke) -333 (per) -333 (jaw:) -333 (40) -333 (mm)] TJ ET\n"
        + b"BT /F1 10 Tf 12 TL 56 720 Td (Grip force: 140 N) Tj"
        + b" (Repeatability: 0.02 mm) ' 2 0.5 (Weight: 0.9 kg) \" ET\n"
        + b"q 1.5 0 0 1.5 0 0 cm BT /F1 10 Tf 37.3333 400 Td (Scaled note) Tj ET Q\n"
        + b"q 1 0 0 1 500 40 cm /Fm1 Do Q\nq 1 0 0 1 500 800 cm /Fm1 Do Q\n"
        + b"q 50 0 0 50 56 600 cm BI /W 2 /H 2 /CS /G /BPC 8 ID \x10\x20\x30\x40 EI Q\n"
        + b"q 100 0 0 60 300 600 cm /Im1 Do Q\n"
    )
    differences = pdf.add(
        {
            "Type": Name("Font"),
            "Subtype": Name("Type1"),
            "BaseFont": Name("Helvetica"),
            "Encoding": {
                "Type": Name("Encoding"),
                "BaseEncoding": Name("WinAnsiEncoding"),
                "Differences": [1, Name("bullet"), Name("g42")],
            },
        }
    )
    second = (
        b"BT /F4 12 Tf 56 500 Td <" + cid_text("Payload 2 kg").hex().upper().encode() + b"> Tj ET\n"
        + text("F5", 11, 56, 470, b"\x01 Safe zone")
        + text("F5", 11, 56, 450, b"Marker \x02")
    )
    resources_one: dict[str, Obj] = {
        "Font": {"F1": f1, "F2": f2},
        "XObject": {"Fm1": form, "Im1": image},
    }
    resources_two: dict[str, Obj] = {
        "Font": {"F4": cid_font(pdf, CID_GLYPHS), "F5": differences}
    }

    def broken(data: bytes) -> bytes:
        stored = bytearray(stored_deflate(data))
        stored[3:5] = b"\x00\x00"  # LEN no longer matches NLEN: the stream cannot inflate
        return bytes(stored)

    a4 = (0, 0, 595, 842)
    page_one = page(
        pdf, tree, first, resources_one, box=a4, compress=broken if corrupt_first_page else None
    )
    page_two = page(pdf, tree, second, resources_two, box=(0, 0, 842, 595), Rotate=90)
    page_tree(pdf, [page_one, page_two], tree)
    info = pdf.add({"Producer": Lit(b"neptune fixture generator"), "Subject": Lit(b"GX-2")})
    return pdf.build(catalog(pdf, tree), info)


# --- The site manifest: object streams, an xref stream and an incremental update ----------------


def site_manifest() -> bytes:
    """Compressed object streams and an xref stream; an incremental update retitles it."""
    pdf = Pdf()
    tree, font, page_ref, root, info = (pdf.reserve() for _ in range(5))
    pdf.set(font, HELVETICA)
    lines = [
        b"Site manifest: North plant",
        b"Asset P-2  centrifugal pump  bay 3",
        b"Asset V-12  gate valve  bay 3",
    ]
    content = b"BT /F1 11 Tf 14 TL 72 720 Td " + b" ".join(
        _literal(line) + (b" Tj" if index == 0 else b" '") for index, line in enumerate(lines)
    ) + b" ET\n"
    contents = pdf.add(Stream({"Filter": Name("FlateDecode")}, stored_deflate(content)))
    pdf.set(
        page_ref,
        {
            "Type": Name("Page"),
            "Parent": tree,
            "MediaBox": [0, 0, 612, 792],
            "Resources": {"Font": {"F1": font}},
            "Contents": contents,
        },
    )
    pdf.set(tree, {"Type": Name("Pages"), "Kids": [page_ref], "Count": 1})
    pdf.set(root, {"Type": Name("Catalog"), "Pages": tree})
    pdf.set(info, {"Title": Lit(b"Draft"), "Producer": Lit(b"neptune fixture generator")})

    packed = [font.number, page_ref.number, tree.number, root.number, info.number]
    header, bodies = bytearray(), bytearray()
    for number in packed:
        header += f"{number} {len(bodies)} ".encode()
        bodies += serialize(pdf.objects[number]) + b"\n"
    objstm_number = max(pdf.objects) + 1
    objstm = Stream(
        {"Type": Name("ObjStm"), "N": len(packed), "First": len(header), "Filter": Name("FlateDecode")},
        stored_deflate(bytes(header) + bytes(bodies)),
    )
    out = bytearray(pdf.header())
    offsets: dict[int, int] = {}
    for number in (contents.number, objstm_number):
        offsets[number] = len(out)
        obj = pdf.objects[number] if number != objstm_number else objstm
        out += f"{number} 0 obj\n".encode() + serialize(obj) + b"\nendobj\n"
    xref_number = objstm_number + 1
    size = xref_number + 1
    rows = bytearray(b"\x00" + struct.pack(">IH", 0, 0xFFFF))
    for number in range(1, size):
        if number in packed:
            rows += b"\x02" + struct.pack(">IH", objstm_number, packed.index(number))
        elif number == xref_number:
            rows += b"\x01" + struct.pack(">IH", len(out), 0)
        else:
            rows += b"\x01" + struct.pack(">IH", offsets[number], 0)
    xref_offset = len(out)
    xref = Stream(
        {
            "Type": Name("XRef"),
            "Size": size,
            "W": [1, 4, 2],
            "Root": root,
            "Info": info,
            "ID": [Hex(FILE_ID), Hex(FILE_ID)],
            "Filter": Name("FlateDecode"),
        },
        stored_deflate(bytes(rows)),
    )
    out += f"{xref_number} 0 obj\n".encode() + serialize(xref) + b"\nendobj\n"
    out += f"startxref\n{xref_offset}\n%%EOF\n".encode()

    # The update: a new Info object, a classic xref section and a trailer naming the old one.
    retitled = len(out)
    new_info = size
    out += f"{new_info} 0 obj\n".encode()
    out += serialize({"Title": Lit(b"Site manifest"), "Producer": Lit(b"neptune fixture generator")})
    out += b"\nendobj\n"
    update = len(out)
    out += f"xref\n{new_info} 1\n{retitled:010d} 00000 n \n".encode()
    trailer: dict[str, Obj] = {
        "Size": new_info + 1,
        "Root": root,
        "Info": Ref(new_info),
        "Prev": xref_offset,
        "ID": [Hex(FILE_ID), Hex(FILE_ID)],
    }
    out += b"trailer\n" + serialize(trailer) + f"\nstartxref\n{update}\n%%EOF\n".encode()
    return bytes(out)


def blank() -> bytes:
    """One page, declared and empty: a document with a page and no blocks."""
    pdf = Pdf()
    tree = pdf.reserve()
    blank_page = page(pdf, tree, None, {}, box=(0, 0, 612, 792))
    page_tree(pdf, [blank_page], tree)
    return pdf.build(catalog(pdf, tree))


def aes_declared() -> bytes:
    """Declares AES encryption (V4, AESV2), which the adapter does not decrypt."""
    pdf = Pdf()
    tree = pdf.reserve()
    only = page(pdf, tree, text("F1", 11, 72, 720, b"Never read"), {"Font": {"F1": HELVETICA}})
    page_tree(pdf, [only], tree)
    encrypt = pdf.add(
        {
            "Filter": Name("Standard"),
            "V": 4,
            "R": 4,
            "Length": 128,
            "CF": {"StdCF": {"CFM": Name("AESV2"), "AuthEvent": Name("DocOpen"), "Length": 16}},
            "StmF": Name("StdCF"),
            "StrF": Name("StdCF"),
            "O": Hex(bytes(32)),
            "U": Hex(bytes(32)),
            "P": -3904,
        }
    )
    return pdf.build(catalog(pdf, tree), trailer={"Encrypt": encrypt})


# --- Hostile files -------------------------------------------------------------------------------


def hostile_nesting() -> bytes:
    """Arrays nested 50,000 deep in a page's content and in the document information."""
    pdf = Pdf()
    tree = pdf.reserve()
    deep = Raw(b"[" * 50_000 + b"]" * 50_000)
    content = b"BT /F1 11 Tf 72 720 Td (Before the nesting) Tj ET\n" + deep.value + b" pop\n"
    first = page(pdf, tree, content, {"Font": {"F1": HELVETICA}})
    second = page(pdf, tree, text("F1", 11, 72, 720, b"After the nesting"), {"Font": {"F1": HELVETICA}})
    page_tree(pdf, [first, second], tree)
    info = pdf.add({"Title": deep})
    return pdf.build(catalog(pdf, tree), info)


BOMB_SIZE: Final = 24 * MiB  # inflates about 160:1 from fixed-Huffman blocks


def hostile_bomb() -> bytes:
    """A page whose content inflates to 24 MiB of spaces, beside a page that is fine."""
    pdf = Pdf()
    tree = pdf.reserve()
    fonts: dict[str, Obj] = {"Font": {"F1": HELVETICA}}
    bomb = pdf.add(Stream({"Filter": Name("FlateDecode")}, bomb_deflate(0x20, BOMB_SIZE)))
    first = page(pdf, tree, bomb, fonts)
    second = page(pdf, tree, text("F1", 11, 72, 720, b"Inspection checklist"), fonts)
    page_tree(pdf, [first, second], tree)
    return pdf.build(catalog(pdf, tree))


def hostile_active() -> bytes:
    """JavaScript on open and on a page, and an embedded file: declared, never run or read."""
    pdf = Pdf()
    tree = pdf.reserve()
    script = pdf.add({"S": Name("JavaScript"), "JS": Lit(b"app.alert('opened');")})
    payload = pdf.add(Stream({"Type": Name("EmbeddedFile")}, b"#!/bin/sh\necho payload\n"))
    spec = pdf.add(
        {"Type": Name("Filespec"), "F": Lit(b"payload.sh"), "UF": Lit(b"payload.sh"), "EF": {"F": payload}}
    )
    only = page(
        pdf,
        tree,
        text("F1", 11, 72, 720, b"Lockout tagout procedure"),
        {"Font": {"F1": HELVETICA}},
        AA={"O": script},
    )
    page_tree(pdf, [only], tree)
    names = {
        "EmbeddedFiles": {"Names": [Lit(b"payload.sh"), spec]},
        "JavaScript": {"Names": [Lit(b"init"), script]},
    }
    return pdf.build(catalog(pdf, tree, OpenAction=script, Names=names))


def hostile_xref() -> bytes:
    """A cross-reference section that claims two billion entries and a trailer that loops."""
    data = bytearray(blank())
    start = data.rindex(b"\nxref\n") + 1
    data[start : data.index(b"\n", start + 5) + 1] = b"xref\n0 2000000000\n"
    trailer = data.rindex(b"trailer\n<<") + len(b"trailer\n<<")
    data[trailer:trailer] = b"/Prev %d " % start
    return bytes(data)


def hostile_pages() -> bytes:
    """A page tree whose second kid is the tree itself."""
    pdf = Pdf()
    tree = pdf.reserve()
    only = page(pdf, tree, text("F1", 11, 72, 720, b"Loop guard"), {"Font": {"F1": HELVETICA}})
    pdf.set(tree, {"Type": Name("Pages"), "Kids": [only, tree], "Count": 2})
    return pdf.build(catalog(pdf, tree))


def hostile_objstm() -> bytes:
    """An object stream declaring a hundred million objects, holding the catalog's page tree."""
    data = bytearray(site_manifest())
    marker = b"/N 5"
    at = data.index(marker)
    data[at : at + len(marker)] = b"/N 100000000"
    # The longer number shifts every later offset; the reader must repair or refuse, never hang.
    return bytes(data)


# --- Corruptions ---------------------------------------------------------------------------------


def inspection_log() -> bytes:
    """A two-page log written catalog first, as many producers do; its second page is long."""
    pdf = Pdf()
    root, tree, info = pdf.reserve(), pdf.reserve(), pdf.reserve()
    font = pdf.add(HELVETICA)
    first, second = pdf.reserve(), pdf.reserve()
    first_content, second_content = pdf.reserve(), pdf.reserve()
    fonts: dict[str, Obj] = {"Font": {"F1": font}}
    for ref, content in ((first, first_content), (second, second_content)):
        page_dict: dict[str, Obj] = {
            "Type": Name("Page"),
            "Parent": tree,
            "MediaBox": [0, 0, 612, 792],
            "Resources": fonts,
            "Contents": content,
        }
        pdf.set(ref, page_dict)
    pdf.set(first_content, Stream({}, text("F1", 12, 72, 720, b"Inspection log: pump P-2")))
    lines = b"".join(
        text("F1", 10, 72, 700 - 12 * i, b"Reading %02d: 1.%d bar, nominal" % (i, i % 10))
        for i in range(40)
    )
    pdf.set(second_content, Stream({}, lines))
    pdf.set(root, {"Type": Name("Catalog"), "Pages": tree})
    page_tree(pdf, [first, second], tree)
    pdf.set(info, {"Title": Lit(b"Inspection log")})
    return pdf.build(root, info)


def truncated() -> bytes:
    """The inspection log cut inside its second page's content: no xref table, no trailer."""
    data = inspection_log()
    return data[: data.index(b"Reading 20")]


FIXTURES: Final[dict[str, Callable[[], bytes]]] = {
    "pump_sop.pdf": pump_sop,
    "gripper_datasheet.pdf": gripper_datasheet,
    "site_manifest.pdf": site_manifest,
    "blank.pdf": blank,
    "renamed_datasheet": gripper_datasheet,
    "empty.pdf": lambda: b"",
    "inspection_log.pdf": inspection_log,
    "truncated.pdf": truncated,
    "corrupted.pdf": lambda: gripper_datasheet(corrupt_first_page=True),
    "encrypted_user.pdf": lambda: pump_sop(Rc4Security(user=b"operator", owner=b"supervisor")),
    "encrypted_owner.pdf": lambda: pump_sop(Rc4Security(user=b"", owner=b"supervisor")),
    "encrypted_aes.pdf": aes_declared,
    "hostile_nesting.pdf": hostile_nesting,
    "hostile_bomb.pdf": hostile_bomb,
    "hostile_active.pdf": hostile_active,
    "hostile_xref.pdf": hostile_xref,
    "hostile_pages.pdf": hostile_pages,
    "hostile_objstm.pdf": hostile_objstm,
}


def build() -> dict[str, bytes]:
    return {name: make() for name, make in FIXTURES.items()}


if __name__ == "__main__":
    for name, data in build().items():
        (HERE / name).write_bytes(data)
