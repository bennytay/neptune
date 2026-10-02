"""PX4 ULog (https://docs.px4.io/main/en/dev_log/ulog_file_format.html), version 0 and 1: constants,
message formats and the row layouts they give, and the typed key/value pairs of info and parameter
messages. Parsing only; nothing here reads a source or builds a record.
"""

import re
import struct
from dataclasses import dataclass
from typing import Final

from neptune.adapters.flightlog.common import MAX_COLUMNS
from neptune.model.series import ColumnType

MAGIC: Final = b"ULog\x01\x12\x35"
HEADER_SIZE: Final = 16
SYNC_MAGIC: Final = bytes([0x2F, 0x73, 0x13, 0x20, 0x25, 0x0C, 0xBB, 0x12])
SYNC_MESSAGE: Final = b"\x08\x00S" + SYNC_MAGIC  # header (size 8, type 'S') + magic
FLAG_BITS_SIZE: Final = 8 + 8 + 3 * 8
DATA_APPENDED: Final = 0x1  # incompat_flags[0], bit 0: the only incompatible flag defined

DEFINITION_TYPES: Final = frozenset(b"BFIMPQ")  # messages of the definitions section
DEFINED_TYPES: Final = frozenset(b"BFIMPQARDLCSO")
# Message sizes and types a parser trusts: an upper-case ASCII letter is a type the format may
# still define (skipped by size when unknown); anything else is a damaged header.
PLAUSIBLE_TYPES: Final = frozenset(range(ord("A"), ord("Z") + 1))

MAX_FORMATS: Final = 4096
MAX_STREAMS: Final = 4096
MAX_DEPTH: Final = 8
MAX_NODES: Final = 8192  # fields visited, padding and nested repeats included, per layout
LOG_NODES: Final = 500_000  # fields visited for all layouts of one log
MAX_ARRAY: Final = 65535
MAX_ROW_BYTES: Final = 65535

# type name -> (struct code, size, column type)
PRIMITIVES: Final = {
    "int8_t": ("b", 1, ColumnType.INT8),
    "uint8_t": ("B", 1, ColumnType.UINT8),
    "int16_t": ("h", 2, ColumnType.INT16),
    "uint16_t": ("H", 2, ColumnType.UINT16),
    "int32_t": ("i", 4, ColumnType.INT32),
    "uint32_t": ("I", 4, ColumnType.UINT32),
    "int64_t": ("q", 8, ColumnType.INT64),
    "uint64_t": ("Q", 8, ColumnType.UINT64),
    "float": ("f", 4, ColumnType.FLOAT32),
    "double": ("d", 8, ColumnType.FLOAT64),
    "bool": ("?", 1, ColumnType.BOOL),
    "char": ("c", 1, ColumnType.STRING),
}
_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FIELD: Final = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[([0-9]+)\])?")


class FormatError(ValueError):
    """A format, key or layout that does not hold; the message is the reason (no source text)."""


@dataclass(frozen=True)
class Field:
    type: str
    count: int | None  # None: a scalar
    name: str


@dataclass(frozen=True)
class MessageFormat:
    name: str
    fields: tuple[Field, ...]


def parse_format(text: str) -> MessageFormat:
    """``name:type name;type[N] name;...`` -> its fields, in order."""
    name, colon, body = text.partition(":")
    if not colon or not _NAME.fullmatch(name):
        raise FormatError("a format is `name:fields`, the name an identifier")
    fields: list[Field] = []
    seen: set[str] = set()
    for part in body.split(";"):
        if not part:
            continue
        kind, space, rest = part.partition(" ")
        match = _FIELD.fullmatch(kind)
        if not space or match is None or not _NAME.fullmatch(rest):
            raise FormatError("a field is `type name` or `type[N] name`")
        count = int(match.group(2)) if match.group(2) is not None else None
        if count is not None and not 1 <= count <= MAX_ARRAY:
            raise FormatError(f"an array holds 1 to {MAX_ARRAY} elements")
        if rest in seen:
            raise FormatError("a field name repeats")
        seen.add(rest)
        fields.append(Field(match.group(1), count, rest))
    if not fields:
        raise FormatError("a format has at least one field")
    return MessageFormat(name, tuple(fields))


@dataclass(frozen=True)
class Item:
    """One flattened column of a layout."""

    path: str
    type: ColumnType
    repeated: bool
    count: int  # elements in a repeated column, else 1
    string: bool
    start: int  # index of its first value in the unpacked tuple


@dataclass(frozen=True)
class Layout:
    """How one message's bytes unpack: a struct, and the columns its values make."""

    struct: struct.Struct
    size: int
    min_size: int  # bytes up to the last field that is not trailing padding
    items: tuple[Item, ...]  # the value columns; the timestamp is not among them
    time_index: int | None  # index in the unpacked tuple of the top-level uint64 `timestamp`
    time_offset: int | None  # byte offset of that timestamp in the message data
    strings: tuple[tuple[int, int], ...]  # (byte offset, width) of every char-array column
    nested: tuple[str, ...]  # the nested format names the layout used, sorted


