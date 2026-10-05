"""Verifying an ingest package as a stream, in memory bounded whatever its records (ADR 0070).

``verify`` checks everything the package reader always checked (ADR 0022): the manifest, every
file's size and hash, no stray files, every table's order and records, lineage, every series
against its stream, every blob against its source, and a receipt that recomputes from the tables.
It never holds the package: each table is read once, line by line, and each record is checked as
the streaming writer (ADR 0065) checks what it writes, with the same code (``_Gathered``), in the
same table order. The receipt is recomputed as the writer computes it, its large sections sorted
in a ``SpillSpace``, and compared with ``receipt.json`` and ``receipt.md`` by hash, so neither is
parsed. Only when they differ is a small ``receipt.json`` parsed, to say how.

What it returns holds paths and counts, never records: ``PackageRecords`` and ``StoredTable``
read the tables again each time they are iterated, and the receipt is parsed when first asked for.
"""

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Final

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.model.ids import ContentId, RecordId, parse_content_id, parse_record_id
from neptune.model.kinds import RECORD_KINDS, kinds_at, record_key
from neptune.model.package import (
    PackageManifest,
    Storage,
    ingest_receipt_from_json,
    package_manifest_from_json,
)
from neptune.model.record import OLDEST_READABLE_VERSION
from neptune.store.package import (
    _BLOB,
    _DERIVED,
    _SERIES,
    _TABLE,
    MANIFEST,
    RECEIPT,
    RECEIPT_TEXT,
    Content,
    IngestPackage,
    PackageError,
    PackageRecords,
    StoredTable,
    _bytes,
    _derived_line,
    _digest,
    _lines,
    _load,
    _series_settings,
    blob_path,
    derived_path,
    series_path,
    table_path,
)
from neptune.store.receipt import receipt_id
from neptune.store.spill import SPILL_BUDGET, SpillSpace
from neptune.store.writer import _Gathered, _Sink, table_order

# A receipt.json that does not recompute is parsed to say how only up to this size; a larger one
# is refused without being held.
_EXPLAIN_LIMIT: Final = 64 * 1024 * 1024


