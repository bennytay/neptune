"""The ingest package on disk: record tables, series, blobs, a receipt and a manifest (ADR 0022).

A package is a directory::

    manifest.json                    PackageManifest; the sha256 of these bytes is the package id
    receipt.json                     IngestReceipt: the receipt's deterministic core
    receipt.md                       the same core, rendered for people
    records/<kind>.jsonl             one table per record kind, sorted by id; empty file = none
    series/<64 hex>.parquet          one per stream, named by the stream's id (MVL-16 writes them)
    blobs/sha256/<2 hex>/<64 hex>    a materialised source's bytes
    volatile/receipt-envelope.json   ReceiptEnvelope: job, wall clock, host, root; not listed

``package_files`` computes every deterministic file from the records: the same records, series
and blobs always give the same bytes and so the same package id. ``read_package`` checks all of
it: the manifest, every file's size and hash, no stray files, every table's order and records,
lineage ids, and a receipt that recomputes from the tables.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import check_evidence_record_id, check_transform_record
from neptune.model.ids import ContentId, RecordId, parse_content_id, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.kinds import RECORD_KINDS, record_key
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

MANIFEST: Final = "manifest.json"
RECEIPT: Final = "receipt.json"
RECEIPT_TEXT: Final = "receipt.md"
VOLATILE: Final = "volatile"
ENVELOPE: Final = f"{VOLATILE}/receipt-envelope.json"
_SERIES: Final = re.compile(r"series/([0-9a-f]{64})\.parquet")
_BLOB: Final = re.compile(r"blobs/sha256/([0-9a-f]{2})/([0-9a-f]{64})")
_TABLE: Final = re.compile(r"records/([a-z][a-z0-9_]*)\.jsonl")


class PackageError(ValueError):
    """A package, or what was given to write one, breaks the package contract (ADR 0022)."""


def table_path(kind: str) -> str:
    return f"records/{kind}.jsonl"


def series_path(stream: RecordId) -> str:
    return f"series/{parse_record_id(stream).removeprefix('rec:sha256:')}.parquet"


def blob_path(content: ContentId) -> str:
    digest = parse_content_id(content).removeprefix("sha256:")
    return f"blobs/sha256/{digest[:2]}/{digest}"


def _document(value: Any) -> bytes:
    return canonical_json.dumps(value.to_json())


# --- Writing -----------------------------------------------------------------------------------


def package_files(
    records: Iterable[Any],
    *,
    series: Mapping[RecordId, bytes] | None = None,
    blobs: Mapping[ContentId, bytes] | None = None,
    store: JsonObject | None = None,
) -> dict[str, bytes]:
    """Every file of the package holding ``records``, by path: deterministic, manifest included.

    ``records`` are the source ledger, the transforms, the evidence records and the findings.
    ``series`` are the stream files the store wrote, by stream id. ``blobs`` are the sources to
    materialise, by content id; every other source stays referenced (ADR 0022 §5). ``store`` holds
    the settings the store wrote them with.
    """
    series, blobs, store = dict(series or {}), dict(blobs or {}), dict(store or {})
    tables: dict[str, list[Any]] = {kind: [] for kind in RECORD_KINDS}
    for record in records:
        kind = getattr(record, "kind", None)
        if kind not in tables:
            raise PackageError(f"not a record of a known kind: {record!r}")
        tables[kind].append(record)
    files: dict[str, bytes] = {}
    for kind, members in tables.items():
        members.sort(key=record_key)
        keys = [record_key(record) for record in members]
        if len(set(keys)) != len(keys):
            raise PackageError(f"two {kind} records share an id")
        files[table_path(kind)] = b"".join(_document(record) + b"\n" for record in members)
    streams = {stream.id for stream in tables["stream"]}
    for stream, data in series.items():
        if stream not in streams:
            raise PackageError(f"series for {stream}, which is not a stream of this package")
        files[series_path(stream)] = data
    artifacts = {artifact.content_id: artifact for artifact in tables["source_artifact"]}
    for content, data in blobs.items():
        if content not in artifacts:
            raise PackageError(f"blob {content} is not a source artifact of this package")
        if content_id(data) != content or len(data) != artifacts[content].size:
            raise PackageError(f"blob bytes do not hash to {content}")
        files[blob_path(content)] = data
    receipt = build_receipt(record for members in tables.values() for record in members)
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
        files=tuple(
            PackageFile(path, len(data), content_id(data)) for path, data in sorted(files.items())
        ),
        store=store,
    )
    files[MANIFEST] = _document(manifest)
    read_files(files)  # never hand out a package the reader would refuse
    return files


def package_id(files: Mapping[str, bytes]) -> ContentId:
    """The package's identity: the sha256 of its manifest's bytes (ADR 0002 §5)."""
    return content_id(files[MANIFEST])


def write_package(root: Path, files: Mapping[str, bytes]) -> ContentId:
    """Write ``package_files`` output into ``root``, which must not exist or be empty."""
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise PackageError(f"{root} is not an empty directory")
    for relative, data in sorted(files.items()):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return package_id(files)


def write_envelope(root: Path, envelope: ReceiptEnvelope) -> None:
    """Add the volatile envelope; it must accompany this package's receipt."""
    manifest = package_manifest_from_json(canonical_json.loads((root / MANIFEST).read_bytes()))
    if envelope.receipt != manifest.receipt:
        raise PackageError(f"envelope is for receipt {envelope.receipt}, not {manifest.receipt}")
    path = root / ENVELOPE
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(_document(envelope))


