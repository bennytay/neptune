"""TIFF structure: a TIFF, BigTIFF or DNG file, or the TIFF stream an EXIF block carries.

Each IFD becomes one ``StructuredTable`` citing its exact bytes (the entry count, the entries and
the next-IFD offset), named for the pointer that declares it (``IFD0``, ``IFD1``, …, ``Exif``,
``GPS``, ``Interop``, ``SubIFD``; the name cites that pointer). Each entry is one
``StructuredRecord`` cited ``[IFD, Row(i)]`` in stored order, with cells ``tag``, ``type`` and
``count`` (numbers as stored) and then the values as the type declares them:

- integers (BYTE, SHORT, LONG, LONG8, their signed forms, IFD, UNDEFINED) one cell each;
- RATIONAL and SRATIONAL two cells each, numerator then denominator, never divided;
- FLOAT and DOUBLE one real each;
- ASCII (and EXIF 3.0 UTF-8) one text cell per NUL-terminated string, trailing NULs dropped;
  blank text is ``Unknown``, and bytes that are not ASCII (or UTF-8) are ``Unknown`` with
  ``image.value_unreadable``.

A value is copied only up to ``max_value_bytes``; past it, a MakerNote (always: it is opaque and
vendor-defined) and a value of an unknown type stay in the bytes, and ``image.value_not_copied``
cites them. XMP (tag 700) and ICC profiles (tag 34675) are read into their own tables instead.
Every offset and size is checked against the stream before anything is read, every IFD is read
once (``image.ifd_loop`` for a second pointer), and every IFD and entry spends the budget.
"""

import struct
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.image._context import Context
from neptune.adapters.image._emit import (
    BAD_OFFSET,
    IFD_LOOP,
    MALFORMED,
    REPEATED,
    TRUNCATED,
    VALUE_NOT_COPIED,
    VALUE_UNREADABLE,
    CellInput,
)
from neptune.adapters.image._space import LimitHit, Space
from neptune.model.ids import RecordId
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.provenance import Locator, Row

ASCII: Final = 2
RATIONAL: Final = 5
SRATIONAL: Final = 10
UTF8: Final = 129
_SIZES: Final = {
    1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4,
    10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8, 129: 1,
}  # fmt: skip
_FORMATS: Final = {
    1: "B", 3: "H", 4: "I", 6: "b", 7: "B", 8: "h", 9: "i",
    11: "f", 12: "d", 13: "I", 16: "Q", 17: "q", 18: "Q",
}  # fmt: skip
_FLOATS: Final = frozenset({11, 12})

IMAGE_WIDTH: Final = 256
IMAGE_LENGTH: Final = 257
STRIP_OFFSETS: Final = 273
STRIP_BYTE_COUNTS: Final = 279
TILE_OFFSETS: Final = 324
TILE_BYTE_COUNTS: Final = 325
SUBIFDS: Final = 330
XMP_TAG: Final = 700
MAKER_NOTE: Final = 37500
EXIF_POINTER: Final = 34665
GPS_POINTER: Final = 34853
ICC_TAG: Final = 34675
INTEROP_POINTER: Final = 40965
DNG_VERSION: Final = 50706

IFD_HEADER: Final = ("tag", "type", "count", "value")
# Child IFDs by pointer tag, and the name each one's table takes.
_CHILDREN: Final = {EXIF_POINTER: "Exif", GPS_POINTER: "GPS", SUBIFDS: "SubIFD"}
_EXIF_CHILDREN: Final = {INTEROP_POINTER: "Interop"}
_MAX_DEPTH: Final = 4
_ENTRY_BLOCK: Final = 1024  # IFD entries read from the source at a time, each charged first
_EMBEDDED: Final = frozenset({XMP_TAG, ICC_TAG})


@dataclass
class Entry:
    """One IFD entry: its row's citation, where its value is, and the value if it was copied.

    ``items`` holds ints, ``(numerator, denominator)`` pairs, floats, or strings (``None`` for a
    string that is not ASCII), or is ``None`` when the value was not copied.
    """

    tag: int
    type: int
    count: int
    locator: tuple[Locator, ...]
    value_at: tuple[int, int] | None
    items: tuple[object, ...] | None


