"""The series catalog: which Parquet file holds a stream's rows in a package (ADR 0013 §4).

A ``SeriesFile`` is one registered stream's series file where its package lies, with what a
reader needs to open it correctly: the stream's clocks in ``time/<i>`` order, the store settings
the package's manifest records, the file's size and sha256 from that manifest, and the order the
compiler guarantees (clock 0 ascending, unknown last, then ``seq``; root ADR 0018 §7).

``SeriesCatalog`` resolves ``(package id, stream id)`` pairs, or a thread's stream entries, from
the catalog and the packages' manifests. It never copies a file, and it reports every pair it
cannot serve as a ``CatalogFinding`` rather than raising: a package that moved, a manifest whose
bytes no longer hash to the package id, a stream without a series file, a file whose size
differs from its manifest, one stream id with two different series files.
"""

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any, Final

import psycopg
from psycopg import sql

from neptune.identity import canonical_json
from neptune.model.ids import parse_record_id
from neptune.model.jsonvalue import JsonObject
from neptune.model.package import PackageManifest, package_manifest_from_json
from neptune.store.package import MANIFEST, series_path, table_path
from neptune.store.series import SeriesError, check_settings
from neptune_ledger.api.types import CatalogFinding, Thread
from neptune_ledger.catalog.migrate import tenant_schema
from neptune_ledger.lake.store import GLOB_CHARACTERS, Location, ObjectStore, local_store

Conn = psycopg.Connection[tuple[Any, ...]]
Locate = Callable[[str, str], ObjectStore]

# What a series file is ordered by, whatever its stream (root ADR 0018 §7, ADR 0025).
SORTED_BY: Final = ("time/0", "seq")
# The largest manifest or stream table the catalog reads whole; anything larger is unreadable.
MANIFEST_LIMIT: Final = 64 * 1024 * 1024
STREAM_TABLE_LIMIT: Final = 256 * 1024 * 1024
_CONTENT_ID: Final = re.compile(r"sha256:[0-9a-f]{64}")

_STREAMS: Final = """
SELECT r.package_id, r.record_id, r.line, r.body_digest, r.body -> 'clocks', p.tx_seq,
       p.root_locator
  FROM record r
  JOIN package p ON p.tenant_id = r.tenant_id AND p.package_id = r.package_id
 WHERE r.tenant_id = %(tenant)s AND r.kind = 'stream'
   AND (r.package_id, r.record_id) IN (
       SELECT * FROM unnest(%(packages)s::text[], %(streams)s::text[]))
"""


@dataclass(frozen=True)
class SeriesFile:
    """One stream's series file in one package, read in place (ADR 0013 §4).

    ``clocks`` are the stream's ``TimestampDomain`` ids; ``clocks[i]`` is column ``time/<i>``.
    ``settings`` are the manifest's ``store.series``. ``registration_key`` is the package's
    ``tx_seq``. ``also_in`` names later packages that hold the same stream with the same file
    bytes, which a read leaves out (one stream id, one series: ADR 0005 §2).
    """

    package_id: str
    stream_id: str
    registration_key: int
    location: Location
    size: int
    sha256: str
    clocks: tuple[str, ...]
    settings: JsonObject
    also_in: tuple[str, ...] = ()

    def time_column(self, clock: str) -> str | None:
        """The column holding ticks on ``clock``, or None if the stream does not carry it."""
        return f"time/{self.clocks.index(clock)}" if clock in self.clocks else None


@dataclass(frozen=True)
class SeriesSelection:
    """The files a read scans, one per stream id, and why any asked-for pair is not among them."""

    files: tuple[SeriesFile, ...]
    findings: tuple[CatalogFinding, ...]


@dataclass(frozen=True)
class _Row:
    package_id: str
    stream_id: str
    line: int
    body_digest: str
    clocks: Any
    tx_seq: int
    root_locator: str


