"""A read-only SQLite reader over a ``SourceReader`` (https://sqlite.org/fileformat2.html).

rosbag2's ``sqlite3`` storage is a SQLite database, and an adapter reads bytes through a
``SourceReader``, not a path: the standard library's ``sqlite3`` needs a file, so it would need the
whole database copied to scratch for every ``plan`` and ``ingest`` call, against chunk purity and
the scratch limit (ADR 0045 §2). This reader walks the table b-trees the format defines, straight
from the source's bytes, and nothing else: no engine, no SQL, no journal, no ``-wal`` or ``-shm``
file, no write of any kind.

The database is hostile input. Every page number, length, cell pointer and key is checked against
the page and the file before it is used; a page that does not hold is a ``Problem`` and the walk
goes on with what is left, so one damaged page costs the rows under it. Pages are never read twice
by one walk beyond the file's page count (a cycle exhausts the budget), a walk is no deeper than
``MAX_DEPTH``, and nothing is allocated from a length the file states.

Only table b-trees are read (rowid tables); payloads that spill into overflow pages are measured
(their size is in the record header) but not followed: a consumer who wants the bytes reads the
overflow chain the cell names.
"""

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.contract import SourceReader

MAGIC: Final = b"SQLite format 3\x00"
HEADER_SIZE: Final = 100
MAX_DEPTH: Final = 40
INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1

LEAF_TABLE: Final = 13
INTERIOR_TABLE: Final = 5


class SqliteError(ValueError):
    """The bytes are not a database this reader can open."""


@dataclass(frozen=True)
class Problem:
    """A page or cell that does not hold: where, and why (a short stable reason)."""

    page: int
    reason: str


@dataclass(frozen=True)
class Header:
    page_size: int
    reserved: int
    declared_pages: int  # 0 when the header's own size is not valid (the file's is used)
    pages: int  # whole pages the file holds
    journal: int  # file format version numbers 18 (write) and 19 (read): 1 rollback, 2 WAL
    encoding: int  # 1 UTF-8, 2 UTF-16le, 3 UTF-16be
    truncated: bool

    @property
    def usable(self) -> int:
        return self.page_size - self.reserved


def parse_header(head: bytes, size: int) -> Header:
    """The database header of a file of ``size`` bytes starting with ``head``."""
    if len(head) < HEADER_SIZE or not head.startswith(MAGIC):
        raise SqliteError("the source does not start with SQLite's magic")
    page_size = struct.unpack_from(">H", head, 16)[0]
    if page_size == 1:
        page_size = 65536
    if page_size < 512 or page_size & (page_size - 1):
        raise SqliteError(f"page size {page_size} is not a power of two from 512 to 65536")
    reserved = head[20]
    if page_size - reserved < 480:
        raise SqliteError(f"usable page size {page_size - reserved} is below 480")
    change = struct.unpack_from(">I", head, 24)[0]
    declared = struct.unpack_from(">I", head, 28)[0]
    valid_for = struct.unpack_from(">I", head, 92)[0]
    declared_pages = declared if declared and change == valid_for else 0
    pages = size // page_size
    if pages == 0:
        raise SqliteError("the source is shorter than one page")
    truncated = size % page_size != 0 or declared_pages > pages
    return Header(
        page_size,
        reserved,
        declared_pages,
        pages,
        head[18],
        struct.unpack_from(">I", head, 56)[0],
        truncated,
    )


def varint(buf: bytes, pos: int, end: int) -> tuple[int, int]:
    """The unsigned varint at ``buf[pos]`` (1 to 9 bytes), and where it ends."""
    value = 0
    for i in range(8):
        at = pos + i
        if at >= end:
            raise SqliteError("a varint runs past its page")
        byte = buf[at]
        if byte < 0x80:
            return (value << 7) | byte, at + 1
        value = (value << 7) | (byte & 0x7F)
    at = pos + 8
    if at >= end:
        raise SqliteError("a varint runs past its page")
    return (value << 8) | buf[at], at + 1


