"""The ingest package on disk: record tables, series, blobs, a receipt and a manifest (ADR 0022).

A package is a directory::

    manifest.json                    PackageManifest; the sha256 of these bytes is the package id
    receipt.json                     IngestReceipt: the receipt's deterministic core
    receipt.md                       the same core, rendered for people
    records/<kind>.jsonl             one table per record kind of the package's schema version,
                                     sorted by id; empty file = none
    derived/<kind>.jsonl             a derived (inferred) table, sorted by id; absent = not made
    series/<64 hex>.parquet          one per stream, named by the stream's id (MVL-16 writes them)
    blobs/sha256/<2 hex>/<64 hex>    a materialised source's bytes
    volatile/receipt-envelope.json   ReceiptEnvelope: job, wall clock, host, root; not listed
    volatile/cache-report.json       what the job reused and recomputed, and why (ADR 0031)

``package_contents`` computes every deterministic file from the records: the same records,
series and blobs always give the same bytes and so the same package id. ``package_files`` is the
same for a package held wholly in memory. ``read_package`` checks all of
it: the manifest, every file's size and hash, no stray files, every table's order and records,
lineage ids, every series against its stream, and a receipt that recomputes from the tables. It
reads as a stream (``neptune.store.reader``, ADR 0070) and gives a package that holds paths: its
records are read from their tables each time they are iterated. ``write_package`` writes into
``.<name>.partial`` beside the target and renames it into place, so a killed write leaves the
target as it was.

Derived tables (ADR 0036, amending ADR 0023 §5) hold what a producer inferred, such as session
proposals, apart from the evidence in ``records/``. The store checks their structure only, since
it never imports ``neptune.derived``: canonical lines, each an object of the table's ``kind`` with
an integer ``schema_version``, a record ``id`` (sorted, each once) and a ``transform`` the package
holds. ``neptune.derived`` reads their meaning. A table present and empty means its producer ran
and inferred nothing; a table absent means it did not run.

A file's content is bytes or a path on disk. Series and blobs can be gigabytes, so they stay
paths: hashed, checked and copied as streams, never held in memory (ADR 0025). Every path is
opened with ``open_file``: a symlink is refused, not followed, and so is a FIFO or device.
"""

import contextlib
import errno
import fcntl
import hashlib
import io
import os
import re
import shutil
import stat
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from itertools import zip_longest
from pathlib import Path
from typing import Any, BinaryIO, Final, TypeAlias, TypeGuard

from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import check_evidence_record_id
from neptune.model.ids import ContentId, RecordId, parse_content_id, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.package import (
    IngestReceipt,
    PackageManifest,
    ReceiptEnvelope,
    ingest_receipt_from_json,
    package_manifest_from_json,
    receipt_envelope_from_json,
)
from neptune.model.provenance import Provenance, TransformRecord
from neptune.store.series import SeriesError, check_settings
from neptune.store.spill import SPILL_BUDGET

MANIFEST: Final = "manifest.json"
RECEIPT: Final = "receipt.json"
RECEIPT_TEXT: Final = "receipt.md"
VOLATILE: Final = "volatile"
ENVELOPE: Final = f"{VOLATILE}/receipt-envelope.json"
CACHE_REPORT: Final = f"{VOLATILE}/cache-report.json"
_SERIES: Final = re.compile(r"series/([0-9a-f]{64})\.parquet")
_BLOB: Final = re.compile(r"blobs/sha256/([0-9a-f]{2})/([0-9a-f]{64})")
_TABLE: Final = re.compile(r"records/([a-z][a-z0-9_]*)\.jsonl")
_DERIVED: Final = re.compile(r"derived/([a-z][a-z0-9_]*)\.jsonl")
# A package bound for ``root`` is written in ``.<name>.partial`` beside it, then renamed (ADR 0070).
PARTIAL_SUFFIX: Final = ".partial"


# A package file's content: bytes in memory, or a file on disk read as a stream.
Content: TypeAlias = bytes | Path
_READ_SIZE: Final = 1024 * 1024


class PackageError(ValueError):
    """A package, or what was given to write one, breaks the package contract (ADR 0022)."""


def table_path(kind: str) -> str:
    return f"records/{kind}.jsonl"


def derived_path(kind: str) -> str:
    return f"derived/{kind}.jsonl"