class SeriesCatalog:
    """Series files of registered streams for one tenant (ADR 0013 §4).

    ``locate(package_id, root_locator)`` gives the store a package's objects are read from; by
    default, the local directory it was registered from. A deployment that mirrors packages to an
    object store passes its own (ADR 0013 §2).
    """

    def __init__(self, conninfo: str, tenant_id: str, *, locate: Locate = local_store) -> None:
        self._conninfo = conninfo
        self._tenant = tenant_id
        self._schema = tenant_schema(tenant_id)
        self._locate = locate
        self._conn: Conn | None = None

    def __enter__(self) -> "SeriesCatalog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connection(self) -> Conn:
        if self._conn is None or self._conn.closed:
            conn: Conn = psycopg.connect(self._conninfo, autocommit=True)
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self._schema)))
            self._conn = conn
        return self._conn

    def of_thread(self, thread: Thread) -> SeriesSelection:
        """The series files of every stream entry of ``thread``, in every package it names.

        A history holds lineage siblings (one stream under two adapter versions) as two entries,
        each with its own clocks, so a read keeps them in separate partitions; a current view
        holds one of them.
        """
        pairs = [
            (package, entry.record_id)
            for partition in thread.partitions
            for entry in partition.entries
            if entry.kind == "stream"
            for package in entry.packages
        ]
        return self.files(pairs)

    def files(self, pairs: Iterable[tuple[str, str]]) -> SeriesSelection:
        """The series files of these ``(package id, stream id)`` pairs, in the order given."""
        asked = list(dict.fromkeys(pairs))
        findings: list[CatalogFinding] = []
        valid = []
        for package_id, stream_id in asked:
            if _is_id(package_id, stream_id):
                valid.append((package_id, stream_id))
            else:
                detail = "not a (package id, stream record id) pair"
                findings.append(CatalogFinding("invalid_request", str(stream_id)[:200], detail))
        rows = self._rows(valid)
        manifests: dict[str, PackageManifest | CatalogFinding] = {}
        found: list[SeriesFile] = []
        for pair in valid:
            row = rows.get(pair)
            if row is None:
                detail = f"no stream {pair[1]} is registered in package {pair[0]}"
                findings.append(CatalogFinding("unknown_record", pair[1], detail))
                continue
            made = self._file(row, manifests)
            if isinstance(made, CatalogFinding):
                findings.append(made)
            else:
                found.append(made)
        kept, conflicts = _one_per_stream(found)
        return SeriesSelection(tuple(kept), tuple(findings + conflicts))

    def _rows(self, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], _Row]:
        if not pairs:
            return {}
        params = {
            "tenant": self._tenant,
            "packages": [p for p, _ in pairs],
            "streams": [s for _, s in pairs],
        }
        with self._connection().transaction():
            fetched = self._connection().execute(_STREAMS, params).fetchall()
        return {
            (str(p), str(s)): _Row(str(p), str(s), int(line), str(digest), clocks, int(seq), root)
            for p, s, line, digest, clocks, seq, root in fetched
        }

    def _file(
        self, row: _Row, manifests: dict[str, PackageManifest | CatalogFinding]
    ) -> SeriesFile | CatalogFinding:
        store = self._locate(row.package_id, row.root_locator)
        if row.package_id not in manifests:
            manifests[row.package_id] = _manifest(store, row.package_id)
        manifest = manifests[row.package_id]
        if isinstance(manifest, CatalogFinding):
            return manifest
        key = series_path(parse_record_id(row.stream_id))
        listed = next((f for f in manifest.files if f.path == key), None)
        if listed is None:
            detail = f"package {row.package_id} holds no series file for this stream"
            return CatalogFinding("file_missing", row.stream_id, detail)
        try:
            settings = check_settings(manifest.store.get("series"))
        except SeriesError as exc:
            return CatalogFinding("manifest_invalid", row.package_id, f"store.series: {exc}")
        clocks = row.clocks if row.clocks is not None else _clocks_from_package(store, row)
        if isinstance(clocks, CatalogFinding):
            return clocks
        if not _is_clock_list(clocks):
            detail = "the stream's clocks are not a list of record ids"
            return CatalogFinding("record_invalid", row.stream_id, detail)
        location = store.location(key)
        if GLOB_CHARACTERS & set(location.url):
            detail = f"{store.describe()}: query engines read * ? [ ] {{ }} in a path as a glob"
            return CatalogFinding("unsafe_entry", row.package_id, detail)
        size = store.size(key)
        if size is None:
            detail = f"{key} is missing from {store.describe()}, or not a plain file"
            return CatalogFinding("file_missing", row.stream_id, detail)
        if size != listed.size:
            detail = f"{key} holds {size} bytes; the package manifest says {listed.size}"
            return CatalogFinding("file_digest_mismatch", row.stream_id, detail)
        return SeriesFile(
            package_id=row.package_id,
            stream_id=row.stream_id,
            registration_key=row.tx_seq,
            location=location,
            size=listed.size,
            sha256=listed.sha256,
            clocks=tuple(clocks),
            settings=settings,
        )


