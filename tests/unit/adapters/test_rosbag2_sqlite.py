"""The SQLite reader against the standard library's own: same rows, same types, and no raise on
damage. The standard library writes the databases and reads them back as the oracle."""

import random
import sqlite3
import struct
from contextlib import suppress
from itertools import pairwise
from pathlib import Path
from typing import Final

import pytest

from neptune.adapters.rosbag2._sqlite import (
    HEADER_SIZE,
    MAGIC,
    Database,
    SqliteError,
    Walk,
    column_names,
    int_value,
    parse_header,
    read_schema,
    record_fields,
    rowid_alias,
    text_value,
    varint,
)
from neptune.discovery.reader import BytesReader

TIMESTAMPS: Final = (
    0, 1, -1, 127, -128, 128, 32767, 32768, 8388607, 8388608, 2**31 - 1, 2**31, -(2**31) - 1,
    2**47 - 1, 2**47, 2**63 - 1, -(2**63),
)  # fmt: skip


def build(
    path: Path,
    count: int = 600,
    page_size: int = 4096,
    sizes: tuple[int, ...] = (0, 10, 400, 5000, 20000),
    seed: int = 1,
    stamps: tuple[int, ...] = (),
) -> bytes:
    rng = random.Random(seed)
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA page_size = {page_size}")
    db.execute(
        "CREATE TABLE topics(id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,"
        " serialization_format TEXT NOT NULL, offered_qos_profiles TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL,"
        " timestamp INTEGER NOT NULL, data BLOB NOT NULL)"
    )
    db.execute("CREATE INDEX timestamp_idx ON messages (timestamp ASC)")
    db.execute("INSERT INTO topics VALUES (1, '/a', 'pkg/msg/A', 'cdr', '')")
    for index in range(count):
        stamp = stamps[index] if index < len(stamps) else 10**9 + index * 1000
        db.execute(
            "INSERT INTO messages(topic_id, timestamp, data) VALUES (?, ?, ?)",
            (1 + index % 2, stamp, rng.randbytes(rng.choice(sizes))),
        )
    db.commit()
    db.close()
    return path.read_bytes()


def table_root(db: Database, name: str) -> int:
    return next(entry.root for entry in read_schema(db) if entry.name == name)


