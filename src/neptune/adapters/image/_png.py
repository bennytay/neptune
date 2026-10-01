"""PNG: the chunks in order, metadata chunks read, image data counted and never inflated.

Read: ``IHDR`` (the image), ``gAMA``, ``cHRM``, ``sRGB``, ``sBIT``, ``cICP``, ``pHYs``, ``tIME``
and ``acTL`` as one-row tables; ``iCCP`` as a table plus its inflated ICC profile; ``eXIf`` as
EXIF; ``tEXt``, ``zTXt`` and ``iTXt`` as one-row tables of their keyword and text (Latin-1, or
UTF-8 for ``iTXt``, as the specification states), an ``iTXt`` keyed ``XML:com.adobe.xmp`` also as
XMP. Each table cites the chunk's data. The CRC of every chunk read is checked; ``IDAT`` is only
counted, so a declared raster no deflate stream of that size can hold (deflate never exceeds
1032:1) is ``image.raster_truncated``. An animated PNG's frames are noted, not modelled.
"""

import struct
import zlib
from typing import TYPE_CHECKING, Final

import neptune.adapters.image._blocks as _blocks
from neptune.adapters.image._context import Context
from neptune.adapters.image._emit import (
    CRC_MISMATCH,
    MALFORMED,
    NOT_MODELLED,
    RASTER_TRUNCATED,
    REPEATED,
    TRUNCATED,
    UNREADABLE,
    VALUE_NOT_COPIED,
    VALUE_UNREADABLE,
    CellInput,
)
from neptune.adapters.image._space import LimitHit, Space, Truncated, payload_step
from neptune.adapters.image._still import TAGS, Still
from neptune.model.knowledge import Unknown
from neptune.model.provenance import Locator

if TYPE_CHECKING:
    from neptune.adapters.image._capture import Tags

SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
DEFLATE_RATIO: Final = 1032
_MAX_LENGTH: Final = 2**31 - 1
_CHANNELS: Final = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_STRUCTS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "IHDR": (
        ">IIBBBBB",
        ("width", "height", "bit_depth", "colour_type", "compression_method", "filter_method",
         "interlace_method"),
    ),
    "gAMA": (">I", ("gamma",)),
    "cHRM": (
        ">8I",
        ("white_x", "white_y", "red_x", "red_y", "green_x", "green_y", "blue_x", "blue_y"),
    ),
    "sRGB": (">B", ("rendering_intent",)),
    "cICP": (
        ">4B",
        ("colour_primaries", "transfer_function", "matrix_coefficients", "video_full_range"),
    ),
    "pHYs": (">IIB", ("pixels_per_unit_x", "pixels_per_unit_y", "unit")),
    "tIME": (">HBBBBB", ("year", "month", "day", "hour", "minute", "second")),
    "acTL": (">II", ("num_frames", "num_plays")),
}  # fmt: skip
_TEXT_CHUNKS: Final = frozenset({"tEXt", "zTXt", "iTXt", "iCCP", "eXIf", "sBIT"})
_ONCE: Final = frozenset({"IHDR", "eXIf", "iCCP", "acTL"})
XMP_KEYWORD: Final = b"XML:com.adobe.xmp"