def _is_record_id(value: object) -> bool:
    try:
        parse_record_id(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return True


def _is_id(package_id: object, stream_id: object) -> bool:
    content = isinstance(package_id, str) and _CONTENT_ID.fullmatch(package_id) is not None
    return content and _is_record_id(stream_id)


def _is_clock_list(clocks: object) -> bool:
    return isinstance(clocks, list) and bool(clocks) and all(map(_is_record_id, clocks))


def _manifest(store: ObjectStore, package_id: str) -> PackageManifest | CatalogFinding:
    """The package's manifest, read through ``store``, if its bytes still hash to the id."""
    data = store.read(MANIFEST, MANIFEST_LIMIT)
    if data is None:
        detail = f"no readable {MANIFEST} at {store.describe()}"
        return CatalogFinding("package_unreadable", package_id, detail)
    if "sha256:" + hashlib.sha256(data).hexdigest() != package_id:
        detail = f"{MANIFEST} at {store.describe()} no longer hashes to the package id"
        return CatalogFinding("manifest_digest_mismatch", package_id, detail)
    try:
        return package_manifest_from_json(canonical_json.loads(data))
    except (TypeError, ValueError) as exc:  # a registered package's manifest parses; be safe
        return CatalogFinding("manifest_invalid", package_id, str(exc).splitlines()[0][:300])


def _clocks_from_package(store: ObjectStore, row: _Row) -> Any:
    """The stream's clocks from its line in the package, when the catalog holds no body for it
    (a string holding U+0000, which ``jsonb`` cannot store: ADR 0009 §1)."""
    data = store.read(table_path("stream"), STREAM_TABLE_LIMIT)
    lines = data.split(b"\n") if data is not None else []
    line = lines[row.line - 1] if 0 < row.line <= len(lines) else None
    if line is None or "sha256:" + hashlib.sha256(line).hexdigest() != row.body_digest:
        detail = f"the stream's line in {store.describe()} is missing or changed"
        return CatalogFinding("package_unreadable", row.package_id, detail)
    try:
        return json.loads(line).get("clocks")
    except (ValueError, AttributeError):
        return None


def _one_per_stream(
    found: list[SeriesFile],
) -> tuple[list[SeriesFile], list[CatalogFinding]]:
    """One file per stream id: the first registration's, naming the others in ``also_in``.

    A stream id is one record body (ADR 0005 §2), and the compiler writes its series from the
    same bytes, so two packages holding it hold the same file. Two different files for one id
    contradict that: neither is read.
    """
    by_stream: dict[str, list[SeriesFile]] = {}
    for file in found:
        by_stream.setdefault(file.stream_id, []).append(file)
    kept: list[SeriesFile] = []
    conflicts: list[CatalogFinding] = []
    for stream_id, files in by_stream.items():
        files.sort(key=lambda f: (f.registration_key, f.package_id))
        if len({f.sha256 for f in files}) > 1:
            packages = ", ".join(f.package_id for f in files)
            detail = f"packages {packages} hold different series files for this stream"
            conflicts.append(CatalogFinding("conflicting_id", stream_id, detail))
            continue
        first = files[0]
        also = tuple(f.package_id for f in files[1:])
        kept.append(replace(first, also_in=also) if also else first)
    return kept, conflicts
