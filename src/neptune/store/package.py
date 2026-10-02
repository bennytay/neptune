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
lineage ids, every series against its stream, and a receipt that recomputes from the tables.

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

import errno
import hashlib
import os
import re
import shutil
import stat
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Final, TypeAlias

from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import check_evidence_record_id, check_transform_record
from neptune.model.ids import ContentId, RecordId, parse_content_id, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.kinds import RECORD_KINDS, kinds_at, package_version, record_key
from neptune.model.package import (
    IngestReceipt,
    PackageFile,
    PackageManifest,
    ReceiptEnvelope,
    SourceHandle,
    Storage,
    ingest_receipt_from_json,
    package_manifest_from_json,
    receipt_envelope_from_json,
)
from neptune.model.provenance import Provenance, TransformRecord
from neptune.store.receipt import build_receipt, check_receipt, render_receipt
from neptune.store.series import SeriesError, check_series, check_settings

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
    """
    series, blobs, store = dict(series or {}), dict(blobs or {}), dict(store or {})
    held: dict[str, list[Any]] = defaultdict(list)
    for record in records:
        kind = getattr(record, "kind", None)
        if not isinstance(kind, str) or kind not in RECORD_KINDS:
            raise PackageError(f"not a record of a known kind: {record!r}")
        held[kind].append(record)
    # The lowest schema version that holds these records: a package that uses no later kind is
    # what a version 1 writer wrote, byte for byte (ADR 0037 §1).
    version = package_version(held)
    tables: dict[str, list[Any]] = {kind: held.get(kind, []) for kind in kinds_at(version)}
    files: dict[str, Content] = {}
    for kind, members in tables.items():
        members.sort(key=record_key)
        keys = [record_key(record) for record in members]
        if len(set(keys)) != len(keys):
            raise PackageError(f"two {kind} records share an id")
        files[table_path(kind)] = b"".join(_document(record) + b"\n" for record in members)
    streams = {stream.id for stream in tables["stream"]}
    if series:
        _series_settings(store)
    for stream, data in series.items():
        if stream not in streams:
            raise PackageError(f"series for {stream}, which is not a stream of this package")
        files[series_path(stream)] = data
    artifacts = {artifact.content_id: artifact for artifact in tables["source_artifact"]}
    for content, data in blobs.items():
        if content not in artifacts:
            raise PackageError(f"blob {content} is not a source artifact of this package")
        if _digest(data) != (artifacts[content].size, content):
            raise PackageError(f"blob bytes do not hash to {content}")
        files[blob_path(content)] = data
    transforms = {transform.id for transform in tables["transform_record"]}
    for kind, lines in sorted((derived or {}).items()):
        files[derived_path(kind)] = _derived_table(kind, lines, transforms)
    receipt = build_receipt((r for members in tables.values() for r in members), version)
    files[RECEIPT] = _document(receipt)
    files[RECEIPT_TEXT] = render_receipt(receipt).encode("utf-8")
    manifest = PackageManifest(
        receipt=receipt.id,
        tables=tuple((kind, len(tables[kind])) for kind in sorted(tables)),
        sources=tuple(
            SourceHandle(
                content,
                artifacts[content].size,
                Storage.MATERIALISED if content in blobs else Storage.REFERENCED,
            )
            for content in sorted(artifacts)
        ),
        files=tuple(PackageFile(path, *_digest(data)) for path, data in sorted(files.items())),
        store=store,
        version=version,
    )
    files[MANIFEST] = _document(manifest)
    read_files(files)  # never hand out a package the reader would refuse
    return files


def _derived_table(kind: str, lines: Iterable[JsonObject], transforms: set[str]) -> bytes:
    """A derived table's bytes, sorted by id, each line checked as the reader checks it.

    ``lines`` may be lazy: each is encoded and checked as it arrives, so only the table's bytes
    are held, never its JSON objects. Lines given in id order (as a grouping gives them) are
    joined as they come; any other order is sorted once, by id.
    """
    path = derived_path(kind)
    if not _DERIVED.fullmatch(path):
        raise PackageError(f"not a derived table kind: {kind!r}")
    encoded: list[tuple[str, bytes]] = []
    ordered = True
    for line in lines:
        key = _derived_line(kind, line, transforms)
        ordered = ordered and (not encoded or encoded[-1][0] < key)
        encoded.append((key, canonical_json.dumps(line) + b"\n"))
    if not ordered:
        encoded.sort(key=lambda entry: entry[0])
        keys = [key for key, _ in encoded]
        if len(set(keys)) != len(keys):
            raise PackageError(f"{path} must name each id once")
    return b"".join(line for _, line in encoded)


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


def _check_derived(kind: str, data: bytes, transforms: set[str]) -> tuple[JsonObject, ...]:
    """The structure of one derived table: canonical lines, each a ``kind`` object with an
    integer ``schema_version``, a record ``id`` and a ``transform`` the package holds, sorted by
    id, each id once. Its meaning is ``neptune.derived``'s to check."""
    path = derived_path(kind)
    lines: list[JsonObject] = []
    previous: str | None = None
    for raw in data.splitlines(keepends=True):
        line = _load(raw.removesuffix(b"\n"), path)
        if not raw.endswith(b"\n") or canonical_json.dumps(line) + b"\n" != raw:
            raise PackageError(f"{path} is not one canonical line per record")
        key = _derived_line(kind, line, transforms)
        if previous is not None and key <= previous:
            raise PackageError(f"{path} must be sorted by id, each id once")
        previous = key
        assert isinstance(line, Mapping)  # _derived_line refuses anything else
        lines.append(line)
    return tuple(lines)


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