def series_path(stream: RecordId) -> str:
    return f"series/{parse_record_id(stream).removeprefix('rec:sha256:')}.parquet"


def blob_path(content: ContentId) -> str:
    digest = parse_content_id(content).removeprefix("sha256:")
    return f"blobs/sha256/{digest[:2]}/{digest}"


def _document(value: Any) -> bytes:
    return canonical_json.dumps(value.to_json())


def open_file(path: Path) -> BinaryIO:
    """Open a regular file to read, never through a symlink and never a special file.

    A symlink at ``path`` is refused, not followed (``O_NOFOLLOW``), and a FIFO or device is
    refused without waiting on it (``O_NONBLOCK``); either is a ``PackageError``.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PackageError(f"{path} is a symlink; the store never follows one") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PackageError(f"{path} is not a regular file")
        os.set_blocking(descriptor, True)
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def copy_file(path: Path, target: Path) -> None:
    """Copy ``path``, opened with ``open_file``, to a new file ``target``, as a stream."""
    with open_file(path) as data, target.open("xb") as copy:
        shutil.copyfileobj(data, copy, _READ_SIZE)


def _digest(content: Content) -> tuple[int, ContentId]:
    """Size and sha256 of ``content``, streamed from disk for a path."""
    if isinstance(content, bytes):
        return len(content), content_id(content)
    digest, size = hashlib.sha256(), 0
    with open_file(content) as stream:
        while block := stream.read(_READ_SIZE):
            digest.update(block)
            size += len(block)
    return size, ContentId("sha256:" + digest.hexdigest())


def _bytes(content: Content) -> bytes:
    if isinstance(content, bytes):
        return content
    with open_file(content) as stream:
        return stream.read()


# --- Writing -----------------------------------------------------------------------------------


def package_contents(
    records: Iterable[Any],
    *,
    series: Mapping[RecordId, Content] | None = None,
    blobs: Mapping[ContentId, Content] | None = None,
    store: JsonObject | None = None,
    derived: Mapping[str, Iterable[JsonObject]] | None = None,
) -> dict[str, Content]:
    """Every file of the package holding ``records``, by path: deterministic, manifest included.

    ``records`` are the source ledger, the transforms, the evidence records and the findings.
    ``series`` are the stream files the store wrote (``neptune.store.series``), by stream id; a
    stream may have none, as in a records-only package. ``blobs`` are the sources to materialise,
    by content id; every other source stays referenced (ADR 0022 §5). ``store`` holds the settings
    the store wrote them with. Series and blobs may be paths, which are never loaded.
    ``derived`` are derived tables by kind, each its lines as JSON objects, in any order.

    It is ``PackageWriter`` (``neptune.store.writer``) with nothing spilled: every table is held
    and given as bytes. ``write_package_stream`` writes the same bytes in bounded memory. Every
    check the package reader makes is made as the files are computed, so the reader never refuses
    what this gives.
    """
    from neptune.store.writer import PackageWriter  # the writer builds on this module

    with PackageWriter() as writer:
        writer.extend(records)
        return writer.finish(series=series, blobs=blobs, store=store, derived=derived)


def _derived_key(kind: str, line: JsonValue) -> str:
    if not isinstance(line, Mapping) or not isinstance(line.get("id"), str):
        raise PackageError(f"a {kind} line must be a JSON object with a record id")
    return str(line["id"])


def _derived_line(kind: str, line: JsonValue, transforms: set[str]) -> str:
    """One derived line's structure (``_check_derived``); its id."""
    path = derived_path(kind)
    if not isinstance(line, Mapping) or line.get("kind") != kind:
        raise PackageError(f"{path} holds a line that is not a {kind}")
    version = line.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise PackageError(f"{path} holds a line without a derived schema_version")
    key = _derived_key(kind, line)
    try:
        parse_record_id(key)
    except ValueError as exc:
        raise PackageError(f"{path}: {exc}") from exc
    transform = line.get("transform")
    if not isinstance(transform, str) or transform not in transforms:
        raise PackageError(f"{path} holds a line whose transform is not in the package")
    return key


def _series_settings(store: JsonObject) -> JsonObject:
    """The settings the package's series were written with, as ``store.series`` records them."""
    try:
        return check_settings(store.get("series"))
    except SeriesError as exc:
        raise PackageError(
            f"series need the settings they were written with, under store.series: {exc}"
        ) from exc


