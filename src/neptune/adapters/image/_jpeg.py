"""JPEG: the marker segments in order, metadata read, entropy-coded data skipped, never decoded.

Read: ``SOFn`` (the image: a table of precision, height, width and component count, and a table of
its components), ``APP0`` JFIF, ``APP1`` EXIF and XMP, ``APP2`` ICC profiles (their segments
joined in sequence order), ``APP14`` Adobe and ``COM`` comments. Each table cites the bytes of its
fields. Entropy-coded data is scanned only for the next marker, so the file must reach ``EOI``:
otherwise it is ``image.truncated``. Bytes after ``EOI`` (an MPF image, an appended video) are
noted, as are MPF and extended XMP, which no record holds.
"""

import re
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import neptune.adapters.image._blocks as _blocks
from neptune.adapters.image._context import Context
from neptune.adapters.image._emit import (
    ICC_UNREADABLE,
    MALFORMED,
    NOT_MODELLED,
    REPEATED,
    TRUNCATED,
    UNREADABLE,
    VALUE_UNREADABLE,
    CellInput,
)
from neptune.adapters.image._space import LimitHit, Space, Truncated, payload_step
from neptune.adapters.image._still import TAGS, Still
from neptune.model.knowledge import Known, Unknown
from neptune.model.provenance import Locator

if TYPE_CHECKING:
    from neptune.adapters.image._capture import Tags

SOI: Final = b"\xff\xd8"
_SOF: Final = frozenset({*range(0xC0, 0xC4), *range(0xC5, 0xC8), *range(0xC9, 0xCC), 0xCD, 0xCE,
                         0xCF})  # fmt: skip
_STANDALONE: Final = frozenset({0x01, *range(0xD0, 0xD9)})
EOI: Final = 0xD9
SOS: Final = 0xDA
EXIF_IDS: Final = (b"Exif\x00\x00", b"Exif\x00\xff")
XMP_ID: Final = b"http://ns.adobe.com/xap/1.0/\x00"
XMP_EXTENSION_ID: Final = b"http://ns.adobe.com/xmp/extension/\x00"
ICC_ID: Final = b"ICC_PROFILE\x00"
MPF_ID: Final = b"MPF\x00"
_MARKER: Final = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")
_SCAN_PIECE: Final = 1024 * 1024
_SOF_COLUMNS: Final = ("precision", "height", "width", "components")
_COMPONENT_COLUMNS: Final = ("id", "horizontal_sampling", "vertical_sampling", "quantization_table")
_JFIF_COLUMNS: Final = (
    "version_major", "version_minor", "units", "x_density", "y_density", "thumbnail_width",
    "thumbnail_height",
)  # fmt: skip
_ADOBE_COLUMNS: Final = ("version", "flags0", "flags1", "transform")


@dataclass(frozen=True)
class _IccPart:
    sequence: int
    count: int
    start: int
    end: int
    data_at: int
    data_length: int