def _signed(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value


@dataclass(frozen=True)
class Cell:
    """A table-leaf cell: its place in the file and the local part of its record."""

    rowid: int
    offset: int  # of the cell's first byte in the file
    length: int  # cell bytes in the page: sizes, local payload and any overflow page number
    body: int  # file offset of the local payload
    payload: int  # the record's full size, overflow included
    local: bytes
    page: int

    @property
    def spills(self) -> bool:
        return self.payload > len(self.local)


# Serial types (https://sqlite.org/fileformat2.html#record_format).
_FIXED: Final = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8, 7: 8, 8: 0, 9: 0}
_SIGNED: Final = {1: ">b", 2: ">h", 4: ">i", 6: ">q"}


def serial_size(serial: int) -> int:
    if serial in _FIXED:
        return _FIXED[serial]
    if serial < 12:
        raise SqliteError(f"serial type {serial} is reserved")
    return (serial - 12) // 2 if serial % 2 == 0 else (serial - 13) // 2


@dataclass(frozen=True)
class Field:
    """One column of a record: its serial type, where its bytes are, and their count."""

    serial: int
    offset: int  # file offset of the value's first byte
    size: int
    at: int  # the value's offset within the cell's local payload

    @property
    def is_null(self) -> bool:
        return self.serial == 0

    @property
    def is_int(self) -> bool:
        return 1 <= self.serial <= 6 or self.serial in (8, 9)

    @property
    def is_blob(self) -> bool:
        return self.serial >= 12 and self.serial % 2 == 0

    @property
    def is_text(self) -> bool:
        return self.serial >= 13 and self.serial % 2 == 1


def record_fields(cell: Cell, wanted: int) -> list[Field]:
    """The first ``wanted`` columns of the cell's record, from its header alone.

    The record's header must lie in the local payload and its sizes must add up to the payload's;
    anything else is a ``SqliteError``. A column whose bytes are not all local is still described.
    """
    local = cell.local
    size, pos = varint(local, 0, len(local))
    if size > len(local) or size < 1:
        raise SqliteError("the record header is not within the cell's local payload")
    serials: list[int] = []
    while pos < size:
        serial, pos = varint(local, pos, size)
        serials.append(serial)
    if pos != size:
        raise SqliteError("the record header's serial types overrun its size")
    total = size
    for serial in serials:
        total += serial_size(serial)
    if total != cell.payload:
        raise SqliteError(f"the record's columns add up to {total} bytes, not {cell.payload}")
    fields: list[Field] = []
    at = size
    for serial in serials[:wanted]:
        width = serial_size(serial)
        fields.append(Field(serial, cell.body + at, width, at))
        at += width
    return fields


def int_value(cell: Cell, column: Field) -> int:
    """The integer in an integer column whose bytes are all in the cell's local payload."""
    if column.serial in (8, 9):
        return column.serial - 8
    end = column.at + column.size
    if end > len(cell.local):
        raise SqliteError("an integer column lies outside the cell's local payload")
    raw = cell.local[column.at : end]
    if column.serial in (3, 5):  # 24- and 48-bit big-endian two's complement
        return int.from_bytes(raw, "big", signed=True)
    return int(struct.unpack(_SIGNED[column.serial], raw)[0])


def text_value(cell: Cell, column: Field, limit: int = 1 << 20) -> str | None:
    """A text column whose bytes are all local, or ``None``: not local, too long, not UTF-8."""
    end = column.at + column.size
    if column.size > limit or end > len(cell.local):
        return None
    try:
        return cell.local[column.at : end].decode("utf-8")
    except UnicodeDecodeError:
        return None


@dataclass
class Walk:
    """What one walk of a table b-tree met besides its cells."""

    problems: list[Problem] = field(default_factory=list)
    budget: int = 0


