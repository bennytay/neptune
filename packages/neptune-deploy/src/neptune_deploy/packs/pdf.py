"""A minimal, deterministic PDF 1.4 writer (ADR 0013 §9).

Text only: lines placed at absolute positions in the standard Type1 fonts with WinAnsiEncoding.
No compression (a compressor's output can differ between library versions), no timestamps but
the fixed ones the caller passes, a file ``/ID`` the caller derives from content, and a
cross-reference table computed from the bytes written. Same pages and metadata, same bytes.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

PAGE_WIDTH: Final = 595  # A4, points
PAGE_HEIGHT: Final = 842
FONTS: Final[Mapping[str, str]] = {
    "F1": "Helvetica-Bold",
    "F2": "Courier",
    "F3": "Courier-Bold",
    "F4": "Courier-Oblique",
}


@dataclass(frozen=True)
class PlacedLine:
    font: str  # a key of FONTS
    size: float
    x: float
    y: float
    text: str  # every character WinAnsi-encodable (``text.winansi``)


def _number(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def literal(text: str) -> bytes:
    """A PDF literal string of WinAnsi text: ``\\``, ``(`` and ``)`` escaped, bytes above 0x7E
    as octal escapes, so the file stays 7-bit."""
    out = bytearray(b"(")
    for byte in text.encode("cp1252"):
        if byte in b"\\()":
            out += b"\\" + bytes([byte])
        elif 0x20 <= byte <= 0x7E:
            out.append(byte)
        else:
            out += f"\\{byte:03o}".encode("ascii")
    out += b")"
    return bytes(out)


def _content(lines: Sequence[PlacedLine]) -> bytes:
    out = bytearray()
    for line in lines:
        if line.font not in FONTS:
            raise ValueError(f"unknown font {line.font!r}")
        out += (
            f"BT /{line.font} {_number(line.size)} Tf 1 0 0 1 {_number(line.x)}"
            f" {_number(line.y)} Tm "
        ).encode("ascii")
        out += literal(line.text) + b" Tj ET\n"
    return bytes(out)


def write_pdf(
    pages: Sequence[Sequence[PlacedLine]], info: Mapping[str, str], file_id: bytes
) -> bytes:
    """The PDF bytes of ``pages``. ``info`` values must be printable ASCII; ``file_id`` is the 16
    bytes both halves of the trailer's ``/ID`` carry."""
    if len(file_id) != 16:
        raise ValueError("a file id is 16 bytes")
    if not pages:
        raise ValueError("a PDF has at least one page")
    for key, value in info.items():
        if not key.isalpha() or not all(0x20 <= ord(c) <= 0x7E for c in value):
            raise ValueError(f"info {key!r} is not a name with printable ASCII text")
    font_ids = {name: 4 + i for i, name in enumerate(FONTS)}
    first_page = 4 + len(FONTS)
    page_ids = [first_page + 2 * i for i in range(len(pages))]
    fonts = " ".join(f"/{name} {font_ids[name]} 0 R" for name in FONTS)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}]"
            f" /Count {len(pages)} /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}]"
            f" /Resources << /Font << {fonts} >> >> >>"
        ).encode("ascii"),
        b"<< "
        + b" ".join(f"/{key} ".encode("ascii") + literal(info[key]) for key in sorted(info))
        + b" >>",
    ]
    objects += [
        f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} /Encoding /WinAnsiEncoding >>".encode(
            "ascii"
        )
        for base in FONTS.values()
    ]
    for page_id, lines in zip(page_ids, pages, strict=True):
        stream = _content(lines)
        objects.append(f"<< /Type /Page /Parent 2 0 R /Contents {page_id + 1} 0 R >>".encode())
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    hex_id = file_id.hex().upper()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 3 0 R"
        f" /ID [<{hex_id}> <{hex_id}>] >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)
