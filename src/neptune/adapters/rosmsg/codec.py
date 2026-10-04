"""Decoding ROS payloads into typed series columns: a layout compiled once per stream, then one
walk per message (ADR 0068 §1).

``compile_layout`` turns a parsed ``Definition`` into the columns a stream's series gains and a
program that reads a payload into them. A column is named by its field path, as
``neptune.derived.schemas`` writes paths: segments joined by ``.``, ``[]`` after an array field
(``transforms[].header.frame_id``). A path under no array is a scalar column; under one array
level a repeated column, one list per message. Three kinds of path are walked but get no column
(``LeftOut``): a byte array (``uint8[]``, ``byte[]``, ``char[]``, ``octet``: an image's or a point
cloud's bytes, which the row already cites whole), a path under two array levels (no column type
holds a list of lists), and nothing else. ROS 1's ``time`` and ``duration`` are two columns,
``<path>.secs`` and ``<path>.nsecs``, as the format defines them.

``Decoder.decode`` walks one payload: ROS 2's CDR (the encapsulation header, then XCDR1 with
alignment counted from after it, either byte order) or ROS 1's serialisation (little-endian,
packed). Every count and length is checked against the bytes left before anything is read or
allocated, an array is bounded by ``max_array_items``, a whole message by ``max_message_bytes``,
and the walk by ``max_walk_items``: every element of every array walked one at a time costs one,
whatever its size, so arrays of a type with no bytes (a ROS 1 empty message, ``T[0]``) cannot
make a payload of a few bytes cost a walk of millions. Parts of a layout that take no bytes on the
wire read nothing and store nothing, so they are never walked.

An empty message is not the same on both wires. ROS 1 serialises it as nothing. rosidl gives a
ROS 2 message with no fields one ``uint8 structure_needs_at_least_one_member`` that its ``.msg``
text does not show, so under CDR an empty message is one byte, read and given no column. A payload
that breaks its layout raises ``Malformed``, never anything else. Text that is not UTF-8 makes only
its own cell unknown.

Compiling is bounded too: a definition is a graph of types, and the layout unrolls it along every
path, so ``MAX_LAYOUT_NODES`` caps the fields the layout visits (with and without a column). The
count is taken over the graph before anything is unrolled (``layout_nodes``), so a definition past
the cap costs its own size, not the cap.
"""

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from neptune.adapters.rosmsg.definitions import (
    BYTE_NAMES,
    ArrayKind,
    Definition,
    DefinitionError,
    FieldDef,
    MessageDef,
)
from neptune.model.series import ColumnType

COLUMN_TYPES: Final = {
    "bool": ColumnType.BOOL,
    "int8": ColumnType.INT8,
    "uint8": ColumnType.UINT8,
    "int16": ColumnType.INT16,
    "uint16": ColumnType.UINT16,
    "int32": ColumnType.INT32,
    "uint32": ColumnType.UINT32,
    "int64": ColumnType.INT64,
    "uint64": ColumnType.UINT64,
    "float32": ColumnType.FLOAT32,
    "float64": ColumnType.FLOAT64,
    "string": ColumnType.STRING,
}
_CODES: Final = {
    "bool": "B",
    "int8": "b",
    "uint8": "B",
    "int16": "h",
    "uint16": "H",
    "int32": "i",
    "uint32": "I",
    "int64": "q",
    "uint64": "Q",
    "float32": "f",
    "float64": "d",
}
_U32: Final = (struct.Struct("<I"), struct.Struct(">I"))
MAX_DEPTH: Final = 32
# Fields one layout may visit, columns and left-out paths alike (a definition is a DAG of types
# the layout unrolls along every path: 21 types of two fields each are 2^21 paths).
MAX_LAYOUT_NODES: Final = 16_384
NANOS: Final = 1_000_000_000