def package_files(
    records: Iterable[Any],
    *,
    series: Mapping[RecordId, bytes] | None = None,
    blobs: Mapping[ContentId, bytes] | None = None,
    store: JsonObject | None = None,
    derived: Mapping[str, Iterable[JsonObject]] | None = None,
) -> dict[str, bytes]:
    """``package_contents`` for a package held in memory: every file as bytes."""
    contents = package_contents(records, series=series, blobs=blobs, store=store, derived=derived)
    return {path: _bytes(data) for path, data in contents.items()}


def package_id(files: Mapping[str, Content]) -> ContentId:
    """The package's identity: the sha256 of its manifest's bytes (ADR 0002 §5)."""
    return content_id(_bytes(files[MANIFEST]))


def partial_path(root: Path) -> Path:
    """Where a package bound for ``root`` is written before it is renamed into place."""
    return root.parent / f".{root.name}{PARTIAL_SUFFIX}"


def _claim(partial: Path, root: Path) -> int:
    """Lock the directory at ``partial`` for one write of ``root``, made if missing.

    One left by a write that was killed (no process holds its lock) is emptied and reused, so a
    crash leaves at most one such directory per root, and the next write of that root removes it.
    A write of the same root in progress elsewhere holds the lock: this one is refused.
    """
    while True:
        with contextlib.suppress(FileExistsError):
            partial.mkdir()
        try:
            descriptor = os.open(partial, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue  # renamed into place or removed since: make it again
        except OSError as exc:
            if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                raise
            partial.unlink()  # a stale file or link where the directory goes: never followed
            continue
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            here = partial.lstat()
        except BlockingIOError as exc:
            os.close(descriptor)
            raise PackageError(f"{root} is being written by another process") from exc
        except FileNotFoundError:
            os.close(descriptor)
            continue
        locked = os.fstat(descriptor)
        if (here.st_dev, here.st_ino) != (locked.st_dev, locked.st_ino):
            os.close(descriptor)  # the write that held it renamed it into place: not ours
            continue
        try:
            for entry in partial.iterdir():  # what a killed write left
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor


@contextlib.contextmanager
def replacing(root: Path) -> Iterator[Path]:
    """A directory to write the package bound for ``root`` into, renamed to ``root`` when the
    block ends without error and removed when it does not (ADR 0070).

    ``root`` must not exist or be an empty directory. The directory is ``partial_path(root)``, in
    the same parent, so the rename is one atomic step: a process killed at any point leaves
    ``root`` as it was, never a manifest without the files it lists. It is atomic, not durable:
    ``neptune.store.assemble.publish`` also flushes to disk (ADR 0026).
    """
    if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
        raise PackageError(f"{root} is not an empty directory")
    root.parent.mkdir(parents=True, exist_ok=True)
    partial = partial_path(root)
    descriptor = _claim(partial, root)
    try:
        yield partial
        try:
            partial.rename(root)  # replaces an empty directory at ``root`` in the same step
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR, errno.EISDIR):
                raise PackageError(f"{root} is not an empty directory") from exc
            raise
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    finally:
        os.close(descriptor)


def lay_down(directory: Path, files: Mapping[str, Content]) -> None:
    """Write ``files`` into ``directory``: bytes as given, paths copied as streams, except a path
    already where it belongs (a streaming writer's own table)."""
    for relative, data in sorted(files.items()):
        target = directory / relative
        if isinstance(data, bytes):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        elif data != target:
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_file(data, target)


def write_package(root: Path, files: Mapping[str, Content]) -> ContentId:
    """Write ``package_contents`` output into ``root``, which must not exist or be empty.

    A path is copied as a stream; ``read_package`` checks the result. The package is written
    beside ``root`` and renamed into place (``replacing``): ``root`` holds all of it or none.
    """
    with replacing(root) as partial:
        lay_down(partial, files)
    return package_id(files)


def write_envelope(root: Path, envelope: ReceiptEnvelope) -> None:
    """Add the volatile envelope; it must accompany this package's receipt."""
    manifest = package_manifest_from_json(canonical_json.loads((root / MANIFEST).read_bytes()))
    if envelope.receipt != manifest.receipt:
        raise PackageError(f"envelope is for receipt {envelope.receipt}, not {manifest.receipt}")
    path = root / ENVELOPE
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(_document(envelope))