def write_package(root: Path, files: Mapping[str, Content]) -> ContentId:
    """Write ``package_contents`` output into ``root``, which must not exist or be empty.

    A path is copied as a stream; ``read_package`` checks the result.
    """
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise PackageError(f"{root} is not an empty directory")
    for relative, data in sorted(files.items()):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            copy_file(data, path)
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


@dataclass(frozen=True)
class IngestPackage:
    """A package read and verified: its id, manifest, receipt, records, series and blobs, and its
    derived tables by kind, each line as JSON (``neptune.derived`` reads them)."""

    id: ContentId
    manifest: PackageManifest
    receipt: IngestReceipt
    records: tuple[Any, ...]
    series: Mapping[RecordId, Content]
    blobs: Mapping[ContentId, Content]
    derived: Mapping[str, tuple[JsonObject, ...]] = field(default_factory=dict)

    def files(self) -> dict[str, Content]:
        """The package's deterministic files, rebuilt from what was read."""
        return package_contents(
            self.records,
            series=self.series,
            blobs=self.blobs,
            store=self.manifest.store,
            derived=self.derived,
        )


def read_package(root: Path) -> IngestPackage:
    """Read a package directory and verify it; ``volatile/`` is left out.

    Series and blobs stay on disk: they are hashed and checked as streams, and the package holds
    their paths.
    """
    files: dict[str, Content] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise PackageError(f"{relative} is a symlink; a package holds regular files only")
        if relative == VOLATILE or relative.startswith(f"{VOLATILE}/") or path.is_dir():
            continue
        large = _SERIES.fullmatch(relative) or _BLOB.fullmatch(relative)
        files[relative] = path if large else _bytes(path)
    return read_files(files)


def read_envelope(root: Path) -> ReceiptEnvelope:
    return receipt_envelope_from_json(canonical_json.loads((root / ENVELOPE).read_bytes()))


def _load(data: bytes, what: str) -> JsonValue:
    try:
        return canonical_json.loads(data)
    except ValueError as exc:
        raise PackageError(f"{what} is not canonical JSON: {exc}") from exc


