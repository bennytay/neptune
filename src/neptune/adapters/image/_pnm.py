"""Netpbm: PBM, PGM, PPM (plain ``P1`` to ``P3`` and binary ``P4`` to ``P6``) and PAM (``P7``).

The header is one table of one row: ``magic`` and its decimal fields as Netpbm names them
(``width``, ``height``, ``maxval``; PAM adds ``depth`` and ``tupltype``). It cites the header's
bytes, comments included. A binary raster the file is too short for is
``image.raster_truncated``; bytes after it (Netpbm allows images back to back) are noted. Netpbm
has no place for capture metadata or orientation: both are ``NotCovered``.
"""

from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import PROBE_HEAD_SIZE
from neptune.adapters.image._context import Context
from neptune.adapters.image._emit import NOT_MODELLED, RASTER_TRUNCATED, UNREADABLE
from neptune.adapters.image._space import Space
from neptune.adapters.image._still import NOT_COVERED, Still

ENCODINGS: Final = {
    b"P1": "pbm", b"P2": "pgm", b"P3": "ppm", b"P4": "pbm", b"P5": "pgm", b"P6": "ppm",
    b"P7": "pam",
}  # fmt: skip
PLAIN: Final = frozenset({b"P1", b"P2", b"P3"})
HEADER_LIMIT: Final = PROBE_HEAD_SIZE  # what probe and detect see: one limit for all three
MAX_DIGITS: Final = (
    10  # a width, height, depth or maxval: far past anything valid, so int() is cheap
)
_SPACE: Final = b" \t\n\r\x0b\x0c"
_CHANNELS: Final = {b"P4": 1, b"P5": 1, b"P6": 3}


@dataclass(frozen=True)
class Header:
    magic: bytes
    columns: tuple[str, ...]
    values: tuple[int | str, ...]
    end: int  # the raster's first byte

    def field(self, name: str) -> int | str | None:
        return dict(zip(self.columns, self.values, strict=True)).get(name)


def parse_header(data: bytes) -> Header | None:
    """The header at the start of ``data``, if it is a complete, well-formed Netpbm header."""
    magic = data[:2]
    if magic not in ENCODINGS:
        return None
    if magic == b"P7":
        return _pam(data)
    names = ("width", "height") if magic in (b"P1", b"P4") else ("width", "height", "maxval")
    position, values = 2, []
    if not data[2:3].isspace() and data[2:3] != b"#":
        return None
    for _ in names:
        found = _token(data, position)
        if found is None:
            return None
        value, position = found
        values.append(value)
    if not data[position : position + 1].isspace():
        return None
    header = Header(magic, ("magic", *names), (magic.decode("ascii"), *values), position + 1)
    return header if _valid(header) else None


def _token(data: bytes, position: int) -> tuple[int, int] | None:
    """The decimal number after any whitespace and ``#`` comments from ``position``, and its end.

    One pass, no backtracking: a hostile header costs time linear in its length.
    """
    size = len(data)
    while position < size:
        byte = data[position]
        if byte in _SPACE:
            position += 1
        elif byte == 0x23:  # '#': a comment runs to the end of its line
            while position < size and data[position] not in b"\r\n":
                position += 1
        else:
            break
    end = position
    while end < size and 0x30 <= data[end] <= 0x39:
        end += 1
    digits = data[position:end].lstrip(b"0")  # zero padding is free: only the value is bounded
    if end == position or len(digits) > MAX_DIGITS:
        return None
    return int(digits or b"0"), end


def _pam(data: bytes) -> Header | None:
    if data[2:3] != b"\n":
        return None
    position, fields, tupltype = 3, {}, []
    while True:
        while data[position : position + 1] == b"#":
            newline = data.find(b"\n", position)
            if newline < 0:
                return None
            position = newline + 1
        newline = data.find(b"\n", position)
        if newline < 0:
            return None
        line = data[position:newline]
        position = newline + 1
        if any(byte in line for byte in b"\r\x0b\x0c"):
            return None  # PAM lines end at LF and separate with spaces or tabs, nothing else
        parts = line.split(None, 1)
        if not parts or not (parts[0].isalpha() and parts[0].isupper()):
            return None
        key, value = parts[0], parts[1].strip() if len(parts) > 1 else b""
        if key == b"ENDHDR":
            break
        if key == b"TUPLTYPE":
            tupltype.append(value.decode("ascii", errors="replace"))
        elif (
            key in (b"WIDTH", b"HEIGHT", b"DEPTH", b"MAXVAL")
            and value.isdigit()
            and len(value.lstrip(b"0")) <= MAX_DIGITS
        ):
            fields[key.decode("ascii").lower()] = int(value)
        else:
            return None
    if set(fields) != {"width", "height", "depth", "maxval"}:
        return None
    columns = ("magic", "width", "height", "depth", "maxval", "tupltype")
    values = ("P7", fields["width"], fields["height"], fields["depth"], fields["maxval"],
              " ".join(tupltype))  # fmt: skip
    header = Header(b"P7", columns, values, position)
    return header if _valid(header) else None


def _valid(header: Header) -> bool:
    width, height = header.field("width"), header.field("height")
    maxval, depth = header.field("maxval"), header.field("depth")
    if not (isinstance(width, int) and isinstance(height, int) and width >= 1 and height >= 1):
        return False
    if maxval is not None and not (isinstance(maxval, int) and 1 <= maxval <= 65535):
        return False
    return depth is None or (isinstance(depth, int) and depth >= 1)


def read(ctx: Context, space: Space) -> list[Still]:
    """The Netpbm image at the start of ``space``."""
    out = ctx.out
    header = parse_header(space.read(0, min(space.size, HEADER_LIMIT)))
    if header is None:
        out.finding(UNREADABLE, space.whole(), "the file does not start with a Netpbm header")
        return []
    ctx.structure(space.cite(0, header.end), "PNM header", header.columns, list(header.values))
    width, height = header.field("width"), header.field("height")
    assert isinstance(width, int) and isinstance(height, int)
    if header.magic not in PLAIN:
        maxval = header.field("maxval")
        sample = 2 if isinstance(maxval, int) and maxval > 255 else 1
        if header.magic == b"P4":
            needed = (width + 7) // 8 * height
        else:
            depth = header.field("depth")
            channels = depth if isinstance(depth, int) else _CHANNELS[header.magic]
            needed = width * height * channels * sample
        end = header.end + needed
        if end > space.size:
            out.finding(
                RASTER_TRUNCATED,
                space.whole(),
                f"the declared raster ends at byte {end}; the file holds {space.size}",
                {"needed": end, "size": space.size},
            )
        elif end < space.size:
            out.finding(
                NOT_MODELLED,
                space.cite(end, space.size - end),
                f"{space.size - end} bytes follow the raster (another image?); not read",
            )
    encoding = ENCODINGS[header.magic]
    return [Still(space.whole(), width, height, encoding, None, NOT_COVERED)]