@dataclass
class Ifd:
    """One IFD: its table's citation and id, its entries, and the IFDs its pointers declare."""

    name: str
    offset: int
    locator: tuple[Locator, ...]
    table: RecordId
    entries: list[Entry]
    next: int
    next_locator: tuple[Locator, ...]
    children: list["Ifd"] = field(default_factory=list)

    def get(self, tag: int) -> Entry | None:
        for entry in self.entries:
            if entry.tag == tag:
                return entry
        return None

    def child(self, name: str) -> "Ifd | None":
        for child in self.children:
            if child.name == name:
                return child
        return None


class Tiff:
    """The TIFF structure in ``space``. ``label`` names it in messages (``TIFF``, ``EXIF``).

    ``read_header`` first; then ``chain`` reads IFD0 and the IFDs that follow it, each with the
    IFDs its pointers declare. ``on_embedded(entry, window)`` is called for an XMP or ICC value
    that fits ``max_metadata_bytes``.
    """

    def __init__(
        self,
        ctx: Context,
        space: Space,
        label: str,
        on_embedded: Callable[[Entry, Space], None] | None = None,
    ) -> None:
        self.ctx = ctx
        self.space = space
        self.label = label
        self.on_embedded = on_embedded
        self.little = True
        self.big = False
        self.first = 0
        self.header_locator: tuple[Locator, ...] = space.cite(0, min(8, space.size))
        self.read: dict[int, Ifd] = {}

    # --- Header ---------------------------------------------------------------------------------

    def read_header(self) -> bool:
        """The byte order, classic or BigTIFF, and IFD0's offset; ``False`` with a finding."""
        out, space = self.ctx.out, self.space
        head = space.read(0, min(16, space.size))
        whole = space.whole()
        if len(head) < 8 or head[:2] not in (b"II", b"MM"):
            out.finding(MALFORMED, whole, f"the {self.label} header is not II or MM and 8 bytes")
            return False
        self.little = head[:2] == b"II"
        order = "<" if self.little else ">"
        (magic,) = struct.unpack(order + "H", head[2:4])
        if magic == 42:
            (self.first,) = struct.unpack(order + "I", head[4:8])
            return True
        if magic == 43 and len(head) == 16:
            size, zero, first = struct.unpack(order + "HHQ", head[4:16])
            if size == 8 and zero == 0:
                self.big, self.first = True, first
                self.header_locator = space.cite(0, 16)
                return True
        out.finding(
            MALFORMED,
            space.cite(0, len(head)),
            f"the {self.label} header declares version {magic}, not 42 (TIFF) or 43 (BigTIFF)",
            {"version": magic},
        )
        return False

    # --- IFDs -----------------------------------------------------------------------------------

    def chain(self) -> list[Ifd]:
        """IFD0 and every IFD after it, each with its child IFDs; stops at a damaged one."""
        found: list[Ifd] = []
        offset, declared_by = self.first, self.header_locator
        while offset:
            try:
                ifd = self.ifd(offset, f"IFD{len(found)}", declared_by, 0)
            except LimitHit as hit:
                self.ctx.stopped(hit)
                break
            if ifd is None:
                break
            found.append(ifd)
            offset, declared_by = ifd.next, ifd.next_locator
        return found

    def ifd(
        self, offset: int, name: str, declared_by: tuple[Locator, ...], depth: int
    ) -> Ifd | None:
        """The IFD at ``offset``, its rows emitted and its children read; ``None`` if unreadable."""
        ctx, out, space = self.ctx, self.ctx.out, self.space
        ctx.budget.structure()
        if offset in self.read:
            out.finding(
                IFD_LOOP,
                declared_by,
                f"{self.label} IFD {name} points at the IFD at {offset}, already read as"
                f" {self.read[offset].name}",
                {"offset": offset},
            )
            return None
        count_size, entry_size, tail = (8, 20, 8) if self.big else (2, 12, 4)
        if offset < (16 if self.big else 8) or not space.fits(offset, count_size):
            out.finding(
                BAD_OFFSET,
                declared_by,
                f"{self.label} IFD {name} is declared at {offset}, outside the {space.size}"
                " bytes of the stream",
                {"offset": offset, "size": space.size},
            )
            return None
        order = "<" if self.little else ">"
        (declared,) = struct.unpack(
            order + ("Q" if self.big else "H"), space.read(offset, count_size)
        )
        room = (space.size - offset - count_size) // entry_size
        held = min(declared, room)
        complete = declared <= room and space.fits(
            offset + count_size + declared * entry_size, tail
        )
        length = count_size + held * entry_size + (tail if complete else 0)
        locator = space.cite(offset, length)
        table = out.table(
            locator,
            Known(name, out.provenance(declared_by, AssertionKind.STATED)),
            IFD_HEADER,
            f"IFD {name}",
            AssertionKind.STATED,
        )
        if table is None:
            return None
        if not complete:
            out.finding(
                TRUNCATED,
                locator,
                f"{self.label} IFD {name} declares {declared} entries and the stream holds"
                f" {held} of them and no next-IFD offset",
                {"declared": declared, "held": held},
            )
        next_at = offset + count_size + held * entry_size
        ifd = Ifd(name, offset, locator, table, [], 0, space.cite(next_at, tail))
        self.read[offset] = ifd
        try:
            block, block_at = b"", 0
            for index in range(held):
                ctx.budget.entry()
                if index == block_at + len(block) // entry_size:  # the next block of entries
                    block_at = index
                    block = space.read(
                        offset + count_size + index * entry_size,
                        min(_ENTRY_BLOCK, held - index) * entry_size,
                    )
                start = (index - block_at) * entry_size
                at = offset + count_size + index * entry_size
                ifd.entries.append(self._entry(ifd, index, at, block[start : start + entry_size]))
        except LimitHit as hit:
            ctx.stopped(hit)
            return ifd
        if complete:
            (ifd.next,) = struct.unpack(
                order + ("Q" if self.big else "I"), space.read(next_at, tail)
            )
        repeated = sorted(tag for tag, n in Counter(e.tag for e in ifd.entries).items() if n > 1)
        if repeated:
            out.finding(
                REPEATED,
                locator,
                f"{self.label} IFD {name} repeats tags {repeated}; the first of each is read",
                {"tags": list(repeated)},
            )
        self._children(ifd, depth)
        return ifd

    def _children(self, ifd: Ifd, depth: int) -> None:
        pointers = dict(_CHILDREN)
        if ifd.name == "Exif":
            pointers.update(_EXIF_CHILDREN)
        for entry in ifd.entries:
            child = pointers.get(entry.tag)
            if child is None or entry.items is None:
                continue
            if depth >= _MAX_DEPTH:
                self.ctx.out.finding(
                    MALFORMED,
                    entry.locator,
                    f"{self.label} IFD {ifd.name} nests child IFDs deeper than {_MAX_DEPTH}",
                    {"depth": depth},
                )
                continue
            for offset in entry.items:
                if not isinstance(offset, int):
                    continue
                try:
                    found = self.ifd(offset, child, entry.locator, depth + 1)
                except LimitHit as hit:
                    self.ctx.stopped(hit)
                    return
                if found is not None:
                    ifd.children.append(found)

    # --- Entries --------------------------------------------------------------------------------

    def _entry(self, ifd: Ifd, index: int, at: int, raw: bytes) -> Entry:
        out = self.ctx.out
        order = "<" if self.little else ">"
        if self.big:
            tag, kind, count = struct.unpack(order + "HHQ", raw[:12])
            field_at, field_size = 12, 8
        else:
            tag, kind, count = struct.unpack(order + "HHI", raw[:8])
            field_at, field_size = 8, 4
        locator = (*ifd.locator, Row(index))
        cells: list[CellInput] = [tag, kind, count]
        entry = Entry(tag, kind, count, locator, None, None)
        unit = _SIZES.get(kind)
        size = None if unit is None else count * unit
        if size is not None and size <= field_size:
            entry.value_at = (at + field_at, size)
        elif size is not None:
            (pointer,) = struct.unpack(
                order + ("Q" if self.big else "I"), raw[field_at : field_at + field_size]
            )
            if self.space.fits(pointer, size):
                entry.value_at = (pointer, size)
            else:
                out.finding(
                    BAD_OFFSET,
                    locator,
                    f"tag {tag} in {self.label} IFD {ifd.name} declares {size} bytes at"
                    f" {pointer}, outside the {self.space.size} bytes of the stream",
                    {"offset": pointer, "size": size, "tag": tag},
                )
        reason = self._withheld(entry, size)
        if reason is None and entry.value_at is not None and tag not in _EMBEDDED:
            data = self.space.read(*entry.value_at)
            entry.items, values = self._decode(kind, count, data, entry, ifd)
            cells.extend(values)
        elif reason is not None:
            subject = locator if entry.value_at is None else self.space.cite(*entry.value_at)
            out.finding(
                VALUE_NOT_COPIED,
                subject,
                f"the value of tag {tag} in {self.label} IFD {ifd.name} is not copied ({reason})",
                {"count": count, "reason": reason, "tag": tag, "type": kind},
            )
        if tag in _EMBEDDED and entry.value_at is not None and self.on_embedded is not None:
            offset, length = entry.value_at
            if length <= self.ctx.max_metadata_bytes:
                self.on_embedded(entry, self.space.window(offset, length))
            else:
                out.finding(
                    VALUE_NOT_COPIED,
                    self.space.cite(offset, length),
                    f"tag {tag} holds {length} bytes, over max_metadata_bytes; it is not read",
                    {"count": count, "reason": "too_large", "tag": tag, "type": kind},
                )
        out.row(ifd.table, ifd.locator, index, cells)
        return entry

    def _withheld(self, entry: Entry, size: int | None) -> str | None:
        """Why the value is not copied, or ``None`` if it is (or cannot be read at all)."""
        if size is None:
            return "unknown_type"
        if entry.value_at is None or entry.tag in _EMBEDDED:
            return None
        if entry.tag == MAKER_NOTE:
            return "maker_note"
        if size > self.ctx.max_value_bytes:
            return "too_large"
        return None

    def _decode(
        self, kind: int, count: int, data: bytes, entry: Entry, ifd: Ifd
    ) -> tuple[tuple[object, ...], list[CellInput]]:
        order = "<" if self.little else ">"
        if kind in (ASCII, UTF8):
            strings: list[object] = []
            cells: list[CellInput] = []
            text = data.rstrip(b"\x00")
            for part in text.split(b"\x00") if text else ():
                try:
                    value = part.decode("ascii" if kind == ASCII else "utf-8")
                except UnicodeDecodeError:
                    self.ctx.out.finding(
                        VALUE_UNREADABLE,
                        entry.locator,
                        f"tag {entry.tag} in {self.label} IFD {ifd.name} is declared"
                        f" {'ASCII' if kind == ASCII else 'UTF-8'} and its bytes are not",
                        {"tag": entry.tag},
                    )
                    strings.append(None)
                    cells.append(Unknown())
                    continue
                strings.append(value)
                cells.append(value)
            return tuple(strings), cells
        if kind in (RATIONAL, SRATIONAL):
            numbers = struct.unpack(f"{order}{2 * count}{'I' if kind == RATIONAL else 'i'}", data)
            pairs = tuple(zip(numbers[0::2], numbers[1::2], strict=True))
            return pairs, list(numbers)
        values = struct.unpack(f"{order}{count}{_FORMATS[kind]}", data)
        if kind in _FLOATS:
            return values, [float(value) for value in values]
        return values, list(values)

    def numbers(self, entry: Entry, limit: int) -> tuple[int, ...] | None:
        """``entry``'s integer values, read even if not copied, if at most ``limit`` of them."""
        if entry.items is not None:
            return tuple(v for v in entry.items if isinstance(v, int))
        if entry.value_at is None or entry.count > limit or entry.type not in _FORMATS:
            return None
        if entry.type in _FLOATS:
            return None
        data = self.space.read(*entry.value_at)
        order = "<" if self.little else ">"
        return tuple(struct.unpack(f"{order}{entry.count}{_FORMATS[entry.type]}", data))