def verify(
    files: Mapping[str, Content], *, scratch: Path | None = None, budget: int = SPILL_BUDGET
) -> IngestPackage:
    """Verify a package given as its files by path (everything but ``volatile/``), as a stream.

    ``scratch`` is where the recomputed receipt's sections spill (a private directory is made in
    it and removed); without one they are held in memory. Raises ``PackageError`` for anything
    the package contract (ADR 0022) refuses.
    """
    if MANIFEST not in files:
        raise PackageError(f"no {MANIFEST}")
    manifest_bytes = _bytes(files[MANIFEST])
    manifest = package_manifest_from_json(_load(manifest_bytes, MANIFEST))
    listed = {file.path: (file.size, file.sha256) for file in manifest.files}
    present = set(files) - {MANIFEST}
    if present != set(listed):
        raise PackageError(
            f"files do not match the manifest: unlisted {sorted(present - set(listed))},"
            f" missing {sorted(set(listed) - present)}"
        )
    for path, digest in sorted(listed.items()):  # every file, before any is parsed (and tables
        # are hashed again as they are parsed: what is checked is what was hashed)
        if _digest(files[path]) != digest:
            raise PackageError(f"{path} does not match its size and hash in the manifest")
    kinds = kinds_at(manifest.version)
    counted = dict(manifest.tables)
    if counted.keys() != set(kinds):
        raise PackageError(
            f"the manifest must count a table for every record kind of schema version"
            f" {manifest.version}, and no other"
        )
    for kind in kinds:
        if table_path(kind) not in files:
            raise PackageError(f"no table for {kind}: an empty table is an empty file")
    series, blobs, derived = _placed(files)
    for name in (RECEIPT, RECEIPT_TEXT):
        if name not in files:
            raise PackageError(f"no {name}")

    with SpillSpace(scratch, budget) as space:
        settings = _series_settings(manifest.store) if series else None
        gathered = _Gathered(space.sorter, series, settings, OLDEST_READABLE_VERSION)
        for kind in table_order(kinds):
            path = table_path(kind)
            count = _read_table(kind, files[path], listed[path], gathered)
            if count != counted[kind]:
                raise PackageError(f"{path} holds {count} records, the manifest says otherwise")
            gathered.done(kind, count)
        # A package is written at the lowest version that holds its records (ADR 0037 §1, ADR 0061
        # §6), so the same records have one package: a higher version would be a second package.
        if manifest.version != gathered.version:
            raise PackageError(
                f"the manifest says schema version {manifest.version}, but its records are of"
                f" version {gathered.version}: a package is written at the lowest version that"
                f" holds its records"
            )
        for stream in series:
            if stream not in gathered.streams:
                raise PackageError(f"{series_path(stream)} names no stream of this package")
        transforms = set(gathered.transforms)
        lines = {
            kind: _read_derived(kind, data, listed[derived_path(kind)], transforms)
            for kind, data in derived.items()
        }
        _check_sources(manifest, gathered.artifacts, blobs, listed)

        try:
            made, written = gathered.receipt_files(lambda _: _Sink(None, keep=False))
        except ValueError as exc:
            raise PackageError(str(exc)) from exc
        if written[RECEIPT][1] != listed[RECEIPT]:
            raise _receipt_refused(files[RECEIPT], manifest, made)
        if made != manifest.receipt:
            raise PackageError("the manifest names another receipt")
        if written[RECEIPT_TEXT][1] != listed[RECEIPT_TEXT]:
            raise PackageError(f"{RECEIPT_TEXT} is not the rendering of {RECEIPT}")

    def stored(path: str, count: int, parse: Callable[[bytes], Any]) -> StoredTable:
        return StoredTable(path, files[path], listed[path], count, parse)

    records = PackageRecords(
        {
            kind: stored(table_path(kind), counted[kind], _parser(kind, table_path(kind)))
            for kind in kinds
        }
    )
    return IngestPackage(
        id=content_id(manifest_bytes),
        manifest=manifest,
        records=records,
        series=series,
        blobs=blobs,
        derived={
            kind: stored(derived_path(kind), count, _derived_parser(derived_path(kind)))
            for kind, count in sorted(lines.items())
        },
        receipt_document=files[RECEIPT],
    )


def _placed(
    files: Mapping[str, Content],
) -> tuple[dict[RecordId, Content], dict[ContentId, Content], dict[str, Content]]:
    """The series, blobs and derived tables among ``files``; any file with no place is refused."""
    series: dict[RecordId, Content] = {}
    blobs: dict[ContentId, Content] = {}
    derived: dict[str, Content] = {}
    for path, data in sorted(files.items()):
        if path in (MANIFEST, RECEIPT, RECEIPT_TEXT) or _TABLE.fullmatch(path):
            continue
        if match := _DERIVED.fullmatch(path):
            derived[match[1]] = data
        elif match := _SERIES.fullmatch(path):
            series[parse_record_id(f"rec:sha256:{match[1]}")] = data
        elif (match := _BLOB.fullmatch(path)) and match[2].startswith(match[1]):
            blobs[parse_content_id(f"sha256:{match[2]}")] = data
        else:
            raise PackageError(f"{path} has no place in a package")
    return series, blobs, derived


def _hashed(content: Content, path: str, listed: tuple[int, ContentId]) -> Iterator[bytes]:
    """The lines of a file, hashed as they pass: what is checked is what is hashed, even if the
    file changed after the first pass hashed it. ``PackageError`` at the end if they differ from
    the manifest's size and hash."""
    digest, size = hashlib.sha256(), 0
    for line in _lines(content):
        digest.update(line)
        size += len(line)
        yield line
    if (size, "sha256:" + digest.hexdigest()) != listed:
        raise PackageError(f"{path} does not match its size and hash in the manifest")