class Malformed(ValueError):
    """A payload that does not hold its layout. ``limit`` marks one past a decoding limit (an
    array or a message too large), which is not the payload's fault."""

    def __init__(self, reason: str, limit: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.limit = limit


class _BadText:
    """A cell whose text is not UTF-8: unknown, the rest of the row still decoded."""


BAD_TEXT: Final = _BadText()


@dataclass(frozen=True)
class Column:
    path: str
    type: ColumnType
    repeated: bool


@dataclass(frozen=True)
class LeftOut:
    path: str
    reason: str  # byte_array, nested_array, name_taken


@dataclass(frozen=True)
class DecodeLimits:
    max_columns: int = 512
    max_array_items: int = 65_536
    max_message_bytes: int = 16 << 20
    # Array elements one message's walk visits one at a time (packed primitive arrays are one
    # step): a fixed bound of the decoder, not configurable; past it the message is not covered.
    max_walk_items: int = 1 << 22


# --- The program --------------------------------------------------------------------------------


class _State:
    """One walk: the payload, where it is, its byte order, where alignment counts from."""

    __slots__ = ("big", "buf", "cdr", "cells", "limits", "origin", "pos", "walked")

    def __init__(
        self,
        buf: bytes | memoryview,
        pos: int,
        cdr: bool,
        big: bool,
        cells: list[object],
        limits: DecodeLimits,
    ) -> None:
        self.buf, self.pos, self.cdr, self.big = buf, pos, cdr, big
        self.origin = pos
        self.cells = cells
        self.limits = limits
        self.walked = 0

    def walk(self, items: int) -> None:
        """Charge ``items`` array elements to the message's walk budget."""
        self.walked += items
        if self.walked > self.limits.max_walk_items:
            raise Malformed("walk_limit", limit=True)

    def align(self, size: int) -> None:
        if self.cdr and size > 1:
            self.pos += -(self.pos - self.origin) % size

    def need(self, size: int) -> None:
        if size > len(self.buf) - self.pos:
            raise Malformed("short")

    def count(self) -> int:
        self.align(4)
        self.need(4)
        value: int = _U32[self.big].unpack_from(self.buf, self.pos)[0]
        self.pos += 4
        return value


class _Node:
    min_size = 0  # the fewest bytes the node takes on the wire, alignment aside

    def read(self, state: _State, depth: int) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


def _put(state: _State, slot: int | None, depth: int, value: object) -> None:
    """Store a leaf's value: a scalar column at depth 0, an item of a list at depth 1."""
    if slot is None:
        return
    if depth == 0:
        state.cells[slot] = value
    else:
        cell = state.cells[slot]
        if isinstance(cell, list):
            cell.append(value)


class _Primitive(_Node):
    def __init__(self, wire: str, slot: int | None) -> None:
        code = _CODES[wire]
        self.size = struct.calcsize(code)
        self.structs = (struct.Struct("<" + code), struct.Struct(">" + code))
        self.bool = wire == "bool"
        self.slot = slot
        self.min_size = self.size

    def read(self, state: _State, depth: int) -> None:
        state.align(self.size)
        state.need(self.size)
        value = self.structs[state.big].unpack_from(state.buf, state.pos)[0]
        state.pos += self.size
        if self.bool:
            if value > 1:
                raise Malformed("bool")
            value = bool(value)
        _put(state, self.slot, depth, value)


class _String(_Node):
    min_size = 4

    def __init__(self, bound: int | None, slot: int | None) -> None:
        self.bound = bound
        self.slot = slot

    def read(self, state: _State, depth: int) -> None:
        length = state.count()
        state.need(length)
        start = state.pos
        state.pos += length
        if state.cdr and length:  # CDR counts the terminating NUL
            if state.buf[state.pos - 1] != 0:
                raise Malformed("string_terminator")
            length -= 1
        if self.bound is not None and length > self.bound:
            raise Malformed("string_bound")
        if self.slot is None:
            return
        try:
            text: object = bytes(state.buf[start : start + length]).decode("utf-8")
        except UnicodeDecodeError:
            text = BAD_TEXT
        _put(state, self.slot, depth, text)


class _Time(_Node):
    """ROS 1 ``time`` (two uint32) or ``duration`` (two int32): seconds, nanoseconds."""

    min_size = 8

    def __init__(self, signed: bool, slots: tuple[int | None, int | None]) -> None:
        self.struct = struct.Struct("<ii" if signed else "<II")
        self.slots = slots

    def read(self, state: _State, depth: int) -> None:
        state.need(8)
        secs, nsecs = self.struct.unpack_from(state.buf, state.pos)
        state.pos += 8
        _put(state, self.slots[0], depth, secs)
        _put(state, self.slots[1], depth, nsecs)


class _Struct(_Node):
    def __init__(self, members: list[_Node]) -> None:
        # A member that takes no bytes on the wire (an empty message, ``T[0]``, arrays of those)
        # reads nothing and stores nothing: it is not walked.
        self.members = [member for member in members if member.min_size]
        self.min_size = sum(member.min_size for member in members)

    def read(self, state: _State, depth: int) -> None:
        for member in self.members:
            member.read(state, depth)


class _Array(_Node):
    def __init__(self, kind: ArrayKind, length: int | None, element: _Node) -> None:
        self.kind, self.length, self.element = kind, length, element
        # Arrays of fixed-size primitives are read in one step.
        self.packed = element if isinstance(element, _Primitive) else None
        if kind is ArrayKind.FIXED:
            self.min_size = (length or 0) * element.min_size
        else:
            self.min_size = 4

    def read(self, state: _State, depth: int) -> None:
        if self.kind is ArrayKind.FIXED:
            assert self.length is not None
            count = self.length
        else:
            count = state.count()
            if self.kind is ArrayKind.BOUNDED and self.length is not None and count > self.length:
                raise Malformed("array_bound")
        if count > state.limits.max_array_items:
            raise Malformed("array_limit", limit=True)
        packed = self.packed
        if packed is not None:
            if count:  # an empty sequence's elements add no padding
                state.align(packed.size)
            state.need(count * packed.size)
            slot = packed.slot
            if slot is not None and depth == 0:
                code = packed.structs[state.big].format[1:]
                order = ">" if state.big else "<"
                values = struct.unpack_from(f"{order}{count}{code}", state.buf, state.pos)
                if packed.bool:
                    if any(value > 1 for value in values):
                        raise Malformed("bool")
                    values = tuple(bool(value) for value in values)
                state.cells[slot] = list(values)
            state.pos += count * packed.size
            return
        # a count past what the bytes left could hold is a lie, refused before the walk
        state.need(count * self.element.min_size)
        state.walk(count)
        if not self.element.min_size:
            return  # elements that take no bytes read and store nothing
        for _ in range(count):
            self.element.read(state, depth + 1)


class _Skip(_Node):
    """A byte array left out: its count checked, its bytes passed over."""

    def __init__(self, kind: ArrayKind, length: int | None) -> None:
        self.kind, self.length = kind, length
        self.min_size = (length or 0) if kind is ArrayKind.FIXED else 4

    def read(self, state: _State, depth: int) -> None:
        if self.kind is ArrayKind.FIXED:
            assert self.length is not None
            count = self.length
        else:
            count = state.count()
            if self.kind is ArrayKind.BOUNDED and self.length is not None and count > self.length:
                raise Malformed("array_bound")
        state.need(count)
        state.pos += count


# --- Compiling ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class HeaderStamp:
    """Where a ``std_msgs/Header`` as the root's first field puts its stamp: the seconds and
    nanoseconds columns (by index), and the frame id's."""

    seconds: int
    nanoseconds: int
    frame: int


@dataclass(frozen=True)
class Layout:
    """The columns a stream's messages decode into, what is walked without a column, and the
    program. ``header_only``: only the leading header is read (the rest could not be)."""

    root: str
    columns: tuple[Column, ...]
    left_out: tuple[LeftOut, ...]
    header: HeaderStamp | None
    header_only: bool
    program: _Struct


class _Compiler:
    def __init__(
        self, definition: Definition, limits: DecodeLimits, ros1: bool, reserved: frozenset[str]
    ) -> None:
        self.types = definition.types
        self.limits = limits
        self.ros1 = ros1
        self.reserved = reserved
        self.columns: list[Column] = []
        self.left_out: list[LeftOut] = []
        self.nodes = 0

    def visit(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_LAYOUT_NODES:
            raise DefinitionError(
                "node_limit", f"the layout visits more than {MAX_LAYOUT_NODES} fields"
            )

    def column(self, path: str, wire: str, arrays: int) -> int | None:
        if arrays > 1:
            self.left_out.append(LeftOut(path, "nested_array"))
            return None
        if path in self.reserved:
            self.left_out.append(LeftOut(path, "name_taken"))
            return None
        if len(self.columns) >= self.limits.max_columns:
            raise DefinitionError(
                "column_limit", f"more than {self.limits.max_columns} decoded columns"
            )
        self.columns.append(Column(path, COLUMN_TYPES[wire], arrays == 1))
        return len(self.columns) - 1

    def message(
        self, message: MessageDef, prefix: str, arrays: int, ancestors: tuple[str, ...]
    ) -> _Struct:
        if len(ancestors) > MAX_DEPTH:
            raise DefinitionError("nesting_limit", f"fields nest deeper than {MAX_DEPTH}")
        if not message.fields and not self.ros1:
            # rosidl's one-byte placeholder of a ROS 2 message with no fields: read, no column
            return _Struct([_Primitive("uint8", None)])
        return _Struct([self.field(f, prefix, arrays, ancestors) for f in message.fields])

    def field(self, field: FieldDef, prefix: str, arrays: int, ancestors: tuple[str, ...]) -> _Node:
        self.visit()
        path = prefix + field.name + ("[]" if field.array is not None else "")
        inner = arrays + (1 if field.array is not None else 0)
        if field.array is not None and field.declared in BYTE_NAMES:
            self.left_out.append(LeftOut(path, "byte_array"))
            return _Skip(field.array, field.length)
        element = self.element(field, path, inner, ancestors)
        if field.array is None:
            return element
        return _Array(field.array, field.length, element)

    def element(self, field: FieldDef, path: str, arrays: int, ancestors: tuple[str, ...]) -> _Node:
        wire = field.wire
        if wire is None:
            if field.type in ancestors:
                raise DefinitionError("unsupported", f"{field.type} holds itself")
            return self.message(
                self.types[field.type], path + ".", arrays, (*ancestors, field.type)
            )
        if wire == "wstring":
            raise DefinitionError("unsupported", "a wstring field is not decoded")
        if wire in ("time", "duration"):
            seconds = self.column(
                path + ".secs", "int32" if wire == "duration" else "uint32", arrays
            )
            nanos = self.column(
                path + ".nsecs", "int32" if wire == "duration" else "uint32", arrays
            )
            return _Time(wire == "duration", (seconds, nanos))
        slot = self.column(path, wire, arrays)
        if wire == "string":
            return _String(field.bound, slot)
        return _Primitive(wire, slot)


def layout_nodes(definition: Definition, fields: Sequence[FieldDef], cap: int) -> int:
    """How many fields a layout of ``fields`` visits, unrolled along every path, counted over the
    type graph without unrolling it (each type once); at most ``cap + 1``. A type that holds itself
    counts as nothing here: compiling refuses it."""
    counted: dict[str, int] = {}
    open_types: set[str] = set()

    def of_type(name: str) -> int:
        if name in counted:
            return counted[name]
        if name in open_types:
            return 0
        open_types.add(name)
        message = definition.types.get(name)
        total = of_fields(message.fields) if message is not None else 0
        open_types.discard(name)
        counted[name] = total
        return total

    def of_fields(items: Sequence[FieldDef]) -> int:
        total = 0
        for item in items:
            total += 1
            byte_array = item.array is not None and item.declared in BYTE_NAMES
            if item.wire is None and not byte_array:
                total += of_type(item.type)
            if total > cap:
                return cap + 1
        return total

    return of_fields(fields)


def header_field(definition: Definition) -> FieldDef | None:
    """The root's first field when it is a ``std_msgs/Header`` called ``header`` whose stamp and
    frame id are what ROS defines: ``stamp`` (ROS 1 ``time``; ROS 2 ``builtin_interfaces/Time``
    of ``int32 sec`` and ``uint32 nanosec``) and ``string frame_id``."""
    fields = definition.root_type.fields
    if not fields:
        return None
    first = fields[0]
    if first.name != "header" or first.type != "std_msgs/Header" or first.array is not None:
        return None
    header = definition.types.get("std_msgs/Header")
    if header is None:
        return None
    named = {f.name: f for f in header.fields}
    stamp, frame = named.get("stamp"), named.get("frame_id")
    if frame is None or frame.wire != "string" or frame.array is not None or stamp is None:
        return None
    if stamp.array is not None:
        return None
    if stamp.wire == "time":
        return first
    if stamp.wire is None and stamp.type == "builtin_interfaces/Time":
        time = definition.types.get(stamp.type)
        if time is not None:
            parts = [(f.name, f.wire, f.array) for f in time.fields]
            if parts == [("sec", "int32", None), ("nanosec", "uint32", None)]:
                return first
    return None


def compile_layout(
    definition: Definition,
    limits: DecodeLimits,
    *,
    header_only: bool = False,
    reserved: frozenset[str] = frozenset(),
) -> Layout:
    """The layout of ``definition``'s root; ``header_only`` reads only a leading header.

    ``reserved`` are paths the adapter's own columns already use (MCAP's ``sequence``): a field at
    one of them is walked without a column (``name_taken``), never a second column of one name.
    Raises ``DefinitionError`` (``column_limit``, ``node_limit``, ``nesting_limit``,
    ``unsupported``) where the layout cannot be built; a caller may then try ``header_only``.
    """
    ros1 = definition.encoding == "ros1msg"
    compiler = _Compiler(definition, limits, ros1, reserved)
    header = header_field(definition)
    root = definition.root_type
    walked = [header] if header_only and header is not None else root.fields
    if layout_nodes(definition, walked, MAX_LAYOUT_NODES) > MAX_LAYOUT_NODES:
        raise DefinitionError(
            "node_limit", f"the layout visits more than {MAX_LAYOUT_NODES} fields"
        )
    if header_only:
        if header is None:
            raise DefinitionError("unsupported", "the type has no leading std_msgs/Header")
        program = _Struct([compiler.field(header, "", 0, (root.name,))])
    else:
        program = compiler.message(root, "", 0, (root.name,))
    stamp = None
    if header is not None:
        index = {column.path: i for i, column in enumerate(compiler.columns)}
        stamp_field = next(f for f in definition.types[header.type].fields if f.name == "stamp")
        # ROS 1's primitive ``time`` is two columns, ``secs`` and ``nsecs``; a Time message's
        # fields are its own (``sec``, ``nanosec``), whichever dialect declares it.
        names = ("secs", "nsecs") if stamp_field.wire == "time" else ("sec", "nanosec")
        wanted = (f"header.stamp.{names[0]}", f"header.stamp.{names[1]}", "header.frame_id")
        if all(path in index for path in wanted):
            stamp = HeaderStamp(index[wanted[0]], index[wanted[1]], index[wanted[2]])
    return Layout(
        root.name,
        tuple(compiler.columns),
        tuple(compiler.left_out),
        stamp,
        header_only,
        program,
    )


# --- Decoding -----------------------------------------------------------------------------------


class Decoder:
    """Reads payloads of one layout. ``cdr``: ROS 2's CDR; otherwise ROS 1's serialisation."""

    def __init__(self, layout: Layout, cdr: bool, limits: DecodeLimits) -> None:
        self.layout = layout
        self.cdr = cdr
        self.limits = limits
        self.repeated = [i for i, column in enumerate(layout.columns) if column.repeated]

    def decode(self, payload: bytes | memoryview) -> list[object]:
        """One cell per column (``BAD_TEXT`` where text is not UTF-8), or ``Malformed``."""
        if len(payload) > self.limits.max_message_bytes:
            raise Malformed("message_limit", limit=True)
        cells: list[object] = [None] * len(self.layout.columns)
        for index in self.repeated:
            cells[index] = []
        big, start = False, 0
        if self.cdr:
            if len(payload) < 4:
                raise Malformed("short")
            if payload[0] != 0 or payload[1] not in (0, 1):
                raise Malformed("encapsulation")
            big, start = payload[1] == 0, 4
        state = _State(payload, start, self.cdr, big, cells, self.limits)
        self.layout.program.read(state, 0)
        if not self.layout.header_only:
            trailing = len(payload) - state.pos
            # CDR may pad a message to four bytes; ROS 1 holds exactly the fields
            if trailing > (3 if self.cdr else 0):
                raise Malformed("trailing_bytes")
        for index in self.repeated:
            cell = cells[index]
            if isinstance(cell, list):
                cells[index] = BAD_TEXT if any(v is BAD_TEXT for v in cell) else tuple(cell)
        return cells

    def stamp(self, cells: Sequence[object]) -> int | None:
        """The header stamp as nanosecond ticks; ``None`` without a header, or where the
        nanoseconds are out of their range ``[0, 10^9)``."""
        header = self.layout.header
        if header is None:
            return None
        seconds, nanos = cells[header.seconds], cells[header.nanoseconds]
        if not isinstance(seconds, int) or not isinstance(nanos, int):
            return None
        if not 0 <= nanos < NANOS:
            return None
        return seconds * NANOS + nanos