def write_cache_report(root: Path, report: JsonObject) -> None:
    """Add the runtime's cache report (ADR 0031 §5); like the envelope, it names its receipt.

    The report is the runtime's document (``neptune.runtime.cache``); the store only checks
    that it accompanies this package's receipt and writes it, canonical, beside the envelope.
    """
    manifest = package_manifest_from_json(canonical_json.loads((root / MANIFEST).read_bytes()))
    if report.get("receipt") != manifest.receipt:
        raise PackageError(f"cache report is for receipt {report.get('receipt')!r}, not this one")
    path = root / CACHE_REPORT
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(canonical_json.dumps(report))


def read_cache_report(root: Path) -> JsonObject:
    """The cache report a job left in a package, as canonical JSON; ``PackageError`` if none."""
    try:
        data = canonical_json.loads((root / CACHE_REPORT).read_bytes())
    except (OSError, ValueError) as exc:
        raise PackageError(f"{CACHE_REPORT} cannot be read: {exc}") from exc
    if not isinstance(data, dict):
        raise PackageError(f"{CACHE_REPORT} is not a JSON object")
    return data


# --- Reading -----------------------------------------------------------------------------------


def _lines(content: Content) -> Iterator[bytes]:
    """A table's lines as stored, each with its newline (a last line may lack one), streamed."""
    if isinstance(content, bytes):
        yield from io.BytesIO(content)  # split at b"\n" only, as a file is
        return
    with open_file(content) as stream:
        yield from stream


class StoredTable(Collection[Any]):
    """One table of a verified package, read from its file each time it is iterated, never held.

    Each pass is hashed as it goes: a file changed since the package was verified is refused
    (``PackageError``) when the pass reaches its end, and one gone or unreadable as soon as the
    pass meets it. A pass raises nothing else.
    """

    def __init__(
        self,
        path: str,
        content: Content,
        listed: tuple[int, ContentId],
        count: int,
        parse: Callable[[bytes], Any],
    ) -> None:
        self.path = path
        self._content = content
        self._listed = listed
        self._count = count
        self._parse = parse

    def __iter__(self) -> Iterator[Any]:
        digest, size = hashlib.sha256(), 0
        try:
            for line in _lines(self._content):
                digest.update(line)
                size += len(line)
                yield self._parse(line)
        except OSError as exc:
            raise PackageError(f"{self.path} cannot be read since the package was: {exc}") from exc
        if (size, "sha256:" + digest.hexdigest()) != self._listed:
            raise PackageError(f"{self.path} changed since the package was read")

    def __len__(self) -> int:
        return self._count

    def __contains__(self, item: object) -> bool:
        return any(item == member for member in self)

    def __eq__(self, other: object) -> bool:
        if not _comparable(other):
            return NotImplemented
        return _same(self, other)


def _comparable(other: object) -> TypeGuard[Collection[Any]]:
    return isinstance(other, Collection) and not isinstance(other, str | bytes | Mapping)


def _same(lazy: Collection[Any], other: Collection[Any]) -> bool:
    """Equal members in the same order, compared as both are read: neither is held whole. Equal
    by content, which is not held, so neither view is hashable."""
    if len(lazy) != len(other):
        return False
    end = object()
    return all(a == b for a, b in zip_longest(lazy, other, fillvalue=end))


class PackageRecords(Collection[Any]):
    """A verified package's records, table by table (each sorted by id), read from the tables each
    time they are iterated and never held whole (ADR 0070). ``of(kind)`` reads one table."""

    def __init__(self, tables: Mapping[str, StoredTable]) -> None:
        self._tables = dict(tables)

    def of(self, kind: str) -> Collection[Any]:
        """The records of one kind, read from their table; none for a kind the package lacks."""
        return self._tables.get(kind, ())

    def __iter__(self) -> Iterator[Any]:
        for table in self._tables.values():
            yield from table

    def __len__(self) -> int:
        return sum(len(table) for table in self._tables.values())

    def __contains__(self, item: object) -> bool:
        kind = getattr(item, "kind", None)
        return isinstance(kind, str) and item in self.of(kind)

    def __eq__(self, other: object) -> bool:
        if not _comparable(other):
            return NotImplemented
        return _same(self, other)


def records_of(records: Iterable[Any], kind: str) -> Collection[Any]:
    """The records of one kind: a package's table as it is read, or picked from any records."""
    if isinstance(records, PackageRecords):
        return records.of(kind)
    return [record for record in records if record.kind == kind]


