"""Reading Open-RMF's logs from local files: JSON, JSON Lines and SQLite (ADR 0010 §5).

Every read here is of a file the operator named, below the directory they named, and never
anything else:

- A path is relative, with no ``..``, no empty or absolute part, and no symlink on the way (a
  symlink is not followed: it could point anywhere). Only a regular file is opened, bounded by
  ``max_file_bytes``, and its size is checked on the open descriptor.
- JSON is strict UTF-8 (``documents.parse_json``). A file is one array of objects or JSON Lines
  (one object per line); the map is one object.
- SQLite is untrusted input. It is opened with the standard library, read-only through a URI
  (``mode=ro``), with ``PRAGMA query_only`` and an authoriser that allows ``SELECT`` and reading
  and nothing else (no ``ATTACH``, ``PRAGMA`` or write of any kind). A progress handler bounds the
  work a hostile view or recursive query can do, by instruction count and not by the clock, so a
  run is the same on a slow machine. The table must exist in ``sqlite_master`` under the declared
  name, which is then quoted; it is never spliced into a statement as written.
- A row is an object of column to value as stored: integers, floats, text. A column the operator
  declared as JSON (RMF's api-server stores a task or fleet state as JSON text in ``data``) is
  parsed strictly where it parses, and stays text where it does not. A blob, or a float that JSON
  cannot hold, has no value in a document: the cell is absent (``Unknown``) and the count is a
  finding.
"""

import contextlib
import math
import os
import sqlite3
import stat
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.fleet_ops.documents import DocumentInvalid, parse_json

SQLITE_MAGIC: Final = b"SQLite format 3\x00"
PROGRESS_STEP: Final = 10_000  # VM instructions between progress callbacks
MAX_PROGRESS_CALLS: Final = 20_000  # about 200 million instructions in all
FETCH: Final = 1000


class FileRefused(Exception):
    """A path or file is not one this module reads. ``cause`` is a finding cause; no path text."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


@dataclass
class Rows:
    """Items read from one file or table, and how the read ended."""

    items: list[JsonValue] = field(default_factory=list)
    stopped: str | None = None
    counts: dict[str, int] = field(default_factory=dict)  # finding detail counts, by name


def safe_path(root: str, relative: str) -> str:
    """``relative`` below ``root``, if it is a plain relative path of regular components."""
    if not isinstance(relative, str) or not relative or "\x00" in relative or "\\" in relative:
        raise FileRefused("path_invalid")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in relative.split("/")):
        raise FileRefused("path_invalid")
    current = Path(root)
    for part in pure.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise FileRefused("file_missing") from None
        except OSError:
            raise FileRefused("file_unreadable") from None
        if stat.S_ISLNK(mode):
            raise FileRefused("symlink_refused")
    if not stat.S_ISREG(mode):
        raise FileRefused("not_regular_file")
    return str(current)


def open_regular(path: str, limit: int) -> int:
    """A descriptor for a regular file of at most ``limit`` bytes, opened without following a
    symlink and without waiting on a FIFO: its type and size are checked on the descriptor."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise FileRefused("file_unreadable") from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        os.close(fd)
        raise FileRefused(
            "not_regular_file" if not stat.S_ISREG(info.st_mode) else "file_too_large"
        )
    return fd


