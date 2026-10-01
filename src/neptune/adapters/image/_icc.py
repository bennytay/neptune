"""ICC profiles: the colour profile an image embeds, read from its header and tag table.

A profile becomes two ``StructuredTable``s: ``ICC header`` (one row: the header's fields in the
specification's order, columns named as ICC.1 names them) citing the first 128 bytes, and
``ICC tags`` (one row per tag: ``signature``, ``offset``, ``size``) citing the tag count and
table. Signatures are their four ASCII characters verbatim (``RGB ``, ``mntr``); the version is
its major, minor and bug-fix digits; the illuminant its three s15Fixed16 numbers, read exactly;
the profile ID its 16 bytes in hex. A zero CMM, platform or profile ID is ``KnownAbsent``: ICC.1
defines zero there as "none", cited through the ``acsp`` signature that makes the bytes a profile.
Tag data is not decoded: it stays in the cited bytes.
"""

import struct
from typing import Final

from neptune.adapters.image._context import Context
from neptune.adapters.image._emit import ICC_UNREADABLE, VALUE_UNREADABLE, CellInput
from neptune.adapters.image._space import LimitHit, Space
from neptune.model.knowledge import AssertionKind, Known, KnownAbsent, Unknown
from neptune.model.provenance import Provenance

HEADER_COLUMNS: Final = (
    "size", "cmm", "version_major", "version_minor", "version_bugfix", "class", "colour_space",
    "pcs", "created_year", "created_month", "created_day", "created_hour", "created_minute",
    "created_second", "signature", "platform", "flags", "manufacturer", "model", "attributes",
    "intent", "illuminant_x", "illuminant_y", "illuminant_z", "creator", "profile_id",
)  # fmt: skip
TAG_COLUMNS: Final = ("signature", "offset", "size")
_HEADER: Final = 128


def _signature(
    ctx: Context, space: Space, at: int, absent: Provenance | None, what: str
) -> CellInput:
    raw = space.read(at, 4)
    if raw == b"\x00\x00\x00\x00":
        return KnownAbsent(absent) if absent is not None else Unknown()
    if all(0x20 <= byte < 0x7F for byte in raw):
        return Known(raw.decode("ascii"))
    ctx.out.finding(
        VALUE_UNREADABLE,
        space.cite(at, 4),
        f"a signature in the ICC profile in {what} is not four ASCII characters",
        {"offset": at},
    )
    return Unknown()


def read(ctx: Context, space: Space, what: str) -> None:
    """The ICC profile that is all of ``space``: its header and tag table, as far as they hold."""
    out = ctx.out
    if not out.first("ICC profile", space.whole()):
        return
    if space.size < _HEADER + 4 or space.read(36, 4) != b"acsp":
        out.finding(
            ICC_UNREADABLE,
            space.whole(),
            f"the ICC profile in {what} has no 128-byte header with the 'acsp' signature",
            {"size": space.size},
        )
        return
    head = space.read(0, _HEADER)
    spec = out.provenance(space.cite(36, 4), AssertionKind.STATED)
    header = space.cite(0, _HEADER)
    table = out.table(
        header,
        Known("ICC header"),
        HEADER_COLUMNS,
        f"the ICC header in {what}",
        AssertionKind.STATED,
    )
    if table is None:
        return
    (size,) = struct.unpack(">I", head[0:4])
    if size != space.size:
        out.finding(
            ICC_UNREADABLE,
            header,
            f"the ICC profile in {what} declares {size} bytes and holds {space.size}",
            {"declared": size, "held": space.size},
        )
    created = struct.unpack(">6H", head[24:36])
    (flags,) = struct.unpack(">I", head[44:48])
    (attributes,) = struct.unpack(">Q", head[56:64])
    (intent,) = struct.unpack(">I", head[64:68])
    illuminant = struct.unpack(">3i", head[68:80])
    profile_id = head[84:100]
    cells: list[CellInput] = [
        size,
        _signature(ctx, space, 4, spec, what),
        head[8],
        head[9] >> 4,
        head[9] & 0x0F,
        _signature(ctx, space, 12, None, what),
        _signature(ctx, space, 16, None, what),
        _signature(ctx, space, 20, None, what),
        *created,
        "acsp",
        _signature(ctx, space, 40, spec, what),
        flags,
        _signature(ctx, space, 48, None, what),
        _signature(ctx, space, 52, None, what),
        attributes,
        intent,
        *(value / 65536 for value in illuminant),
        _signature(ctx, space, 80, None, what),
        KnownAbsent(spec) if not any(profile_id) else profile_id.hex(),
    ]
    out.row(table, header, 0, cells)
    _tags(ctx, space, what)


def _tags(ctx: Context, space: Space, what: str) -> None:
    out = ctx.out
    (declared,) = struct.unpack(">I", space.read(_HEADER, 4))
    room = (space.size - _HEADER - 4) // 12
    held = min(declared, room)
    locator = space.cite(_HEADER, 4 + held * 12)
    table = out.table(
        locator,
        Known("ICC tags"),
        TAG_COLUMNS,
        f"the ICC tag table in {what}",
        AssertionKind.STATED,
    )
    if table is None:
        return
    if held < declared:
        out.finding(
            ICC_UNREADABLE,
            locator,
            f"the ICC tag table in {what} declares {declared} tags and holds {held}",
            {"declared": declared, "held": held},
        )
    raw = space.read(_HEADER + 4, held * 12)
    outside: list[int] = []
    try:
        for index in range(held):
            ctx.budget.entry()
            at = _HEADER + 4 + index * 12
            offset, size = struct.unpack(">II", raw[index * 12 + 4 : index * 12 + 12])
            if not space.fits(offset, size):
                outside.append(index)
            cells = [_signature(ctx, space, at, None, what), offset, size]
            out.row(table, locator, index, cells)
    except LimitHit as hit:
        ctx.stopped(hit)
    if outside:
        out.finding(
            ICC_UNREADABLE,
            locator,
            f"{len(outside)} tags in the ICC tag table in {what} point outside the profile",
            {"rows": outside[:16], "tags": len(outside)},
        )