def read_files(files: Mapping[str, Content]) -> IngestPackage:
    """Verify a package given as its files by path (everything but ``volatile/``)."""
    if MANIFEST not in files:
        raise PackageError(f"no {MANIFEST}")
    manifest_bytes = _bytes(files[MANIFEST])
    manifest = package_manifest_from_json(_load(manifest_bytes, MANIFEST))
    listed = {file.path: file for file in manifest.files}
    present = set(files) - {MANIFEST}
    if present != set(listed):
        raise PackageError(
            f"files do not match the manifest: unlisted {sorted(present - set(listed))},"
            f" missing {sorted(set(listed) - present)}"
        )
    small = {
        path: _bytes(data)
        for path, data in files.items()
        if not (_SERIES.fullmatch(path) or _BLOB.fullmatch(path))
    }
    for path, file in listed.items():
        if _digest(small.get(path, files[path])) != (file.size, file.sha256):
            raise PackageError(f"{path} does not match its size and hash in the manifest")
    kinds = kinds_at(manifest.version)
    if dict(manifest.tables).keys() != set(kinds):
        raise PackageError(
            f"the manifest must count a table for every record kind of schema version"
            f" {manifest.version}, and no other"
        )
    # A package is written at the lowest version that holds its records (ADR 0037 §1), so the same
    # records have one package: a higher version would be a second package of them.
    held = package_version(kind for kind, count in manifest.tables if count)
    if manifest.version != held:
        raise PackageError(
            f"the manifest says schema version {manifest.version}, but its records are of version"
            f" {held}: a package is written at the lowest version that holds its records"
        )

    records: list[Any] = []
    for kind in kinds:
        read = RECORD_KINDS[kind][1]
        path = table_path(kind)
        if path not in small:
            raise PackageError(f"no table for {kind}: an empty table is an empty file")
        members = [read(_load(line, path)) for line in small[path].splitlines()]
        if small[path] != b"".join(_document(record) + b"\n" for record in members):
            raise PackageError(f"{path} is not one canonical line per record")
        keys = [record_key(record) for record in members]
        if keys != sorted(set(keys)):
            raise PackageError(f"{path} must be sorted by id, each id once")
        if len(members) != dict(manifest.tables)[kind]:
            raise PackageError(f"{path} holds {len(members)} records, the manifest says otherwise")
        records.extend(members)
    _check_lineage(records)

    series: dict[RecordId, Content] = {}
    blobs: dict[ContentId, Content] = {}
    derived: dict[str, tuple[JsonObject, ...]] = {}
    transforms = {record.id for record in records if record.kind == "transform_record"}
    streams = {record.id: record for record in records if record.kind == "stream"}
    handles = {handle.content_id: handle for handle in manifest.sources}
    artifacts = {record.content_id for record in records if record.kind == "source_artifact"}
    if set(handles) != artifacts:
        raise PackageError("the manifest's sources must be the package's source artifacts")
    settings: JsonObject | None = None  # validated once, at the first series file
    for path, data in files.items():
        if path in (MANIFEST, RECEIPT, RECEIPT_TEXT) or _TABLE.fullmatch(path):
            continue
        if match := _DERIVED.fullmatch(path):
            derived[match[1]] = _check_derived(match[1], small[path], transforms)
        elif match := _SERIES.fullmatch(path):
            stream = parse_record_id(f"rec:sha256:{match[1]}")
            if stream not in streams:
                raise PackageError(f"{path} names no stream of this package")
            if settings is None:
                settings = _series_settings(manifest.store)
            try:
                check_series(streams[stream], data, settings)
            except SeriesError as exc:
                raise PackageError(f"{path}: {exc}") from exc
            series[stream] = data
        elif (match := _BLOB.fullmatch(path)) and match[2].startswith(match[1]):
            content = parse_content_id(f"sha256:{match[2]}")
            if _digest(data)[1] != content or handles.get(content) is None:
                raise PackageError(f"{path} does not hold the source it is named for")
            blobs[content] = data
        else:
            raise PackageError(f"{path} has no place in a package")
    for content, handle in handles.items():
        if (handle.storage is Storage.MATERIALISED) != (content in blobs):
            raise PackageError(f"source {content} is {handle.storage}, but its blob says otherwise")

    receipt = ingest_receipt_from_json(_load(small[RECEIPT], RECEIPT))
    if receipt.version != manifest.version:
        raise PackageError("the receipt and the manifest are of different schema versions")
    try:
        check_receipt(receipt, records)
    except ValueError as exc:
        raise PackageError(str(exc)) from exc
    if receipt.id != manifest.receipt:
        raise PackageError("the manifest names another receipt")
    if small[RECEIPT_TEXT] != render_receipt(receipt).encode("utf-8"):
        raise PackageError(f"{RECEIPT_TEXT} is not the rendering of {RECEIPT}")
    return IngestPackage(
        id=content_id(manifest_bytes),
        manifest=manifest,
        receipt=receipt,
        records=tuple(records),
        series=series,
        blobs=blobs,
        derived=derived,
    )


def _check_lineage(records: list[Any]) -> None:
    """Every transform, finding and evidence record id recomputes (ADRs 0016, 0017)."""
    transforms: dict[str, TransformRecord] = {}
    by_kind: dict[str, list[Any]] = defaultdict(list)
    for record in records:
        by_kind[record.kind].append(record)
    try:
        for transform in by_kind["transform_record"]:
            transforms[transform.id] = check_transform_record(transform)
        for finding in by_kind["ingest_finding"]:
            check_ingest_finding(finding)
            if finding.transform not in transforms:
                raise PackageError(f"finding {finding.id} names a transform not in the package")
        for record in records:
            provenance = getattr(record, "provenance", None)
            if isinstance(provenance, Provenance):
                if provenance.transform not in transforms:
                    raise PackageError(f"{record.kind} {record.id}: its transform is missing")
                check_evidence_record_id(record, transforms[provenance.transform])
    except PackageError:
        raise
    except ValueError as exc:
        raise PackageError(str(exc)) from exc