def _read_table(
    kind: str, content: Content, listed: tuple[int, ContentId], gathered: _Gathered
) -> int:
    """Read one record table as a stream into ``gathered``: one canonical line per record, sorted
    by id, each id once. Returns how many records it holds."""
    path = table_path(kind)
    read = RECORD_KINDS[kind][1]
    previous: str | None = None
    count = 0
    for raw in _hashed(content, path, listed):
        if not raw.endswith(b"\n"):
            raise PackageError(f"{path} is not one canonical line per record")
        record = read(_load(raw[:-1], path))
        data = record.to_json()
        if canonical_json.dumps(data) + b"\n" != raw:
            raise PackageError(f"{path} is not one canonical line per record")
        key = record_key(record)
        if previous is not None and key <= previous:
            raise PackageError(f"{path} must be sorted by id, each id once")
        previous = key
        gathered.add(record, data)
        count += 1
    return count


def _read_derived(
    kind: str, content: Content, listed: tuple[int, ContentId], transforms: set[str]
) -> int:
    """The structure of one derived table, read as a stream: canonical lines, each a ``kind``
    object with an integer ``schema_version``, a record ``id`` and a ``transform`` the package
    holds, sorted by id, each id once. Its meaning is ``neptune.derived``'s to check. Returns how
    many lines it holds."""
    path = derived_path(kind)
    previous: str | None = None
    count = 0
    for raw in _hashed(content, path, listed):
        line = _load(raw.removesuffix(b"\n"), path)
        if not raw.endswith(b"\n") or canonical_json.dumps(line) + b"\n" != raw:
            raise PackageError(f"{path} is not one canonical line per record")
        key = _derived_line(kind, line, transforms)
        if previous is not None and key <= previous:
            raise PackageError(f"{path} must be sorted by id, each id once")
        previous = key
        count += 1
    return count


def _check_sources(
    manifest: PackageManifest,
    artifacts: Mapping[ContentId, int],
    blobs: Mapping[ContentId, Content],
    listed: Mapping[str, tuple[int, ContentId]],
) -> None:
    """The manifest's sources are the package's source artifacts, and a blob is exactly each
    materialised one, holding the bytes it is named for (hashed already, against the manifest)."""
    handles = {handle.content_id: handle for handle in manifest.sources}
    if set(handles) != set(artifacts):
        raise PackageError("the manifest's sources must be the package's source artifacts")
    for content in blobs:
        path = blob_path(content)
        if listed[path][1] != content or content not in handles:
            raise PackageError(f"{path} does not hold the source it is named for")
    for content, handle in handles.items():
        if (handle.storage is Storage.MATERIALISED) != (content in blobs):
            raise PackageError(f"source {content} is {handle.storage}, but its blob says otherwise")


def _receipt_refused(content: Content, manifest: PackageManifest, made: RecordId) -> PackageError:
    """Why ``receipt.json`` is not the receipt the tables recompute to (``made``): parsed to say
    how when it is small enough to hold, refused without being held when it is not."""
    size = next(file.size for file in manifest.files if file.path == RECEIPT)
    if size > _EXPLAIN_LIMIT:
        return PackageError(f"{RECEIPT} is not the receipt of these records ({made})")
    receipt = ingest_receipt_from_json(_load(_bytes(content), RECEIPT))
    if receipt.version != manifest.version:
        return PackageError("the receipt and the manifest are of different schema versions")
    if receipt_id(receipt) != receipt.id:
        return PackageError(f"receipt {receipt.id}: id does not match its content")
    return PackageError(f"receipt {receipt.id} is not the receipt of these records ({made})")


def _reread(line: bytes, path: str) -> Any:
    """A line of a table already verified, parsed again: plain ``json.loads``, since the pass that
    reads it hashes it against the verified bytes (``StoredTable``), so the canonical check need
    not be paid on every pass."""
    try:
        return json.loads(line)
    except ValueError as exc:
        raise PackageError(f"{path} changed since the package was read: {exc}") from exc


def _parser(kind: str, path: str) -> Callable[[bytes], Any]:
    read = RECORD_KINDS[kind][1]

    def parse(line: bytes) -> Any:
        try:
            return read(_reread(line, path))
        except (ValueError, TypeError, KeyError) as exc:  # PackageError is a ValueError
            if isinstance(exc, PackageError):
                raise
            raise PackageError(f"{path} changed since the package was read: {exc}") from exc

    return parse


def _derived_parser(path: str) -> Callable[[bytes], Any]:
    def parse(line: bytes) -> Any:
        return _reread(line, path)

    return parse
