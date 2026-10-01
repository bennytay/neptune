"""WebP: the RIFF chunks in order; the bitstream headers read, the bitstreams never decoded.

Read: ``VP8X`` (flags and canvas), the ``VP8`` key-frame header and the ``VP8L`` header as one-row
tables of their fields (sizes as the format defines them: the stored minus-one values plus one),
``ICCP`` as an ICC profile, ``EXIF`` as EXIF (after an ``Exif\\0\\0`` prefix some writers add) and
``XMP `` as XMP. The image's size is the ``VP8X`` canvas when there is one, else the bitstream's.
Animation frames (``ANMF``) are noted, not modelled.
"""

import struct
from typing import TYPE_CHECKING, Final

import neptune.adapters.image._blocks as _blocks
from neptune.adapters.image._context import Context
from neptune.adapters.image._emit import (
    MALFORMED,
    NOT_MODELLED,
    REPEATED,
    TRUNCATED,
    UNREADABLE,
)
from neptune.adapters.image._space import LimitHit, Space, Truncated
from neptune.adapters.image._still import TAGS, Still

if TYPE_CHECKING:
    from neptune.adapters.image._capture import Tags

_VP8X: Final = ("flags", "canvas_width", "canvas_height")
_VP8: Final = (
    "key_frame", "version", "show_frame", "first_partition_size", "width", "horizontal_scale",
    "height", "vertical_scale",
)  # fmt: skip
_VP8L: Final = ("width", "height", "alpha_is_used", "version")
_ANIM: Final = ("background_color", "loop_count")


class _WebP:
    def __init__(self, ctx: Context, space: Space) -> None:
        self.ctx = ctx
        self.space = space
        self.canvas: tuple[int, int] | None = None
        self.frame: tuple[int, int] | None = None
        self.tags: Tags | None = None
        self.seen: set[str] = set()
        self.frames = 0

    def read(self) -> list[Still]:
        ctx, out, space = self.ctx, self.ctx.out, self.space
        if space.size < 12:
            out.finding(UNREADABLE, space.whole(), "the file is shorter than a RIFF header")
            return []
        (declared,) = struct.unpack("<I", space.read(4, 4))
        end = 8 + declared
        if end > space.size:
            out.finding(
                TRUNCATED,
                space.whole(),
                f"the RIFF header declares {end} bytes and the file holds {space.size}",
                {"declared": end, "size": space.size},
            )
            end = space.size
        elif end < space.size:
            out.finding(
                NOT_MODELLED,
                space.cite(end, space.size - end),
                f"{space.size - end} bytes follow the RIFF chunk",
            )
        position = 12
        while position + 8 <= end:
            try:
                ctx.budget.structure()
            except LimitHit as hit:
                ctx.stopped(hit)
                break
            fourcc, length = struct.unpack("<4sI", space.read(position, 8))
            name = fourcc.decode("latin-1")
            if position + 8 + length > end:
                out.finding(
                    TRUNCATED,
                    space.cite(position, end - position),
                    f"chunk {name!r} at byte {position} declares {length} bytes; the RIFF"
                    " ends first",
                    {"length": length, "offset": position},
                )
                break
            try:
                self._chunk(name, position + 8, length)
            except LimitHit as hit:
                ctx.stopped(hit)
                break
            except Truncated as exc:
                ctx.cut(exc, space.cite(position, length + 8), f"chunk {name!r} at byte {position}")
            position += 8 + length + (length & 1)
        if self.frames:
            out.finding(
                NOT_MODELLED,
                space.whole(),
                f"an animated WebP of {self.frames} frames: only the canvas is a record",
                {"frames": self.frames},
            )
        size = self.canvas or self.frame
        if size is None:
            out.finding(UNREADABLE, space.whole(), "no VP8X, VP8 or VP8L chunk declares a size")
            return []
        return [Still(space.whole(), size[0], size[1], "webp", self.tags, TAGS)]

    def _chunk(self, name: str, at: int, length: int) -> None:
        out, space = self.ctx.out, self.space
        locator = space.cite(at, length)
        if name in ("VP8X", "VP8 ", "VP8L", "ICCP", "EXIF", "XMP ") and name in self.seen:
            out.finding(REPEATED, locator, f"a second {name.strip()} chunk; the first is read")
            return
        self.seen.add(name)
        if name == "ANMF":
            self.frames += 1
        elif name == "VP8X" and length >= 10:
            data = space.read(at, 10)
            width = 1 + int.from_bytes(data[4:7], "little")
            height = 1 + int.from_bytes(data[7:10], "little")
            self.ctx.structure(space.cite(at, 10), "VP8X", _VP8X, [data[0], width, height])
            self.canvas = (width, height)
        elif name == "VP8 " and length >= 10:
            self._vp8(at)
        elif name == "VP8L" and length >= 5:
            data = space.read(at, 5)
            if data[0] != 0x2F:
                out.finding(MALFORMED, locator, "the VP8L chunk does not start with 0x2f")
                return
            (bits,) = struct.unpack("<I", data[1:5])
            values = [
                (bits & 0x3FFF) + 1,
                ((bits >> 14) & 0x3FFF) + 1,
                (bits >> 28) & 1,
                bits >> 29,
            ]
            self.ctx.structure(space.cite(at, 5), "VP8L", _VP8L, values)
            self.frame = (values[0], values[1])
        elif name == "ICCP":
            _blocks.icc(self.ctx, space.window(at, length), "the ICCP chunk")
        elif name == "EXIF":
            skip = 6 if space.read(at, min(6, length)) == b"Exif\x00\x00" else 0
            window = space.window(at + skip, length - skip)
            self.tags = _blocks.exif(self.ctx, window, "the EXIF chunk")
        elif name == "XMP ":
            _blocks.xmp(self.ctx, space.window(at, length), "the XMP chunk")
        elif name == "ANIM" and length >= 6:
            values = list(struct.unpack("<IH", space.read(at, 6)))
            self.ctx.structure(space.cite(at, 6), "ANIM", _ANIM, values)
        elif name in ("VP8X", "VP8 ", "VP8L", "ANIM"):
            out.finding(MALFORMED, locator, f"chunk {name.strip()} holds only {length} bytes")

    def _vp8(self, at: int) -> None:
        space = self.space
        data = space.read(at, 10)
        bits = data[0] | data[1] << 8 | data[2] << 16
        if data[3:6] != b"\x9d\x01\x2a" or bits & 1:
            self.ctx.out.finding(
                MALFORMED,
                space.cite(at, 10),
                "the VP8 chunk does not start with a key frame and its start code",
            )
            return
        width_bits, height_bits = struct.unpack("<HH", data[6:10])
        width, height = width_bits & 0x3FFF, height_bits & 0x3FFF
        values = [
            1,
            (bits >> 1) & 7,
            (bits >> 4) & 1,
            bits >> 5,
            width,
            width_bits >> 14,
            height,
            height_bits >> 14,
        ]
        self.ctx.structure(space.cite(at, 10), "VP8", _VP8, values)
        if width and height:
            self.frame = (width, height)


def read(ctx: Context, space: Space) -> list[Still]:
    """The WebP file that is all of ``space``."""
    return _WebP(ctx, space).read()