class Database:
    """A database read through a ``SourceReader``; every method reads only what it needs."""

    def __init__(self, source: SourceReader) -> None:
        self.source = source
        self.header = parse_header(source.read(0, HEADER_SIZE), source.size)
        if self.header.encoding != 1:
            raise SqliteError(f"text encoding {self.header.encoding} is not UTF-8")

    def page(self, number: int) -> bytes:
        """Page ``number`` (1-based); a page the file does not hold whole is an error."""
        header = self.header
        if not 1 <= number <= header.pages:
            raise SqliteError(f"page {number} is outside the file's {header.pages} pages")
        data = self.source.read((number - 1) * header.page_size, header.page_size)
        if len(data) != header.page_size:
            raise SqliteError(f"page {number} is cut short")
        return data

    def table(
        self, root: int, low: int = INT64_MIN, high: int = INT64_MAX, walk: Walk | None = None
    ) -> Iterator[Cell]:
        """The rows of the table b-tree at page ``root`` with rowids in ``[low, high]``.

        Rows come in strictly increasing rowid order; a row that is not (a lying key, a repeated
        rowid) is a problem and is skipped, as is a row outside the bounds its parents' keys
        give it, so any range of the table is the same rows however it is cut.
        """
        walk = walk if walk is not None else Walk()
        walk.budget = self.header.pages
        last = [low - 1]
        yield from self._node(root, INT64_MIN - 1, INT64_MAX, low, high, last, walk, 0)

    def _node(
        self,
        number: int,
        above: int,
        upto: int,
        low: int,
        high: int,
        last: list[int],
        walk: Walk,
        depth: int,
    ) -> Iterator[Cell]:
        """Rows of page ``number`` whose rowids lie in ``(above, upto]`` and ``[low, high]``."""
        if depth > MAX_DEPTH:
            walk.problems.append(Problem(number, "too_deep"))
            return
        if walk.budget <= 0:
            walk.problems.append(Problem(number, "page_budget"))
            return
        walk.budget -= 1
        try:
            page = self.page(number)
        except SqliteError:
            walk.problems.append(Problem(number, "unreadable"))
            return
        header = self.header
        usable = header.usable
        base = HEADER_SIZE if number == 1 else 0
        kind = page[base]
        if kind not in (LEAF_TABLE, INTERIOR_TABLE):
            walk.problems.append(Problem(number, "not_table_page"))
            return
        leaf = kind == LEAF_TABLE
        head_end = base + (8 if leaf else 12)
        count = struct.unpack_from(">H", page, base + 3)[0]
        pointers_end = head_end + 2 * count
        if pointers_end > usable:
            walk.problems.append(Problem(number, "cell_count"))
            return
        pointers = struct.unpack_from(f">{count}H", page, head_end)
        if leaf:
            bounds = (above, upto, low, high)
            yield from self._leaf(number, page, pointers, pointers_end, bounds, last, walk)
            return
        right = struct.unpack_from(">I", page, base + 8)[0]
        bound = above
        for pointer in pointers:
            if pointer < pointers_end or pointer + 5 > usable:
                walk.problems.append(Problem(number, "cell_pointer"))
                continue
            child = struct.unpack_from(">I", page, pointer)[0]
            try:
                key, _ = varint(page, pointer + 4, usable)
            except SqliteError:
                walk.problems.append(Problem(number, "cell_pointer"))
                continue
            key = _signed(key)
            if key <= bound or key > upto:
                walk.problems.append(Problem(number, "key_order"))
                continue
            if key >= low and bound < high:
                yield from self._node(child, bound, key, low, high, last, walk, depth + 1)
            bound = key
        if bound < high and upto >= low:
            yield from self._node(right, bound, upto, low, high, last, walk, depth + 1)

    def _leaf(
        self,
        number: int,
        page: bytes,
        pointers: tuple[int, ...],
        pointers_end: int,
        bounds: tuple[int, int, int, int],
        last: list[int],
        walk: Walk,
    ) -> Iterator[Cell]:
        above, upto, low, high = bounds
        usable = self.header.usable
        reach = (number - 1) * self.header.page_size
        for pointer in pointers:
            if pointer < pointers_end or pointer >= usable:
                walk.problems.append(Problem(number, "cell_pointer"))
                continue
            try:
                payload, pos = varint(page, pointer, usable)
                rowid, pos = varint(page, pos, usable)
            except SqliteError:
                walk.problems.append(Problem(number, "cell_pointer"))
                continue
            rowid = _signed(rowid)
            local = _local_size(payload, usable)
            end = pos + local + (4 if local < payload else 0)
            if end > usable:
                walk.problems.append(Problem(number, "cell_overrun"))
                continue
            if rowid < low or rowid > high:
                continue
            if not above < rowid <= upto or rowid <= last[0]:
                # outside the bounds its parents' keys give it, or not after the last row kept
                walk.problems.append(Problem(number, "rowid_order"))
                continue
            last[0] = rowid
            yield Cell(
                rowid,
                reach + pointer,
                end - pointer,
                reach + pos,
                payload,
                page[pos : pos + local],
                number,
            )