def read_bytes(path: str, limit: int) -> bytes:
    """A regular file's bytes, at most ``limit``."""
    with os.fdopen(open_regular(path, limit), "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise FileRefused("file_too_large")
    return data


def json_items(data: bytes, max_rows: int, *, whole: bool = False) -> Rows:
    """The objects of a JSON array, of JSON Lines, or (``whole``) the one object a file is."""
    rows = Rows()
    try:
        parsed: Any = parse_json(data)
        lines = False
    except DocumentInvalid:
        parsed, lines = None, True
    candidates: list[JsonValue]
    if lines:
        candidates = []
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                candidates.append(parse_json(raw))
            except DocumentInvalid:
                rows.stopped = "file_invalid"  # a cut-short last line: what came before is kept
                break
            if len(candidates) > max_rows:
                break
    elif whole:
        candidates = [parsed]
    elif isinstance(parsed, list):
        candidates = list(parsed)
    elif isinstance(parsed, dict):
        candidates = [parsed]  # JSON Lines of one line
    else:
        rows.stopped = "file_invalid"
        return rows
    objects = [item for item in candidates if isinstance(item, dict)]
    if len(objects) != len(candidates):
        rows.counts["not_object"] = len(candidates) - len(objects)
    if len(objects) > max_rows:
        del objects[max_rows:]
        rows.stopped = "row_limit"
    rows.items = list(objects)
    return rows


def _authorizer(action: int, *_args: Any) -> int:
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,  # a view's recursive query: still only reads
    }
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def _value(raw: object, parse: bool, rows: Rows) -> tuple[bool, Any]:
    """``(has a value, the value)`` of one stored cell."""
    if raw is None:
        return True, None
    if isinstance(raw, bytes):
        rows.counts["blob"] = rows.counts.get("blob", 0) + 1
        return False, None
    if isinstance(raw, float) and not math.isfinite(raw):
        rows.counts["non_finite"] = rows.counts.get("non_finite", 0) + 1
        return False, None
    if parse and isinstance(raw, str):
        try:
            return True, parse_json(raw.encode("utf-8", "surrogatepass"))
        except (DocumentInvalid, UnicodeEncodeError):
            rows.counts["json_column_invalid"] = rows.counts.get("json_column_invalid", 0) + 1
    assert isinstance(raw, int | float | str)
    return True, raw


def sqlite_items(
    path: str,
    table: str,
    json_columns: tuple[str, ...],
    max_rows: int,
    max_bytes: int,
    *,
    timeout: float = 5.0,
) -> Rows:
    """The rows of ``table`` in the database at ``path``, read-only (see the module docstring).

    The file is opened and checked by descriptor first (not a symlink, regular, within
    ``max_bytes``, the SQLite magic). SQLite then opens the path itself, as it must for ``mode=ro``,
    so a path swapped in between is not excluded; what is read is discarded unless the file at the
    path afterwards is the very file that was checked (``file_changed``).
    """
    fd = open_regular(path, max_bytes)
    try:
        if os.pread(fd, len(SQLITE_MAGIC), 0) != SQLITE_MAGIC:
            raise FileRefused("not_sqlite")
        checked = os.fstat(fd)
        rows = _query(path, table, json_columns, max_rows, timeout)
        now = Path(path).lstat()
        if (now.st_dev, now.st_ino) != (checked.st_dev, checked.st_ino) or not stat.S_ISREG(
            now.st_mode
        ):
            raise FileRefused("file_changed")
        return rows
    finally:
        os.close(fd)


def _query(
    path: str, table: str, json_columns: tuple[str, ...], max_rows: int, timeout: float
) -> Rows:
    rows = Rows()
    uri = "file:" + urllib.parse.quote(str(Path(path).absolute())) + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=timeout)
    except sqlite3.Error:
        raise FileRefused("sqlite_error") from None
    calls = 0

    def progress() -> int:
        nonlocal calls
        calls += 1
        return 1 if calls > MAX_PROGRESS_CALLS else 0

    try:
        connection.execute("PRAGMA query_only = ON")
        connection.set_authorizer(_authorizer)
        connection.set_progress_handler(progress, PROGRESS_STEP)
        found = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (table,)
        ).fetchone()
        if found is None:
            raise FileRefused("table_missing")
        quoted = '"' + table.replace('"', '""') + '"'
        cursor = connection.execute(f"SELECT * FROM {quoted}")
        names = [column[0] for column in cursor.description]
        parsed = {name: name in json_columns for name in names}
        while True:
            batch = cursor.fetchmany(FETCH)
            if not batch:
                break
            for record in batch:
                if len(rows.items) >= max_rows:
                    rows.stopped = "row_limit"
                    return rows
                item: dict[str, JsonValue] = {}
                for name, raw in zip(names, record, strict=True):
                    has, value = _value(raw, parsed[name], rows)
                    if has:
                        item[name] = value
                rows.items.append(item)
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc):
            rows.stopped = "work_limit"
        else:
            rows.stopped = "sqlite_error"
    except sqlite3.DatabaseError:
        rows.stopped = "file_invalid"
    finally:
        with contextlib.suppress(sqlite3.Error):
            connection.close()
    return rows
