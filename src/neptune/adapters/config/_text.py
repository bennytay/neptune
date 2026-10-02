"""A configuration file's bytes as text: its encoding, byte-order mark and line endings.

UTF-8 unless a byte-order mark names UTF-16 or UTF-32; the mark is not part of the text. Every
span the adapter cites counts code points of that text, as ``Span`` does for the text adapter.
Invalid bytes are never replaced: the first one is reported and nothing of the file is read.
"""

import codecs
from dataclasses import dataclass
from typing import Final

from neptune.model.configuration import LineEndings, TextEncoding

# Longest first: a UTF-32 LE mark begins with the UTF-16 LE one.
_MARKS: Final[tuple[tuple[bytes, TextEncoding], ...]] = (
    (codecs.BOM_UTF32_LE, TextEncoding.UTF_32_LE),
    (codecs.BOM_UTF32_BE, TextEncoding.UTF_32_BE),
    (codecs.BOM_UTF8, TextEncoding.UTF_8),
    (codecs.BOM_UTF16_LE, TextEncoding.UTF_16_LE),
    (codecs.BOM_UTF16_BE, TextEncoding.UTF_16_BE),
)
# The width of one code unit, to turn the index of a failed unit back into a byte offset.
_UNIT: Final[dict[TextEncoding, int]] = {
    TextEncoding.UTF_8: 1,
    TextEncoding.UTF_16_LE: 2,
    TextEncoding.UTF_16_BE: 2,
    TextEncoding.UTF_32_LE: 4,
    TextEncoding.UTF_32_BE: 4,
}


def detect(data: bytes) -> tuple[TextEncoding, int]:
    """The encoding the bytes declare and the length of their byte-order mark (0 if none)."""
    for mark, encoding in _MARKS:
        if data.startswith(mark):
            return encoding, len(mark)
    return TextEncoding.UTF_8, 0


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: TextEncoding
    bom: int  # bytes of the byte-order mark before the text


@dataclass(frozen=True)
class InvalidEncoding:
    """The bytes are not valid in their encoding from ``offset`` (a byte offset in the file)."""

    encoding: TextEncoding
    offset: int
    reason: str


def decode(data: bytes, *, final: bool = True) -> DecodedText | InvalidEncoding:
    """Decode a whole file, or a head of one (``final`` false: a character may be cut short)."""
    encoding, bom = detect(data)
    decoder = codecs.getincrementaldecoder(str(encoding))(errors="strict")
    try:
        text = decoder.decode(data[bom:], final=final)
    except UnicodeDecodeError as exc:
        return InvalidEncoding(encoding, bom + exc.start, exc.reason)
    return DecodedText(text, encoding, bom)


def line_endings(text: str) -> LineEndings:
    """Which line breaks the text uses: one kind, several, or none."""
    crlf = text.count("\r\n")
    cr = text.count("\r") - crlf
    lf = text.count("\n") - crlf
    kinds = [kind for kind, count in ((LineEndings.LF, lf), (LineEndings.CRLF, crlf)) if count]
    if cr:
        kinds.append(LineEndings.CR)
    if not kinds:
        return LineEndings.NONE
    return kinds[0] if len(kinds) == 1 else LineEndings.MIXED


def unit_width(encoding: TextEncoding) -> int:
    return _UNIT[encoding]


def is_blank(text: str) -> bool:
    """Nothing but whitespace and ``#`` comment lines: no format declares anything in it."""
    return all(not line.strip() or line.lstrip().startswith("#") for line in text.splitlines())


def line_and_column(text: str, offset: int) -> tuple[int, int]:
    """The 1-based line and column of code point ``offset``; lines end at LF, CR LF or CR."""
    offset = max(0, min(offset, len(text)))
    before = text[:offset]
    line_start = max(before.rfind("\n"), before.rfind("\r")) + 1
    line = before.count("\n") + before.count("\r") - before.count("\r\n") + 1
    return line, offset - line_start + 1


def offset_of(text: str, line: int, column: int) -> int:
    """The code point at a 1-based ``line`` and ``column``, as parsers report positions."""
    position, current = 0, 1
    while current < line:
        newline = text.find("\n", position)
        if newline < 0:
            return len(text)
        position, current = newline + 1, current + 1
    return min(len(text), position + max(column, 1) - 1)