def _local_size(payload: int, usable: int) -> int:
    """Bytes of a table-leaf payload stored in the page; the rest is in overflow pages."""
    maximum = usable - 35
    if payload <= maximum:
        return payload
    minimum = ((usable - 12) * 32) // 255 - 23
    local = minimum + (payload - minimum) % (usable - 4)
    return local if local <= maximum else minimum


@dataclass(frozen=True)
class SchemaEntry:
    kind: str
    name: str
    root: int
    sql: str
    cell: Cell  # the sqlite_master row, for citations


def read_schema(db: Database, walk: Walk | None = None) -> list[SchemaEntry]:
    """The ``sqlite_master`` rows that name a table, in rowid order."""
    entries: list[SchemaEntry] = []
    for cell in db.table(1, walk=walk):
        try:
            columns = record_fields(cell, 5)
        except SqliteError:
            continue
        if len(columns) < 5 or not columns[0].is_text or not columns[1].is_text:
            continue
        kind, name = text_value(cell, columns[0]), text_value(cell, columns[1])
        if kind != "table" or name is None or not columns[3].is_int:
            continue
        root = int_value(cell, columns[3])
        sql = text_value(cell, columns[4]) if columns[4].is_text else ""
        entries.append(SchemaEntry(kind, name, root, sql or "", cell))
    return entries


def column_names(sql: str) -> list[str] | None:
    """The column names of a ``CREATE TABLE`` statement, in order; ``None`` if it is not one.

    Handles the statements rosbag2 writes and ordinary hand-written ones: a parenthesised list of
    definitions split at top-level commas, constraint clauses skipped. Quoted names are unquoted.
    """
    open_at = sql.find("(")
    if open_at < 0 or not sql.lstrip().upper().startswith("CREATE TABLE"):
        return None
    depth, start, parts, quote = 0, open_at + 1, [], ""
    for i in range(open_at, len(sql)):
        char = sql[i]
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'`[":
            quote = "]" if char == "[" else char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                parts.append(sql[start:i])
                break
        elif char == "," and depth == 1:
            parts.append(sql[start:i])
            start = i + 1
    names: list[str] = []
    for part in parts:
        words = part.split()
        if not words or words[0].upper() in {"PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT"}:
            continue
        names.append(words[0].strip("\"'`[]"))
    return names


def rowid_alias(sql: str) -> str | None:
    """The column declared ``INTEGER PRIMARY KEY`` (an alias of the rowid), if any."""
    open_at = sql.find("(")
    if open_at < 0:
        return None
    for part in sql[open_at + 1 :].split(","):
        words = part.upper().split()
        if "INTEGER" in words[:2] and "PRIMARY" in words and words[0] not in {"PRIMARY", "UNIQUE"}:
            return part.split()[0].strip("\"'`[]")
    return None
