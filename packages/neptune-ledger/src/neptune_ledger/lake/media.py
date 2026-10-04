"""The Lance media store and lazy hydration of evidence references (ADR 0014 §5-§7).

``hydrate(evidence_ref, variant)`` returns a handle at once; nothing is resolved, read or decoded
until ``read()``. Then:

- a ``bytes`` hydration resolves the reference and returns the step-0 span of the source as a
  ``SourceSlice``: ranged, chunk-verified reads of the cited bytes where they lie, so a client
  slices a video without the Ledger reading the rest of it. Nothing is stored; the evidence is
  never copied.
- any other variant is an **artefact**: a derivative extracted from the verified bytes by a
  decoder (``decode``), stored once per tenant in the Lance table
  ``tenant_<id>/tables/media/`` (ADR 0013 §2) with its evidence reference and its extraction
  transform as columns and its bytes as an out-of-line Lance blob. Its id is a hash of the
  evidence reference, the variant and the transform, so the same citation under the same
  decoder and library versions is extracted once and served from the table after; another
  library version is another artefact.

Each write is a Lance version. An ``Artefact`` names the snapshot it was read at, and reading the
store at that snapshot returns the same row for ever (unless the table's old versions are
cleaned up by an operator). Stored values carry no wall clock and no randomness, so two
hydrations of one reference give byte-identical artefacts and rows.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal

import pyarrow as pa

from neptune.identity import canonical_json
from neptune_ledger.api.types import EvidenceAnchor
from neptune_ledger.catalog.migrate import tenant_schema
from neptune_ledger.lake.decode import (
    DEFAULT_LIMITS,
    VARIANTS,
    DecodeFailure,
    ExtractionTransform,
    Limits,
    Variant,
    check_variant,
    cited_bytes,
    decode,
    transform_for,
)
from neptune_ledger.lake.evidence import (
    EvidenceBytes,
    EvidenceResolver,
    MediaFinding,
    ReadFailure,
    SourceReader,
    catalog_findings,
    parse_locator,
)

MEDIA_TABLE: Final = "media"
# Lance file format, pinned: a newer default would change the table's files under a pylance bump.
DATA_STORAGE_VERSION: Final = "2.2"
_ARTEFACT_ID: Final = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT_ATTEMPTS: Final = 8

SCHEMA: Final = pa.schema(
    [
        pa.field("artefact_id", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("locator", pa.string(), nullable=False),
        pa.field("variant", pa.string(), nullable=False),
        pa.field("transform_id", pa.string(), nullable=False),
        pa.field("transform", pa.string(), nullable=False),
        pa.field("media_type", pa.string(), nullable=False),
        pa.field("size", pa.int64(), nullable=False),
        pa.field("sha256", pa.string(), nullable=False),
        pa.field("metadata", pa.string(), nullable=False),
    ]
)
_COLUMNS: Final = tuple(SCHEMA.names)


def _schema() -> Any:
    import lance

    # Every blob out of line, in its own file or a pack file, never inline in the row data.
    return SCHEMA.append(lance.blob_field("data", nullable=False, inline_size_threshold=0))


def artefact_id(anchor: EvidenceAnchor, variant: str, transform: ExtractionTransform) -> str:
    """``sha256`` of the canonical JSON of the evidence reference, variant and transform id."""
    key = {
        "evidence": {"locator": list(anchor.locator), "source": anchor.source},
        "transform": transform.id,
        "variant": variant,
    }
    return "sha256:" + hashlib.sha256(canonical_json.dumps(key)).hexdigest()  # type: ignore[arg-type]


@dataclass(frozen=True)
class Artefact:
    """One stored derivative: what it was extracted from and by what, and its bytes, lazily.

    ``snapshot`` is the media table version it was read at. ``open`` returns the Lance blob as a
    seekable file; ``read_range`` reads part of it without the rest.
    """

    artefact_id: str
    evidence_ref: EvidenceAnchor
    variant: str
    transform: dict[str, Any]
    transform_id: str
    media_type: str
    size: int
    sha256: str
    metadata: dict[str, Any]
    snapshot: int
    _open: Callable[[], BinaryIO] = field(compare=False, repr=False)

    def open(self) -> BinaryIO:
        return self._open()

    def read(self) -> bytes:
        with self.open() as blob:
            return blob.read()

    def read_range(self, offset: int, length: int) -> bytes:
        with self.open() as blob:
            blob.seek(offset)
            return blob.read(length)


@dataclass(frozen=True)
class SourceSlice:
    """The ``bytes`` variant: the bytes every byte_range step of the locator addresses.

    When no step needed decompression they are read lazily from the source, verified per
    chunk (``reader``). When one did (a record inside a compressed MCAP chunk, a member of a
    compressed archive) they are the decoded bytes (``data``), and ``inflated`` names each
    decompression applied, so they are never mistaken for stored bytes.
    """

    evidence: EvidenceBytes
    reader: SourceReader | None
    data: bytes | None = None
    inflated: tuple[str, ...] = ()

    @property
    def size(self) -> int:
        return self.reader.size if self.reader is not None else len(self.data or b"")

    def read_range(self, offset: int, length: int) -> bytes | MediaFinding:
        if self.reader is not None:
            return self.reader.read_range(offset, length)
        if offset < 0 or length < 0 or offset + length > self.size:
            detail = f"[{offset!r}, +{length!r}) is outside the {self.size} cited bytes"
            return MediaFinding("invalid_request", self.evidence.evidence_ref.source, detail)
        return (self.data or b"")[offset : offset + length]

    def read(self) -> bytes | MediaFinding:
        return self.read_range(0, self.size)


@dataclass(frozen=True)
class Hydrated:
    """What ``read`` gives: the artefact or slice, or the findings that stopped it."""

    value: Artefact | SourceSlice | None
    findings: tuple[MediaFinding, ...]


class MediaStore:
    """One tenant's Lance media table under a deployment's Ledger-owned store (ADR 0013 §2).

    ``root`` is a local directory or an object-store URI Lance reads (``s3://bucket/prefix``,
    with ``storage_options``). The table is created on its first write.
    """

    def __init__(
        self,
        root: str | Path,
        tenant_id: str,
        *,
        storage_options: dict[str, str] | None = None,
    ) -> None:
        base = str(root).rstrip("/")
        self.uri = f"{base}/{tenant_schema(tenant_id)}/tables/{MEDIA_TABLE}"
        self._options = storage_options

    def _dataset(self, version: int | None = None) -> Any | None:
        import lance

        try:
            return lance.dataset(self.uri, version=version, storage_options=self._options)
        except (FileNotFoundError, ValueError, OSError):
            return None

    def snapshot(self) -> int | None:
        """The latest table version, or None before the first artefact is stored."""
        dataset = self._dataset()
        return None if dataset is None else int(dataset.version)

    def get(self, artefact: str, *, snapshot: int | None = None) -> Artefact | None:
        """The artefact as stored at ``snapshot`` (default: latest), or None if absent there."""
        if not _ARTEFACT_ID.fullmatch(artefact):
            return None
        dataset = self._dataset(snapshot)
        if dataset is None:
            return None
        rows = dataset.to_table(
            columns=list(_COLUMNS),
            filter=f"artefact_id = '{artefact}'",
            with_row_address=True,
        ).to_pylist()
        if not rows:
            return None
        row = min(rows, key=lambda r: r["_rowaddr"])  # concurrent writers may store it twice
        version = int(dataset.version)
        address = int(row["_rowaddr"])

        def blob() -> BinaryIO:
            pinned = self._dataset(version)
            assert pinned is not None
            (found,) = pinned.take_blobs("data", addresses=[address])
            return found  # type: ignore[no-any-return]

        locator = canonical_json.loads(row["locator"].encode("utf-8"))
        assert isinstance(locator, list)
        return Artefact(
            artefact_id=row["artefact_id"],
            evidence_ref=EvidenceAnchor(row["source"], tuple(locator)),
            variant=row["variant"],
            transform=canonical_json.loads(row["transform"].encode("utf-8")),  # type: ignore[arg-type]
            transform_id=row["transform_id"],
            media_type=row["media_type"],
            size=int(row["size"]),
            sha256=row["sha256"],
            metadata=canonical_json.loads(row["metadata"].encode("utf-8")),  # type: ignore[arg-type]
            snapshot=version,
            _open=blob,
        )

    def put(self, row: dict[str, Any], data: bytes) -> None:
        """Store one artefact unless its id is already stored (Lance merge-insert)."""
        import lance

        table = pa.table(
            {**{name: [row[name]] for name in _COLUMNS}, "data": lance.blob_array([data])},
            schema=_schema(),
        )
        for attempt in range(_COMMIT_ATTEMPTS):
            dataset = self._dataset()
            try:
                if dataset is None:
                    lance.write_dataset(
                        table,
                        self.uri,
                        schema=_schema(),
                        mode="create",
                        data_storage_version=DATA_STORAGE_VERSION,
                        storage_options=self._options,
                    )
                else:
                    merge = dataset.merge_insert("artefact_id").when_not_matched_insert_all()
                    merge.execute(table)
                return
            except OSError:
                # Another writer created the table or committed first: re-read and retry.
                if attempt == _COMMIT_ATTEMPTS - 1:
                    raise


class Hydration:
    """A handle on one evidence reference and variant; ``read`` does the work (ADR 0014 §7)."""

    def __init__(
        self,
        media: "MediaLake",
        evidence_ref: EvidenceAnchor,
        variant: str,
        as_of: int | None,
        snapshot: int | None,
    ) -> None:
        self._media = media
        self.evidence_ref = evidence_ref
        self.variant = variant
        self.as_of = as_of
        self.snapshot = snapshot

    @property
    def artefact_id(self) -> str | None:
        """The id the artefact has or will have; None for ``bytes`` or an unknown variant."""
        if self.variant == "bytes" or self.variant not in VARIANTS:
            return None
        transform = transform_for(self.variant)
        return artefact_id(self.evidence_ref, self.variant, transform)

    def resolve(self) -> EvidenceBytes:
        """Where the cited bytes are, without reading them."""
        return self._media.resolver.resolve(self.evidence_ref, as_of=self.as_of)

    def read(self) -> Hydrated:
        """The slice or artefact; findings instead when the evidence cannot give it."""
        subject = str(self.evidence_ref.source)[:200]
        steps = parse_locator(self.evidence_ref)
        if isinstance(steps, MediaFinding):
            return Hydrated(None, (steps,))
        # The innermost step decides the variant.
        problem = check_variant(self.variant, steps, subject)
        if problem is not None:
            return Hydrated(None, (problem,))
        if self.variant == "bytes":
            if self.snapshot is not None:
                detail = "bytes are served from the source itself, never from a media snapshot"
                return Hydrated(None, (MediaFinding("invalid_request", subject, detail),))
            return self._slice()
        return self._artefact(subject)

    def _slice(self) -> Hydrated:
        evidence = self.resolve()
        if evidence.status != "resolved":
            return Hydrated(None, evidence.findings)
        subject = str(self.evidence_ref.source)[:200]
        try:
            cited = cited_bytes(evidence.inner, evidence.open(), subject, self._media.limits)
        except (DecodeFailure, ReadFailure) as failed:
            return Hydrated(None, (*evidence.findings, failed.finding))
        made = SourceSlice(evidence, cited.reader, cited.data, cited.inflated)
        return Hydrated(made, evidence.findings)

    def _artefact(self, subject: str) -> Hydrated:
        variant: Variant = self.variant  # type: ignore[assignment]
        transform = transform_for(variant)
        made_id = artefact_id(self.evidence_ref, variant, transform)
        store = self._media.store
        # The catalog must hold the evidence at ``as_of`` even when the artefact is stored: a
        # stored artefact is never served for evidence the catalog does not resolve.
        cataloged = self._media.resolver.cataloged(self.evidence_ref, as_of=self.as_of)
        if cataloged.status != "resolved":
            return Hydrated(None, catalog_findings(cataloged))
        if self.snapshot is not None:
            latest = store.snapshot()
            if latest is None or not 1 <= self.snapshot <= latest:
                detail = f"media snapshot {self.snapshot} is not in the table (latest {latest})"
                return Hydrated(None, (MediaFinding("as_of_out_of_range", subject, detail),))
            found = store.get(made_id, snapshot=self.snapshot)
            if found is None:
                detail = f"no {variant} artefact {made_id} at media snapshot {self.snapshot}"
                return Hydrated(None, (MediaFinding("unknown_artefact", subject, detail),))
            return Hydrated(found, ())
        found = store.get(made_id)
        if found is not None:
            return Hydrated(found, ())
        evidence = self._media.resolver.resolve(
            self.evidence_ref, as_of=self.as_of, resolution=cataloged
        )
        if evidence.status != "resolved":
            return Hydrated(None, evidence.findings)
        try:
            made = decode(variant, evidence.inner, evidence.open(), subject, self._media.limits)
        except (DecodeFailure, ReadFailure) as failed:
            return Hydrated(None, (*evidence.findings, failed.finding))
        row = {
            "artefact_id": made_id,
            "source": self.evidence_ref.source,
            "locator": canonical_json.dumps(list(self.evidence_ref.locator)).decode("utf-8"),
            "variant": variant,
            "transform_id": transform.id,
            "transform": canonical_json.dumps(transform.to_json()).decode("utf-8"),
            "media_type": made.media_type,
            "size": len(made.data),
            "sha256": "sha256:" + hashlib.sha256(made.data).hexdigest(),
            "metadata": canonical_json.dumps(made.metadata).decode("utf-8"),
        }
        store.put(row, made.data)
        stored = store.get(made_id)
        assert stored is not None, "an artefact just stored must be readable"
        return Hydrated(stored, evidence.findings)


class MediaLake:
    """Evidence resolution and hydration for one tenant (ADR 0014).

    ``resolver`` finds and verifies source bytes through the catalog; ``store`` is the tenant's
    Lance media table. ``limits`` bound what one hydration decodes.
    """

    def __init__(
        self, resolver: EvidenceResolver, store: MediaStore, *, limits: Limits = DEFAULT_LIMITS
    ) -> None:
        self.resolver = resolver
        self.store = store
        self.limits = limits

    def resolve(self, evidence_ref: EvidenceAnchor, *, as_of: int | None = None) -> EvidenceBytes:
        return self.resolver.resolve(evidence_ref, as_of=as_of)

    def hydrate(
        self,
        evidence_ref: EvidenceAnchor,
        variant: Literal["bytes", "frame", "image_region", "page", "row", "value"] | str,
        *,
        as_of: int | None = None,
        snapshot: int | None = None,
    ) -> Hydration:
        """A handle on ``variant`` of the cited evidence; nothing is read until ``read``.

        ``as_of`` is the catalog point the reference is resolved at. ``snapshot`` pins the media
        table version: the artefact is read as stored there, and never extracted anew.
        """
        return Hydration(self, evidence_ref, variant, as_of, snapshot)
