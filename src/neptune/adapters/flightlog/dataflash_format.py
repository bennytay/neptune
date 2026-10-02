"""ArduPilot DataFlash binary logs (`.bin`): constants and the row layout a FMT record gives.

A record is `A3 95`, a type byte and a payload whose length the FMT record for that type declares.
Parsing only; nothing here reads a source or builds a record.
"""

import struct
from dataclasses import dataclass
from typing import Final

from neptune.adapters.flightlog.ulog_format import FormatError
from neptune.model.series import ColumnType

HEAD: Final = b"\xa3\x95"
HEADER: Final = 3
FMT_TYPE: Final = 128
FMT_LENGTH: Final = 89
FMT_PAYLOAD: Final = struct.Struct("<BB4s16s64s")
BUILTIN_FMT: Final = FMT_PAYLOAD.pack(
    FMT_TYPE, FMT_LENGTH, b"FMT", b"BBnNZ", b"Type,Length,Name,Format,Columns"
)
# Types that describe the other types; their records are read for units, not made into streams.
DEFINITIONS: Final = frozenset({"FMT", "FMTU", "UNIT", "MULT"})
PARAMETERS: Final = "PARM"
TIME_LABELS: Final = {"TimeUS": "us", "TimeMS": "ms"}

# format character -> (struct code, size, column type, array length or 0)
CHARS: Final = {
    "a": ("32h", 64, ColumnType.INT16, 32),
    "b": ("b", 1, ColumnType.INT8, 0),
    "B": ("B", 1, ColumnType.UINT8, 0),
    "h": ("h", 2, ColumnType.INT16, 0),
    "H": ("H", 2, ColumnType.UINT16, 0),
    "i": ("i", 4, ColumnType.INT32, 0),
    "I": ("I", 4, ColumnType.UINT32, 0),
    "f": ("f", 4, ColumnType.FLOAT32, 0),
    "d": ("d", 8, ColumnType.FLOAT64, 0),
    "n": ("4s", 4, ColumnType.STRING, 0),
    "N": ("16s", 16, ColumnType.STRING, 0),
    "Z": ("64s", 64, ColumnType.STRING, 0),
    "c": ("h", 2, ColumnType.INT16, 0),  # the value times 100
    "C": ("H", 2, ColumnType.UINT16, 0),  # the value times 100
    "e": ("i", 4, ColumnType.INT32, 0),  # the value times 100
    "E": ("I", 4, ColumnType.UINT32, 0),  # the value times 100
    "L": ("i", 4, ColumnType.INT32, 0),  # degrees times 10^7
    "M": ("B", 1, ColumnType.UINT8, 0),  # a flight mode number
    "q": ("q", 8, ColumnType.INT64, 0),
    "Q": ("Q", 8, ColumnType.UINT64, 0),
}
INTEGER_CHARS: Final = frozenset("bBhHiIqQ")


@dataclass(frozen=True)
class DfColumn:
    label: str
    type: ColumnType
    repeated: bool
    string: bool
    start: int  # index of its first value in the unpacked tuple
    count: int  # values it takes in the tuple


@dataclass(frozen=True)
class DfLayout:
    struct: struct.Struct
    chars: str
    labels: tuple[str, ...]
    offsets: tuple[int, ...]  # byte offset of each label's field in the payload
    widths: tuple[int, ...]
    columns: tuple[DfColumn, ...]  # every label but the time one
    time_label: str | None
    time_index: int | None  # index in the tuple of the time value
    time_char: str | None
    starts: tuple[int, ...]  # index in the tuple of each label's first value
    strings: tuple[tuple[int, int], ...]  # (byte offset, width) of every text field


def layout_of(chars: str, labels: tuple[str, ...], length: int) -> DfLayout:
    """The layout a FMT's format characters and labels give. Raises FormatError."""
    if not chars or len(chars) != len(labels):
        raise FormatError("a format has one label per format character")
    if len(set(labels)) != len(labels) or any(not label for label in labels):
        raise FormatError("labels are non-empty and unique")
    codes: list[str] = []
    columns: list[DfColumn] = []
    offsets: list[int] = []
    widths: list[int] = []
    starts: list[int] = []
    strings: list[tuple[int, int]] = []
    at = 0
    values = 0
    time_label: str | None = None
    time_index: int | None = None
    for index, (char, label) in enumerate(zip(chars, labels, strict=True)):
        found = CHARS.get(char)
        if found is None:
            raise FormatError("a format character the log format does not define")
        code, size, kind, array = found
        codes.append(code)
        offsets.append(at)
        widths.append(size)
        starts.append(values)
        if kind is ColumnType.STRING:
            strings.append((at, size))
        at += size
        take = array or 1
        if index == 0 and label in TIME_LABELS and char in INTEGER_CHARS:
            time_label, time_index = label, values
        else:
            columns.append(
                DfColumn(label, kind, bool(array), kind is ColumnType.STRING, values, take)
            )
        values += take
    packed = struct.Struct("<" + "".join(codes))
    if packed.size + HEADER != length:
        raise FormatError("the declared record length is not the one its format gives")
    return DfLayout(
        packed,
        chars,
        labels,
        tuple(offsets),
        tuple(widths),
        tuple(columns),
        time_label,
        time_index,
        chars[0] if time_label else None,
        tuple(starts),
        tuple(strings),
    )