class _Png:
    def __init__(self, ctx: Context, space: Space) -> None:
        self.ctx = ctx
        self.space = space
        self.ihdr: tuple[int, ...] | None = None
        self.tags: Tags | None = None
        self.idat = 0
        self.limited = False  # a limit stopped the walk: IDAT was not all counted
        self.seen: set[str] = set()

    def read(self) -> list[Still]:
        ctx, out, space = self.ctx, self.ctx.out, self.space
        position, ended, damaged = len(SIGNATURE), False, False
        while position < space.size:
            try:
                ctx.budget.structure()
            except LimitHit as hit:
                ctx.stopped(hit)
                damaged = self.limited = True
                break
            if not space.fits(position, 12):
                out.finding(
                    TRUNCATED,
                    space.cite(position, space.size - position),
                    f"the file ends inside a chunk header at byte {position}",
                )
                damaged = True
                break
            length, kind = struct.unpack(">I4s", space.read(position, 8))
            if length > _MAX_LENGTH or not kind.isalpha():
                out.finding(
                    MALFORMED,
                    space.cite(position, 8),
                    f"the chunk header at byte {position} is not a PNG chunk",
                    {"length": length, "offset": position},
                )
                damaged = True
                break
            name = kind.decode("ascii")
            if not space.fits(position + 8, length + 4):
                out.finding(
                    TRUNCATED,
                    space.cite(position, space.size - position),
                    f"chunk {name} at byte {position} declares {length} bytes; the file ends first",
                    {"length": length, "offset": position},
                )
                damaged = True
                if name == "IDAT":
                    self.idat += space.size - position - 8
                break
            if position == len(SIGNATURE) and name != "IHDR":
                out.finding(UNREADABLE, space.cite(position, 8), "the first chunk is not IHDR")
                return []
            try:
                self._chunk(name, position, length)
            except LimitHit as hit:
                ctx.stopped(hit)
                damaged = self.limited = True
                break
            except Truncated as exc:
                ctx.cut(exc, space.cite(position, length + 12), f"chunk {name} at byte {position}")
            position += length + 12
            if name == "IEND":
                ended = True
                break
        if not ended and not damaged:
            out.finding(TRUNCATED, space.whole(), "the file ends without an IEND chunk")
        if ended and position < space.size:
            out.finding(
                NOT_MODELLED,
                space.cite(position, space.size - position),
                f"{space.size - position} bytes follow the IEND chunk",
            )
        return self._still()

    def _chunk(self, name: str, position: int, length: int) -> None:
        out, space = self.ctx.out, self.space
        data_at = position + 8
        if name in ("IDAT", "fdAT"):
            self.idat += length if name == "IDAT" else 0
            if name == "fdAT":
                self.seen.add(name)
            return
        if name not in _STRUCTS and name not in _TEXT_CHUNKS:
            return
        if name in self.seen and name in _ONCE:
            out.finding(
                REPEATED,
                space.cite(position, length + 12),
                f"a second {name} chunk; the first is read",
            )
            return
        self.seen.add(name)
        if name in _STRUCTS:
            wanted = struct.calcsize(_STRUCTS[name][0])
            if length != wanted:
                message = f"chunk {name} holds {length} bytes, not {wanted}"
                out.finding(MALFORMED, space.cite(data_at, length), message)
                return
        elif length > self.ctx.max_metadata_bytes:
            out.finding(
                VALUE_NOT_COPIED,
                space.cite(data_at, length),
                f"chunk {name} holds {length} bytes, over max_metadata_bytes; it is not read",
                {"bytes": length, "max_metadata_bytes": self.ctx.max_metadata_bytes},
            )
            return
        data = space.read(data_at, length)
        (crc,) = struct.unpack(">I", space.read(data_at + length, 4))
        if zlib.crc32(name.encode("ascii") + data) != crc:
            out.finding(
                CRC_MISMATCH,
                space.cite(position, length + 12),
                f"the CRC of chunk {name} at byte {position} does not match its bytes",
            )
        locator = space.cite(data_at, length)
        if name in _STRUCTS:
            layout, columns = _STRUCTS[name]
            values = struct.unpack(layout, data)
            self.ctx.structure(locator, name, columns, list(values))
            if name == "IHDR":
                self.ihdr = values
            if name == "acTL":
                out.finding(
                    NOT_MODELLED,
                    locator,
                    f"an animated PNG of {values[0]} frames: only the default image is a record",
                    {"frames": values[0]},
                )
            return
        if name == "sBIT":
            self.ctx.structure(locator, name, ("significant_bits",), list(data))
        elif name == "eXIf":
            self.tags = _blocks.exif(self.ctx, space.window(data_at, length), "the eXIf chunk")
        elif name == "iCCP":
            self._iccp(data, data_at)
        else:
            self._text(name, data, data_at)

    def _keyword(self, name: str, data: bytes, data_at: int) -> tuple[str, int] | None:
        end = data.find(b"\x00")
        if not 1 <= end <= 79:
            self.ctx.out.finding(
                MALFORMED,
                self.space.cite(data_at, len(data)),
                f"chunk {name} has no keyword of 1 to 79 bytes ending in NUL",
            )
            return None
        return data[:end].decode("latin-1"), end + 1

    def _inflated(self, name: str, compressed_at: int, compressed: bytes) -> Space | None:
        prefix = (*self.space.cite(compressed_at, len(compressed)), payload_step("inflate"))
        try:
            inflated = self.ctx.inflate(compressed)
        except LimitHit:
            self.ctx.out.finding(
                MALFORMED,
                self.space.cite(compressed_at, len(compressed)),
                f"chunk {name} inflates past max_metadata_bytes ({self.ctx.max_metadata_bytes});"
                " it is not read",
                {"max_metadata_bytes": self.ctx.max_metadata_bytes},
            )
            return None
        except zlib.error:
            self.ctx.out.finding(
                MALFORMED,
                self.space.cite(compressed_at, len(compressed)),
                f"the compressed data of chunk {name} is not a complete zlib stream",
            )
            return None
        return Space.payload(self.space.source, inflated, prefix)

    def _method(self, name: str, method: int, locator: tuple[Locator, ...]) -> None:
        self.ctx.out.finding(
            MALFORMED,
            locator,
            f"chunk {name} declares compression method {method}; only 0 (zlib) is defined",
            {"method": method},
        )

    def _text_cell(self, text: Space, locator: tuple[Locator, ...], codec: str) -> CellInput:
        """``text`` as a cell: ``Unknown`` with a finding if it is not ``codec`` or too long."""
        out = self.ctx.out
        if text.size > self.ctx.max_value_bytes:
            out.finding(
                VALUE_NOT_COPIED,
                locator,
                f"a text of {text.size} bytes is over max_value_bytes; it stays cited in the bytes",
                {"bytes": text.size, "max_value_bytes": self.ctx.max_value_bytes},
            )
            return Unknown()
        try:
            return text.read(0, text.size).decode(codec)
        except UnicodeDecodeError:
            out.finding(VALUE_UNREADABLE, locator, f"the text is not {codec}")
            return Unknown()

    def _iccp(self, data: bytes, data_at: int) -> None:
        keyword = self._keyword("iCCP", data, data_at)
        if keyword is None:
            return
        name, at = keyword
        locator = self.space.cite(data_at, len(data))
        if at >= len(data):
            self.ctx.out.finding(MALFORMED, locator, "chunk iCCP has no compression method")
            return
        self.ctx.structure(
            locator, "iCCP", ("profile_name", "compression_method"), [name, data[at]]
        )
        if data[at] != 0:
            self._method("iCCP", data[at], locator)
            return
        profile = self._inflated("iCCP", data_at + at + 1, data[at + 1 :])
        if profile is not None:
            _blocks.icc(self.ctx, profile, "the iCCP chunk")

    def _text(self, name: str, data: bytes, data_at: int) -> None:
        keyword = self._keyword(name, data, data_at)
        if keyword is None:
            return
        key, at = keyword
        locator = self.space.cite(data_at, len(data))
        if name == "tEXt":
            plain = self.space.window(data_at + at, len(data) - at)
            cell = self._text_cell(plain, locator, "latin-1")
            self.ctx.structure(locator, name, ("keyword", "text"), [key, cell])
            return
        if name == "zTXt":
            if at >= len(data):
                self.ctx.out.finding(MALFORMED, locator, "chunk zTXt has no compression method")
                return
            if data[at] != 0:
                self._method(name, data[at], locator)
                return
            text = self._inflated(name, data_at + at + 1, data[at + 1 :])
            if text is not None:
                self.ctx.structure(
                    locator,
                    name,
                    ("keyword", "text"),
                    [key, self._text_cell(text, locator, "latin-1")],
                )
            return
        self._itxt(key, data, data_at, at, locator)

    def _itxt(
        self, key: str, data: bytes, data_at: int, at: int, locator: tuple[Locator, ...]
    ) -> None:
        out = self.ctx.out
        cut = data.find(b"\x00", at + 2)
        cut2 = data.find(b"\x00", cut + 1) if cut >= 0 else -1
        if at + 2 > len(data) or cut < 0 or cut2 < 0:
            out.finding(MALFORMED, locator, "chunk iTXt is missing its language or translation")
            return
        compressed = data[at]
        language = data[at + 2 : cut].decode("latin-1")
        text_at = cut2 + 1
        body: Space | None
        if compressed and data[at + 1] != 0:
            self._method("iTXt", data[at + 1], locator)
            body = None
        elif compressed:
            body = self._inflated("iTXt", data_at + text_at, data[text_at:])
        else:
            body = self.space.window(data_at + text_at, len(data) - text_at)
        cells: list[CellInput] = [key, language]
        try:
            cells.append(data[cut + 1 : cut2].decode("utf-8"))
        except UnicodeDecodeError:
            out.finding(VALUE_UNREADABLE, locator, "the iTXt translated keyword is not UTF-8")
            cells.append(Unknown())
        if body is None:
            cells.append(Unknown())
        else:
            cells.append(self._text_cell(body, locator, "utf-8"))
        columns = ("keyword", "language", "translated_keyword", "text")
        self.ctx.structure(locator, "iTXt", columns, cells)
        if body is not None and key.encode("latin-1") == XMP_KEYWORD:
            _blocks.xmp(self.ctx, body, "the iTXt chunk")

    def _still(self) -> list[Still]:
        out, space = self.ctx.out, self.space
        if self.ihdr is None:
            out.finding(UNREADABLE, space.whole(), "the file holds no readable IHDR chunk")
            return []
        width, height, depth, colour, *_ = self.ihdr
        if not (1 <= width <= _MAX_LENGTH and 1 <= height <= _MAX_LENGTH):
            out.finding(
                UNREADABLE,
                space.whole(),
                f"IHDR declares {width} x {height} pixels; PNG requires 1 to 2^31 - 1",
                {"height": height, "width": width},
            )
            return []
        channels = _CHANNELS.get(colour)
        if self.limited:
            pass  # IDAT after the limit was not counted: the raster's size is not judged
        elif channels is None:
            out.finding(MALFORMED, space.whole(), f"IHDR declares colour type {colour}")
        else:
            raw = height * (1 + (width * channels * depth + 7) // 8)
            if raw > DEFLATE_RATIO * self.idat:
                out.finding(
                    RASTER_TRUNCATED,
                    space.whole(),
                    f"the declared raster is {raw} bytes and IDAT holds {self.idat}: no deflate"
                    f" stream that short can hold it",
                    {"idat_bytes": self.idat, "raster_bytes": raw},
                )
        if "fdAT" in self.seen and "acTL" not in self.seen:
            out.finding(NOT_MODELLED, space.whole(), "fdAT chunks without acTL are not read")
        return [Still(space.whole(), width, height, "png", self.tags, TAGS)]


def read(ctx: Context, space: Space) -> list[Still]:
    """The PNG that is all of ``space``."""
    return _Png(ctx, space).read()