# --- Reading -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestPackage:
    """A package read and verified: its id, manifest, receipt, records, series and blobs."""

    id: ContentId
    manifest: PackageManifest
    receipt: IngestReceipt
    records: tuple[Any, ...]
    series: Mapping[RecordId, bytes]
    blobs: Mapping[ContentId, bytes]

    def files(self) -> dict[str, bytes]:
        """The package's deterministic files, rebuilt from what was read."""
        return package_files(
            self.records, series=self.series, blobs=self.blobs, store=self.manifest.store
        )


def read_package(root: Path) -> IngestPackage:
    """Read a package directory and verify it; ``volatile/`` is left out."""
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise PackageError(f"{relative} is a symlink; a package holds regular files only")
        if relative == VOLATILE or relative.startswith(f"{VOLATILE}/") or path.is_dir():
            continue
        files[relative] = path.read_bytes()
    return read_files(files)


def read_envelope(root: Path) -> ReceiptEnvelope:
    return receipt_envelope_from_json(canonical_json.loads((root / ENVELOPE).read_bytes()))


def _load(data: bytes, what: str) -> JsonValue:
    try:
        return canonical_json.loads(data)
    except ValueError as exc:
        raise PackageError(f"{what} is not canonical JSON: {exc}") from exc


def read_files(files: Mapping[str, bytes]) -> IngestPackage:
    """Verify a package given as its files by path (everything but ``volatile/``)."""
    if MANIFEST not in files:
        raise PackageError(f"no {MANIFEST}")
    manifest = package_manifest_from_json(_load(files[MANIFEST], MANIFEST))
    listed = {file.path: file for file in manifest.files}
    present = set(files) - {MANIFEST}
    if present != set(listed):
        raise PackageError(
            f"files do not match the manifest: unlisted {sorted(present - set(listed))},"
            f" missing {sorted(set(listed) - present)}"
        )
    for path, file in listed.items():
        if len(files[path]) != file.size or content_id(files[path]) != file.sha256:
            raise PackageError(f"{path} does not match its size and hash in the manifest")
    if dict(manifest.tables).keys() != RECORD_KINDS.keys():
        raise PackageError("the manifest must count a table for every record kind")

    records: list[Any] = []
    for kind, (_, read) in RECORD_KINDS.items():
        path = table_path(kind)
        if path not in files:
            raise PackageError(f"no table for {kind}: an empty table is an empty file")
        members = [read(_load(line, path)) for line in files[path].splitlines()]
        if files[path] != b"".join(_document(record) + b"\n" for record in members):
            raise PackageError(f"{path} is not one canonical line per record")
        keys = [record_key(record) for record in members]
        if keys != sorted(set(keys)):
            raise PackageError(f"{path} must be sorted by id, each id once")
        if len(members) != dict(manifest.tables)[kind]:
            raise PackageError(f"{path} holds {len(members)} records, the manifest says otherwise")
        records.extend(members)
    _check_lineage(records)

    series: dict[RecordId, bytes] = {}
    blobs: dict[ContentId, bytes] = {}
    streams = {record.id for record in records if record.kind == "stream"}
    handles = {handle.content_id: handle for handle in manifest.sources}
    artifacts = {record.content_id for record in records if record.kind == "source_artifact"}
    if set(handles) != artifacts:
        raise PackageError("the manifest's sources must be the package's source artifacts")
    for path, data in files.items():
        if path in (MANIFEST, RECEIPT, RECEIPT_TEXT) or _TABLE.fullmatch(path):
            continue
        if match := _SERIES.fullmatch(path):
            stream = parse_record_id(f"rec:sha256:{match[1]}")
            if stream not in streams:
                raise PackageError(f"{path} names no stream of this package")
            series[stream] = data
        elif (match := _BLOB.fullmatch(path)) and match[2].startswith(match[1]):
            content = parse_content_id(f"sha256:{match[2]}")
            if content_id(data) != content or handles.get(content) is None:
                raise PackageError(f"{path} does not hold the source it is named for")
            blobs[content] = data
        else:
            raise PackageError(f"{path} has no place in a package")
    for content, handle in handles.items():
        if (handle.storage is Storage.MATERIALISED) != (content in blobs):
            raise PackageError(f"source {content} is {handle.storage}, but its blob says otherwise")

    receipt = ingest_receipt_from_json(_load(files[RECEIPT], RECEIPT))
    try:
        check_receipt(receipt, records)
    except ValueError as exc:
        raise PackageError(str(exc)) from exc
    if receipt.id != manifest.receipt:
        raise PackageError("the manifest names another receipt")
    if files[RECEIPT_TEXT] != render_receipt(receipt).encode("utf-8"):
        raise PackageError(f"{RECEIPT_TEXT} is not the rendering of {RECEIPT}")
    return IngestPackage(
        id=content_id(files[MANIFEST]),
        manifest=manifest,
        receipt=receipt,
        records=tuple(records),
        series=series,
        blobs=blobs,
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