class WorkLimitError(FormatError):
    """The layouts of this log have used all the work the adapter spends on them."""


class Work:
    """What is left of a log's layout budget, shared by every layout of the plan."""

    def __init__(self, nodes: int = LOG_NODES) -> None:
        self.left = nodes


class _State:
    def __init__(self) -> None:
        self.size = 0
        self.nodes = 0
        self.values = 0
        self.data_end = 0
        self.time: int | None = None
        self.time_offset: int | None = None


def layout_of(name: str, formats: dict[str, MessageFormat], work: Work | None = None) -> Layout:
    """Flatten ``name`` (nested types and arrays expanded) into one struct. Raises FormatError."""
    codes: list[str] = []
    items: list[Item] = []
    used: set[str] = set()
    strings: list[tuple[int, int]] = []
    state = _State()

    def flat(type_name: str, prefix: str, stack: tuple[str, ...]) -> None:
        if len(stack) > MAX_DEPTH:
            raise FormatError(f"nested formats go deeper than {MAX_DEPTH}")
        if type_name in stack:
            raise FormatError("a format contains itself")
        found = formats.get(type_name)
        if found is None:
            raise FormatError("a field names a type no format defines")
        if type_name != name or stack:
            used.add(type_name)
        for field in found.fields:
            state.nodes += 1
            if work is not None:
                work.left -= 1
                if work.left < 0:
                    raise WorkLimitError("the log's formats need more layout work than is spent")
            if state.nodes > MAX_NODES:
                raise FormatError(f"a message visits more than {MAX_NODES} fields")
            path = prefix + field.name
            if field.type in PRIMITIVES:
                code, size, column = PRIMITIVES[field.type]
                total = size * (field.count or 1)
                state.size += total
                if state.size > MAX_ROW_BYTES:
                    raise FormatError(f"a message is at most {MAX_ROW_BYTES} bytes")
                if field.name.startswith("_padding"):
                    codes.append(f"{total}x")
                    continue
                before = state.size - total
                state.data_end = state.size
                top_timestamp = prefix == "" and field.name == "timestamp"
                if top_timestamp and field.type == "uint64_t" and field.count is None:
                    state.time = state.values
                    state.time_offset = before
                    state.values += 1
                    codes.append(code)
                    continue
                string = field.type == "char"
                if string:
                    codes.append(f"{field.count or 1}s")
                    count, repeated = 1, False
                    width = 1
                    strings.append((before, total))
                else:
                    codes.append(code if field.count is None else f"{field.count}{code}")
                    count, repeated = field.count or 1, field.count is not None
                    width = count
                items.append(Item(path, column, repeated, count, string, state.values))
                state.values += width
                if len(items) > MAX_COLUMNS:
                    raise FormatError(f"a message flattens to at most {MAX_COLUMNS} columns")
            elif field.count is None:
                flat(field.type, path + ".", (*stack, type_name))
            else:
                for index in range(field.count):
                    flat(field.type, f"{path}[{index}].", (*stack, type_name))

    flat(name, "", ())
    paths = [item.path for item in items]
    if len(set(paths)) != len(paths):
        raise FormatError("two fields flatten to one column name")
    packed = struct.Struct("<" + "".join(codes))
    return Layout(
        packed,
        packed.size,
        state.data_end,
        tuple(items),
        state.time,
        state.time_offset,
        tuple(strings),
        tuple(sorted(used)),
    )


# --- Typed keys: info, multi info and parameters ------------------------------------------------


@dataclass(frozen=True)
class KeyType:
    """A key's declared type: `char[13]`, `float`, `uint32_t[2]`."""

    text: str
    base: str
    count: int | None

    @property
    def size(self) -> int:
        return PRIMITIVES[self.base][1] * (self.count or 1)


def parse_key(raw: bytes) -> tuple[KeyType, str, int]:
    """``type name`` -> the type, the name and where the name starts in ``raw``.

    Raises FormatError when the key is not valid text, not `type name`, or names no primitive type.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FormatError("a key is not UTF-8") from exc
    kind, space, name = text.partition(" ")
    match = _FIELD.fullmatch(kind)
    if not space or match is None or not name or "\0" in name:
        raise FormatError("a key is `type name`")
    base = match.group(1)
    if base not in PRIMITIVES:
        raise FormatError("a key's type is one of the primitive types")
    count = int(match.group(2)) if match.group(2) is not None else None
    if count is not None and not 1 <= count <= MAX_ARRAY:
        raise FormatError("a key's array holds 1 to 65535 elements")
    return KeyType(kind, base, count), name, len(kind.encode("utf-8")) + 1


def decode_value(kind: KeyType, raw: bytes) -> tuple[object, ...] | bytes:
    """A key's value: the bytes of a `char[N]`, else the N numbers. Raises FormatError."""
    if len(raw) != kind.size:
        raise FormatError("a value is as long as its declared type")
    if kind.base == "char":
        return raw
    code = PRIMITIVES[kind.base][0]
    return struct.unpack(f"<{kind.count or 1}{code}", raw)