@dataclass(frozen=True)
class IngestPackage:
    """A package read and verified: its id, manifest, receipt, records, series and blobs, and its
    derived tables by kind, each line as JSON (``neptune.derived`` reads them).

    As ``read_package`` gives it, nothing that grows with the records is held (ADR 0070): the
    records (``PackageRecords``) and each derived table (``StoredTable``) are read from their files
    each time they are iterated, series and blobs are paths, and the receipt is parsed from
    ``receipt.json`` when first asked for. ``records`` may be any collection of records, as a
    caller that builds a package in memory gives it.
    """

    id: ContentId
    manifest: PackageManifest
    records: Collection[Any]
    series: Mapping[RecordId, Content]
    blobs: Mapping[ContentId, Content]
    derived: Mapping[str, Collection[JsonObject]] = field(default_factory=dict)
    receipt_document: Content = field(default=b"", repr=False, compare=False)

    @cached_property
    def receipt(self) -> IngestReceipt:
        """The receipt core, parsed from ``receipt.json`` when first asked for: it lists every
        finding, so it is held only by a caller that asks."""
        data = _bytes(self.receipt_document)
        listed = {file.path: (file.size, file.sha256) for file in self.manifest.files}
        if _digest(data) != listed.get(RECEIPT):
            raise PackageError(f"{RECEIPT} changed since the package was read")
        return ingest_receipt_from_json(_load(data, RECEIPT))

    def files(self) -> dict[str, Content]:
        """The package's deterministic files, rebuilt from what was read."""
        return package_contents(
            self.records,
            series=self.series,
            blobs=self.blobs,
            store=self.manifest.store,
            derived=self.derived,
        )


def read_package(
    root: Path, *, scratch: Path | None = None, budget: int = SPILL_BUDGET
) -> IngestPackage:
    """Read a package directory and verify it; ``volatile/`` is left out.

    Every file stays on disk: hashed, checked and read as a stream, and the package holds paths
    (ADR 0070). ``scratch`` is a directory of the caller's (a workspace's scratch space) where the
    receipt's large sections are sorted as it is recomputed; without one they are sorted in
    memory, which grows with the findings and ambiguous fields, never with the records.
    """
    files: dict[str, Content] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise PackageError(f"{relative} is a symlink; a package holds regular files only")
        if relative == VOLATILE or relative.startswith(f"{VOLATILE}/") or path.is_dir():
            continue
        files[relative] = path
    return read_files(files, scratch=scratch, budget=budget)


def read_envelope(root: Path) -> ReceiptEnvelope:
    return receipt_envelope_from_json(canonical_json.loads((root / ENVELOPE).read_bytes()))


def _load(data: bytes, what: str) -> JsonValue:
    try:
        return canonical_json.loads(data)
    except ValueError as exc:
        raise PackageError(f"{what} is not canonical JSON: {exc}") from exc


def read_files(
    files: Mapping[str, Content], *, scratch: Path | None = None, budget: int = SPILL_BUDGET
) -> IngestPackage:
    """Verify a package given as its files by path (everything but ``volatile/``).

    It is ``neptune.store.reader.verify``: each table is read once as a stream and checked as the
    writer checks what it writes, and the receipt is recomputed and compared by hash (ADR 0070).
    """
    from neptune.store.reader import verify  # the reader builds on this module

    return verify(files, scratch=scratch, budget=budget)


def check_record_lineage(
    record: Any,
    transforms: Mapping[str, TransformRecord],
    *,
    finding: bool = True,
    provenance: bool = True,
) -> None:
    """One record's lineage, against the package's ``transforms``: a finding's id recomputes and
    names a transform of the package, and an evidence record's transform is there and its id
    recomputes under it (ADRs 0016, 0017). The streaming writer checks each record with it."""
    try:
        if finding and record.kind == "ingest_finding":
            check_ingest_finding(record)
            if record.transform not in transforms:
                raise PackageError(f"finding {record.id} names a transform not in the package")
        cited = getattr(record, "provenance", None)
        if provenance and isinstance(cited, Provenance):
            if cited.transform not in transforms:
                raise PackageError(f"{record.kind} {record.id}: its transform is missing")
            check_evidence_record_id(record, transforms[cited.transform])
    except PackageError:
        raise
    except ValueError as exc:
        raise PackageError(str(exc)) from exc