class _Jpeg:
    def __init__(self, ctx: Context, space: Space) -> None:
        self.ctx = ctx
        self.space = space
        self.size: tuple[int, int] | None = None
        self.tags: Tags | None = None
        self.seen: set[str] = set()
        self.icc: list[_IccPart] = []

    def read(self) -> list[Still]:
        ctx, out, space = self.ctx, self.ctx.out, self.space
        position, ended, limited = 2, False, False
        while True:
            try:
                ctx.budget.structure()
            except LimitHit as hit:
                ctx.stopped(hit)
                limited = True
                break
            if not space.fits(position, 2):
                out.finding(
                    TRUNCATED,
                    space.cite(position, space.size - position),
                    "the file ends before the EOI marker",
                )
                break
            fill, marker = space.read(position, 2)
            if fill != 0xFF:
                out.finding(
                    MALFORMED,
                    space.cite(position, 2),
                    f"byte {position} should start a marker and is {fill:#04x}; reading stops",
                    {"offset": position},
                )
                break
            if marker == 0xFF:
                position += 1
                continue
            if marker == EOI:
                position, ended = position + 2, True
                break
            if marker in _STANDALONE:
                position += 2
                continue
            if not space.fits(position + 2, 2):
                out.finding(TRUNCATED, space.cite(position, space.size - position), "a cut marker")
                break
            (length,) = struct.unpack(">H", space.read(position + 2, 2))
            if length < 2 or not space.fits(position + 2, length):
                out.finding(
                    TRUNCATED if length >= 2 else MALFORMED,
                    space.cite(position, space.size - position),
                    f"marker {marker:#04x} at byte {position} declares {length} bytes;"
                    " the file ends first"
                    if length >= 2
                    else "a segment length below 2",
                    {"length": length, "offset": position},
                )
                break
            try:
                self._segment(marker, position, position + 4, length - 2)
            except LimitHit as hit:
                ctx.stopped(hit)
                limited = True
                break
            except Truncated as exc:
                where = f"marker {marker:#04x} at byte {position}"
                ctx.cut(exc, space.cite(position, length + 2), where)
            position += 2 + length
            if marker == SOS:
                found = self._scan(position)
                if found is None:
                    out.finding(
                        TRUNCATED,
                        space.cite(position, space.size - position),
                        "the entropy-coded data runs to the end of the file without an EOI marker",
                    )
                    break
                position = found
        if limited and self.size is None:
            self._frame_only(position)
        try:
            self._join_icc()
        except LimitHit as hit:
            ctx.stopped(hit)
        if ended and position < space.size:
            out.finding(
                NOT_MODELLED,
                space.cite(position, space.size - position),
                f"{space.size - position} bytes follow the EOI marker (another image, a video or"
                " a vendor trailer); they are not records",
                {"bytes": space.size - position},
            )
        return self._still()

    def _frame_only(self, position: int) -> None:
        """After a limit: the frame header alone, so the image is still recorded.

        Walks segment headers only (no table, no row) up to the first SOS, at most
        ``max_structures`` more of them, and takes the size of the first frame header.
        """
        space = self.space
        for _ in range(self.ctx.budget.max_structures):
            if not space.fits(position, 4):
                return
            fill, marker = space.read(position, 2)
            if fill != 0xFF or marker in (EOI, SOS):
                return
            if marker == 0xFF or marker in _STANDALONE:
                position += 1 if marker == 0xFF else 2
                continue
            (length,) = struct.unpack(">H", space.read(position + 2, 2))
            if length < 2 or not space.fits(position + 2, length):
                return
            if marker in _SOF:
                if length >= 8:
                    _, height, width = struct.unpack(">BHH", space.read(position + 4, 5))
                    self.size = (width, height)
                return
            position += 2 + length

    def _scan(self, position: int) -> int | None:
        """The offset of the first marker after entropy-coded data from ``position``."""
        space = self.space
        while position < space.size - 1:
            piece = space.read(position, min(_SCAN_PIECE + 1, space.size - position))
            match = _MARKER.search(piece)
            if match is not None:
                return position + match.start()
            position += len(piece) - 1
        return None

    def _segment(self, marker: int, start: int, data_at: int, length: int) -> None:
        if marker in _SOF:
            self._sof(marker, data_at, length)
            return
        if marker not in (0xE0, 0xE1, 0xE2, 0xEE, 0xFE):
            return
        out, space = self.ctx.out, self.space
        head = space.read(data_at, min(length, len(XMP_EXTENSION_ID)))
        segment = space.cite(start, length + 4)
        if marker == 0xE0 and head.startswith(b"JFIF\x00") and length >= 14:
            values = struct.unpack(">BBBHHBB", space.read(data_at + 5, 9))
            self.ctx.structure(space.cite(data_at + 5, 9), "JFIF", _JFIF_COLUMNS, list(values))
        elif marker == 0xE1 and head[:6] in EXIF_IDS:
            if self._once("Exif", segment):
                window = space.window(data_at + 6, length - 6)
                self.tags = _blocks.exif(self.ctx, window, "the APP1 EXIF segment")
        elif marker == 0xE1 and head.startswith(XMP_ID):
            if self._once("XMP", segment):
                window = space.window(data_at + len(XMP_ID), length - len(XMP_ID))
                _blocks.xmp(self.ctx, window, "the APP1 XMP segment")
        elif marker == 0xE1 and head.startswith(XMP_EXTENSION_ID):
            out.finding(NOT_MODELLED, segment, "extended XMP (a GUID-split packet) is not read")
        elif marker == 0xE2 and head.startswith(ICC_ID) and length >= 14:
            sequence, count = space.read(data_at + 12, 2)
            self.icc.append(
                _IccPart(sequence, count, start, start + length + 4, data_at + 14, length - 14)
            )
        elif marker == 0xE2 and head.startswith(MPF_ID):
            out.finding(NOT_MODELLED, segment, "an MPF index: its further images are not records")
        elif marker == 0xEE and head.startswith(b"Adobe") and length >= 12:
            values = struct.unpack(">HHHB", space.read(data_at + 5, 7))
            self.ctx.structure(space.cite(data_at + 5, 7), "Adobe", _ADOBE_COLUMNS, list(values))
        elif marker == 0xFE:
            self._comment(data_at, length)

    def _once(self, what: str, segment: tuple[Locator, ...]) -> bool:
        if what in self.seen:
            self.ctx.out.finding(REPEATED, segment, f"a second {what} segment; the first is read")
            return False
        self.seen.add(what)
        return True

    def _comment(self, data_at: int, length: int) -> None:
        locator = self.space.cite(data_at, length)
        cell: CellInput
        try:
            cell = self.space.read(data_at, length).decode("ascii")
        except UnicodeDecodeError:
            self.ctx.out.finding(VALUE_UNREADABLE, locator, "a COM comment is not ASCII")
            cell = Unknown()
        self.ctx.structure(locator, "COM", ("comment",), [cell])

    def _sof(self, marker: int, data_at: int, length: int) -> None:
        out, space = self.ctx.out, self.space
        name = f"SOF{marker - 0xC0}"
        locator = space.cite(data_at, length)
        if length < 6:
            out.finding(MALFORMED, locator, f"{name} holds {length} bytes, fewer than 6")
            return
        precision, height, width, count = struct.unpack(">BHHB", space.read(data_at, 6))
        if length != 6 + 3 * count:
            out.finding(
                MALFORMED,
                locator,
                f"{name} declares {count} components in {length} bytes",
                {"components": count, "length": length},
            )
            return
        if self.size is not None:
            out.finding(REPEATED, locator, f"a second frame header ({name}); the first is read")
            return
        fixed = space.cite(data_at, 6)
        self.ctx.structure(fixed, name, _SOF_COLUMNS, [precision, height, width, count])
        components = space.cite(data_at + 6, 3 * count)
        table = out.table(components, Known(f"{name} components"), _COMPONENT_COLUMNS, name)
        if table is not None:
            raw = space.read(data_at + 6, 3 * count)
            for index in range(count):
                self.ctx.budget.entry()
                ident, sampling, quantization = raw[index * 3 : index * 3 + 3]
                cells = [ident, sampling >> 4, sampling & 0x0F, quantization]
                out.row(table, components, index, cells)
        self.size = (width, height)

    def _join_icc(self) -> None:
        if not self.icc:
            return
        out, space = self.ctx.out, self.space
        first, last = min(p.start for p in self.icc), max(p.end for p in self.icc)
        carrier = space.cite(first, last - first)
        parts = sorted(self.icc, key=lambda part: part.sequence)
        count = parts[0].count
        sequences = [part.sequence for part in parts]
        total = sum(part.data_length for part in parts)
        if sequences != list(range(1, count + 1)) or any(p.count != count for p in parts):
            out.finding(
                ICC_UNREADABLE,
                carrier,
                f"the ICC_PROFILE segments are numbered {sequences} of {count}; not joined",
                {"count": count, "sequences": sequences},
            )
            return
        if total > self.ctx.max_metadata_bytes:
            out.finding(
                ICC_UNREADABLE,
                carrier,
                f"the ICC profile is {total} bytes, over max_metadata_bytes; it is not read",
                {"bytes": total},
            )
            return
        joined = b"".join(space.read(part.data_at, part.data_length) for part in parts)
        prefix = (*carrier, payload_step("join"))
        _blocks.icc(self.ctx, Space.payload(space.source, joined, prefix), "the APP2 segments")

    def _still(self) -> list[Still]:
        out, space = self.ctx.out, self.space
        if self.size is None:
            out.finding(UNREADABLE, space.whole(), "the file holds no readable frame header (SOFn)")
            return []
        width, height = self.size
        if width == 0 or height == 0:
            out.finding(
                UNREADABLE,
                space.whole(),
                f"the frame header declares {width} x {height} pixels (a DNL-defined height is"
                " not read)",
                {"height": height, "width": width},
            )
            return []
        return [Still(space.whole(), width, height, "jpeg", self.tags, TAGS)]


def read(ctx: Context, space: Space) -> list[Still]:
    """The JPEG that is all of ``space``."""
    return _Jpeg(ctx, space).read()