def oracle(path: Path) -> list[tuple[int, int, int, int]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT id, topic_id, timestamp, length(data) FROM messages ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize("page_size", [512, 1024, 4096, 65536])
def test_rows_match_the_standard_library_at_every_page_size(tmp_path: Path, page_size: int) -> None:
    data = build(tmp_path / "t.db3", page_size=page_size, count=500)
    db = Database(BytesReader(data))
    assert db.header.page_size == page_size
    walk = Walk()
    rows = []
    for cell in db.table(table_root(db, "messages"), walk=walk):
        topic, stamp, blob = record_fields(cell, 4)[1:]
        assert cell.page >= 2 and cell.offset >= (cell.page - 1) * page_size
        rows.append((cell.rowid, int_value(cell, topic), int_value(cell, stamp), blob.size))
    assert rows == oracle(tmp_path / "t.db3")
    assert walk.problems == []


def test_every_integer_width_reads_back_exactly(tmp_path: Path) -> None:
    data = build(tmp_path / "t.db3", count=len(TIMESTAMPS), stamps=TIMESTAMPS, sizes=(3,))
    db = Database(BytesReader(data))
    stamps = [
        int_value(cell, record_fields(cell, 4)[2]) for cell in db.table(table_root(db, "messages"))
    ]
    assert stamps == list(TIMESTAMPS)


def test_a_range_is_the_same_rows_however_it_is_cut(tmp_path: Path) -> None:
    data = build(tmp_path / "t.db3", count=900, sizes=(100, 3000))
    db = Database(BytesReader(data))
    root = table_root(db, "messages")
    everything = [cell.rowid for cell in db.table(root)]
    assert everything == list(range(1, 901))
    for low, high in [(1, 1), (5, 17), (100, 899), (450, 450), (900, 900), (901, 2000), (-5, 3)]:
        assert [c.rowid for c in db.table(root, low, high)] == [
            r for r in everything if low <= r <= high
        ]
    cuts = [1, 77, 300, 301, 650, 900]
    pieces = [r for a, b in pairwise(cuts) for r in db.table(root, a, b - 1)]
    assert [c.rowid for c in pieces] == list(range(1, 900))


def test_the_schema_is_read_with_its_columns(tmp_path: Path) -> None:
    db = Database(BytesReader(build(tmp_path / "t.db3", count=3)))
    schema = {entry.name: entry for entry in read_schema(db)}
    assert set(schema) == {"topics", "messages"}  # indexes are not tables
    assert column_names(schema["messages"].sql) == ["id", "topic_id", "timestamp", "data"]
    assert rowid_alias(schema["messages"].sql) == "id"
    topic = next(db.table(schema["topics"].root))
    fields = record_fields(topic, 5)
    assert fields[0].is_null
    assert text_value(topic, fields[1]) == "/a"
    assert text_value(topic, fields[2]) == "pkg/msg/A"


def test_column_names_survive_constraints_quotes_and_nested_parentheses() -> None:
    sql = (
        'CREATE TABLE "t"(`a` INTEGER PRIMARY KEY, [b c] TEXT DEFAULT (1, 2), "d" REAL,'
        " PRIMARY KEY (a), UNIQUE (b, d), CHECK (a > (1)))"
    )
    # A name with a space is cut at the space: rosbag2 writes none, and SQL needs quoting anyway.
    assert column_names(sql) == ["a", "b", "d"]
    assert column_names("CREATE INDEX i ON t(a)") is None
    assert column_names("garbage") is None
    assert rowid_alias("CREATE TABLE t(a TEXT, b INTEGER PRIMARY KEY)") == "b"
    assert rowid_alias("CREATE TABLE t(a INTEGER, PRIMARY KEY (a))") is None


def test_varints_cover_one_to_nine_bytes() -> None:
    for value in (0, 1, 127, 128, 16383, 16384, 2**56 - 1, 2**56, 2**64 - 1):
        encoded = _encode(value)
        assert varint(encoded, 0, len(encoded)) == (value, len(encoded))
    with pytest.raises(SqliteError):
        varint(b"\x80\x80", 0, 2)


def _encode(value: int) -> bytes:
    if value >= 1 << 56:
        return bytes([(value >> (8 + 7 * (7 - i))) & 0x7F | 0x80 for i in range(8)]) + bytes(
            [value & 0xFF]
        )
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    return bytes(reversed(out))


# --- Hostile databases -------------------------------------------------------------------------


def test_headers_that_are_not_databases_are_refused_not_crashed() -> None:
    good = bytearray(MAGIC + struct.pack(">H", 4096) + bytes(100 - len(MAGIC) - 2))
    good[20] = 0
    with pytest.raises(SqliteError):
        parse_header(b"", 0)
    with pytest.raises(SqliteError):
        parse_header(b"x" * 200, 200)
    for page_size in (0, 3, 100, 511, 1000):
        broken = bytearray(good)
        struct.pack_into(">H", broken, 16, page_size)
        with pytest.raises(SqliteError):
            parse_header(bytes(broken), 8192)
    reserved = bytearray(good)
    reserved[20] = 255
    struct.pack_into(">H", reserved, 16, 512)
    with pytest.raises(SqliteError):
        parse_header(bytes(reserved), 8192)
    with pytest.raises(SqliteError):
        parse_header(bytes(good), 100)  # shorter than one page
    assert parse_header(bytes(good), 8192).pages == 2


def test_utf16_databases_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "u.db3"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA encoding = 'UTF-16le'")
    conn.execute("CREATE TABLE t(a)")
    conn.commit()
    conn.close()
    with pytest.raises(SqliteError, match="UTF-8"):
        Database(BytesReader(path.read_bytes()))


def test_a_truncated_file_gives_the_rows_before_the_cut_and_never_raises(tmp_path: Path) -> None:
    data = build(tmp_path / "t.db3", count=400)
    full = [r[0] for r in oracle(tmp_path / "t.db3")]
    size = len(data)
    kept_any = False
    for cut in sorted({4096 * k for k in range(1, size // 4096)} | {size - 1, size - 2000, 5000}):
        short = data[:cut]
        try:
            db = Database(BytesReader(short))
        except SqliteError:
            continue
        walk = Walk()
        try:
            root = table_root(db, "messages")
        except StopIteration:
            continue
        rows = [c.rowid for c in db.table(root, walk=walk)]
        assert rows == sorted(set(rows))
        assert set(rows) <= set(full)
        kept_any = kept_any or bool(rows)
        if len(rows) < len(full):
            assert walk.problems, cut
    assert kept_any


def test_random_damage_never_raises_and_keeps_rowids_strictly_increasing(tmp_path: Path) -> None:
    data = build(tmp_path / "t.db3", count=300, page_size=1024, sizes=(50, 1500))
    rng = random.Random(7)
    for _ in range(150):
        broken = bytearray(data)
        for _ in range(rng.choice([1, 3, 20])):
            broken[rng.randrange(len(broken))] = rng.randrange(256)
        try:
            db = Database(BytesReader(bytes(broken)))
            schema = read_schema(db, Walk())
        except SqliteError:
            continue
        roots = [e.root for e in schema if e.name == "messages"]
        for root in roots:
            previous = None
            for cell in db.table(root, walk=Walk()):
                assert previous is None or cell.rowid > previous
                previous = cell.rowid
                with suppress(SqliteError):
                    record_fields(cell, 4)


def test_a_page_that_points_at_itself_ends_the_walk_with_a_problem(tmp_path: Path) -> None:
    data = bytearray(build(tmp_path / "t.db3", count=2000, page_size=1024, sizes=(300,)))
    db = Database(BytesReader(bytes(data)))
    root = table_root(db, "messages")
    page = (root - 1) * 1024
    assert data[page] == 5  # the root is an interior page
    first_cell = struct.unpack_from(">H", data, page + 12)[0]
    struct.pack_into(">I", data, page + first_cell, root)  # its first child is itself
    walk = Walk()
    db = Database(BytesReader(bytes(data)))
    rows = list(db.table(root, walk=walk))
    assert len(rows) < 2000
    assert {p.reason for p in walk.problems} & {"too_deep", "page_budget", "key_order"}


def test_a_lying_cell_count_is_a_problem_not_an_allocation(tmp_path: Path) -> None:
    data = bytearray(build(tmp_path / "t.db3", count=100, page_size=512, sizes=(10,)))
    db = Database(BytesReader(bytes(data)))
    root = table_root(db, "messages")
    leaf = next(iter(db.table(root))).page
    struct.pack_into(">H", data, (leaf - 1) * 512 + 3, 0xFFFF)
    walk = Walk()
    list(Database(BytesReader(bytes(data))).table(root, walk=walk))
    assert any(p.reason == "cell_count" and p.page == leaf for p in walk.problems)


def test_header_constants() -> None:
    assert len(MAGIC) == 16 and HEADER_SIZE == 100
