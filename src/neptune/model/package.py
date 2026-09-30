"""An ingest package's own documents: its manifest, its receipt and its envelope (ADR 0022).

- ``PackageManifest`` (``manifest.json``) lists every file of the package with its size and hash,
  the number of records in each table, how to reach each source's bytes, and the store's
  settings. The package's identity is the hash of its bytes.
- ``IngestReceipt`` (``receipt.json``) is the deterministic core of the receipt: what was read, by
  which adapter at which version, what came out, what went wrong and what stayed ambiguous. It is
  computed from the package's own tables (``neptune.store.receipt``), so anyone can recompute it,
  and its id is the hash of the rest.
- ``ReceiptEnvelope`` (``volatile/receipt-envelope.json``) holds what differs from one run of the
  same job to the next: the job id, wall-clock times, the host, the ingest root and durations. It
  is not in the manifest, so it never changes the package's identity.

These are documents, not record tables: each is one file, with the same ``kind`` and
``schema_version`` envelope as records (ADR 0017 §2).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Final

from neptune.model._fields import (
    enum_decoder,
    exact_object,
    json_array,
    json_int,
    json_str,
)
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import (
    ConfigHash,
    ContentId,
    LogicalId,
    RecordId,
    check_text,
    check_token,
    logical_id_from_json,
    parse_config_hash,
    parse_content_id,
    parse_record_id,
)
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import Knowledge, from_json, to_json
from neptune.model.provenance import provenance_from_json
from neptune.model.record import envelope, record_object
from neptune.model.source import SourceLocation, location_from_json
from neptune.model.time import Timestamp, timestamp_from_json

# A package path: relative, '/'-separated, no empty, '.' or '..' part.
_PATH: Final = re.compile(r"[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*")
# RFC 3339 in UTC, as the envelope records wall-clock times.
_UTC: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


def _check_sorted_pairs(field: str, pairs: tuple[tuple[str, Any], ...]) -> None:
    if not isinstance(pairs, tuple):
        raise TypeError(f"{field} must be a tuple of pairs, got {type(pairs).__name__}")
    names = [name for name, _ in pairs]
    if names != sorted(set(names)):
        raise ValueError(f"{field} must be unique and sorted: {names}")


def _check_count(field: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer, got {value!r}")


def _check_ids(field: str, ids: tuple[RecordId, ...], *, ordered: bool = True) -> None:
    if not isinstance(ids, tuple):
        raise TypeError(f"{field} must be a tuple, got {type(ids).__name__}")
    for record_id in ids:
        parse_record_id(record_id)
    if len(set(ids)) != len(ids) or (ordered and list(ids) != sorted(ids)):
        raise ValueError(f"{field} must be unique{' and sorted' if ordered else ''}: {ids}")


def _ids(data: JsonValue, what: str) -> tuple[RecordId, ...]:
    return tuple(parse_record_id(json_str(item, what)) for item in json_array(data, what))


def _counts(data: JsonValue, what: str) -> tuple[tuple[str, int], ...]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{what} must be a JSON object of counts")
    return tuple(sorted((key, json_int(value, key)) for key, value in data.items()))


def _check_members(field: str, items: tuple[Any, ...], kind: type) -> None:
    if not isinstance(items, tuple):
        raise TypeError(f"{field} must be a tuple, got {type(items).__name__}")
    for item in items:
        if not isinstance(item, kind):
            raise TypeError(f"{field} must hold {kind.__name__}s, got {item!r}")


# --- Manifest ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageFile:
    """One file of the package: its path from the package root, its size and its sha256."""

    path: str
    size: int
    sha256: ContentId

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not _PATH.fullmatch(self.path):
            raise ValueError(f"not a package path: {self.path!r}")
        if any(part in (".", "..") for part in self.path.split("/")):
            raise ValueError(f"not a package path: {self.path!r}")
        _check_count("size", self.size)
        parse_content_id(self.sha256)

    def to_json(self) -> JsonObject:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


def package_file_from_json(data: JsonValue) -> PackageFile:
    obj = exact_object(data, "package file", {"path", "sha256", "size"})
    return PackageFile(
        json_str(obj["path"], "path"),
        json_int(obj["size"], "size"),
        parse_content_id(json_str(obj["sha256"], "sha256")),
    )


class Storage(StrEnum):
    """Where a source's bytes are: left where they were found, or copied into the package."""

    REFERENCED = "referenced"  # at its locations under the ingest root (source_revision records)
    MATERIALISED = "materialised"  # at blobs/sha256/<2 hex>/<64 hex> in the package


@dataclass(frozen=True)
class SourceHandle:
    """How to reach one source's bytes: its content id, its size, and where they are stored."""

    content_id: ContentId
    size: int
    storage: Storage

    def __post_init__(self) -> None:
        parse_content_id(self.content_id)
        _check_count("size", self.size)
        if not isinstance(self.storage, Storage):
            raise TypeError(f"storage must be a Storage, got {self.storage!r}")

    def to_json(self) -> JsonObject:
        return {"content_id": self.content_id, "size": self.size, "storage": str(self.storage)}


def source_handle_from_json(data: JsonValue) -> SourceHandle:
    obj = exact_object(data, "source handle", {"content_id", "size", "storage"})
    return SourceHandle(
        parse_content_id(json_str(obj["content_id"], "content_id")),
        json_int(obj["size"], "size"),
        enum_decoder(Storage)(obj["storage"]),
    )


@dataclass(frozen=True)
class PackageManifest:
    """The package's table of contents (ADR 0022 §2). Its bytes' sha256 is the package id.

    - ``receipt``: the id of the receipt core in ``receipt.json``.
    - ``tables``: the number of records in each table, for every record kind of this schema
      version; a kind with none has an empty table.
    - ``sources``: every source's handle, sorted by content id.
    - ``files``: every file but ``manifest.json`` and ``volatile/``, sorted by path.
    - ``store``: the settings the store wrote series and blobs with, as it records them.
    """

    kind: ClassVar[str] = "package_manifest"
    receipt: RecordId
    tables: tuple[tuple[str, int], ...]
    sources: tuple[SourceHandle, ...]
    files: tuple[PackageFile, ...]
    store: JsonObject

    def __post_init__(self) -> None:
        parse_record_id(self.receipt)
        _check_sorted_pairs("tables", self.tables)
        for kind, count in self.tables:
            check_token("table", kind)
            _check_count(f"{kind} records", count)
        _check_members("sources", self.sources, SourceHandle)
        contents = [handle.content_id for handle in self.sources]
        if contents != sorted(set(contents)):
            raise ValueError("sources must be unique and sorted by content id")
        _check_members("files", self.files, PackageFile)
        paths = [file.path for file in self.files]
        if paths != sorted(set(paths)):
            raise ValueError("files must be unique and sorted by path")
        if not isinstance(self.store, Mapping):
            raise TypeError(f"store must be a JSON object, got {type(self.store).__name__}")

    def to_json(self) -> JsonObject:
        return envelope(
            self.kind,
            {
                "files": [file.to_json() for file in self.files],
                "receipt": self.receipt,
                "sources": [handle.to_json() for handle in self.sources],
                "store": self.store,
                "tables": dict(self.tables),
            },
        )


def package_manifest_from_json(data: JsonValue) -> PackageManifest:
    """Parse strictly: the schema version first, then exactly these keys and types."""
    obj = record_object(
        data, PackageManifest.kind, {"files", "receipt", "sources", "store", "tables"}
    )
    store = obj["store"]
    if not isinstance(store, Mapping):
        raise ValueError("store must be a JSON object")
    return PackageManifest(
        receipt=parse_record_id(json_str(obj["receipt"], "receipt")),
        tables=_counts(obj["tables"], "tables"),
        sources=tuple(source_handle_from_json(h) for h in json_array(obj["sources"], "sources")),
        files=tuple(package_file_from_json(f) for f in json_array(obj["files"], "files")),
        store=store,
    )


# --- Receipt core ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptSource:
    """One location the job saw holding bytes, and the transforms whose records cite them.

    ``read_by`` is empty for a source no adapter read: seen, hashed and kept, but not decoded.
    """

    location: SourceLocation
    content_id: ContentId
    size: int
    read_by: tuple[RecordId, ...]

    def __post_init__(self) -> None:
        parse_content_id(self.content_id)
        _check_count("size", self.size)
        _check_ids("read_by", self.read_by)

    def to_json(self) -> JsonObject:
        return {
            "content_id": self.content_id,
            "location": self.location.to_json(),
            "read_by": list(self.read_by),
            "size": self.size,
        }


def receipt_source_from_json(data: JsonValue) -> ReceiptSource:
    obj = exact_object(data, "receipt source", {"content_id", "location", "read_by", "size"})
    return ReceiptSource(
        location_from_json(obj["location"]),
        parse_content_id(json_str(obj["content_id"], "content_id")),
        json_int(obj["size"], "size"),
        _ids(obj["read_by"], "read_by"),
    )


@dataclass(frozen=True)
class ReceiptTransform:
    """A producer at one version and config: what replaying the job needs to match."""

    id: RecordId
    adapter_id: str
    adapter_version: str
    config_hash: ConfigHash
    libraries: tuple[tuple[str, str], ...]
    upstream: tuple[RecordId, ...]

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        check_token("adapter_id", self.adapter_id)
        check_text("adapter_version", self.adapter_version)
        parse_config_hash(self.config_hash)
        _check_sorted_pairs("libraries", self.libraries)
        _check_ids("upstream", self.upstream, ordered=False)

    def to_json(self) -> JsonObject:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "config_hash": self.config_hash,
            "id": self.id,
            "libraries": dict(self.libraries),
            "upstream": list(self.upstream),
        }


def receipt_transform_from_json(data: JsonValue) -> ReceiptTransform:
    obj = exact_object(
        data,
        "receipt transform",
        {"adapter_id", "adapter_version", "config_hash", "id", "libraries", "upstream"},
    )
    libraries = obj["libraries"]
    if not isinstance(libraries, Mapping):
        raise ValueError("libraries must be a JSON object of name to version")
    return ReceiptTransform(
        id=parse_record_id(json_str(obj["id"], "id")),
        adapter_id=json_str(obj["adapter_id"], "adapter_id"),
        adapter_version=json_str(obj["adapter_version"], "adapter_version"),
        config_hash=parse_config_hash(json_str(obj["config_hash"], "config_hash")),
        libraries=tuple(sorted((k, json_str(v, k)) for k, v in libraries.items())),
        upstream=_ids(obj["upstream"], "upstream"),
    )


def _knowledge(data: JsonValue, decode: Any) -> Knowledge[Any]:
    return from_json(data, decode, provenance_from_json)


def _count_value(data: JsonValue) -> int:
    return json_int(data, "message_count")


def _text(data: JsonValue) -> str:
    return json_str(data, "text")


@dataclass(frozen=True)
class ReceiptClock:
    """A clock by the field its ticks are read from and where (``TimestampDomain``), so a person
    reading the receipt sees ``header.stamp`` on ``/imu`` rather than an id."""

    id: RecordId
    field: str
    scope: tuple[str, ...]

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        check_text("field", self.field)
        if not isinstance(self.scope, tuple):
            raise TypeError(f"scope must be a tuple, got {type(self.scope).__name__}")
        for part in self.scope:
            check_text("scope part", part)

    def to_json(self) -> JsonObject:
        return {"field": self.field, "id": self.id, "scope": list(self.scope)}


def receipt_clock_from_json(data: JsonValue) -> ReceiptClock:
    obj = exact_object(data, "receipt clock", {"field", "id", "scope"})
    return ReceiptClock(
        parse_record_id(json_str(obj["id"], "id")),
        json_str(obj["field"], "field"),
        tuple(json_str(part, "scope part") for part in json_array(obj["scope"], "scope")),
    )


@dataclass(frozen=True)
class ReceiptRun:
    """A run and its declared extent. States are the run record's, provenance and all: a state
    without its own provenance cites the run record, ``id``."""

    id: RecordId
    logical_id: Knowledge[LogicalId]
    machine: Knowledge[LogicalId]
    first: Knowledge[Timestamp]
    last: Knowledge[Timestamp]
    streams: int

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        _check_count("streams", self.streams)

    def to_json(self) -> JsonObject:
        return {
            "first": to_json(self.first, Timestamp.to_json),
            "id": self.id,
            "last": to_json(self.last, Timestamp.to_json),
            "logical_id": to_json(self.logical_id, LogicalId.to_json),
            "machine": to_json(self.machine, LogicalId.to_json),
            "streams": self.streams,
        }


def receipt_run_from_json(data: JsonValue) -> ReceiptRun:
    obj = exact_object(
        data, "receipt run", {"first", "id", "last", "logical_id", "machine", "streams"}
    )
    return ReceiptRun(
        id=parse_record_id(json_str(obj["id"], "id")),
        logical_id=_knowledge(obj["logical_id"], logical_id_from_json),
        machine=_knowledge(obj["machine"], logical_id_from_json),
        first=_knowledge(obj["first"], timestamp_from_json),
        last=_knowledge(obj["last"], timestamp_from_json),
        streams=json_int(obj["streams"], "streams"),
    )


@dataclass(frozen=True)
class ReceiptStream:
    """A stream, its clocks and its declared count and extent, as its record states them."""

    id: RecordId
    run: RecordId
    topic: Knowledge[str]
    clocks: tuple[RecordId, ...]
    message_count: Knowledge[int]
    first: Knowledge[Timestamp]
    last: Knowledge[Timestamp]

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        parse_record_id(self.run)
        _check_ids("clocks", self.clocks, ordered=False)

    def to_json(self) -> JsonObject:
        return {
            "clocks": list(self.clocks),
            "first": to_json(self.first, Timestamp.to_json),
            "id": self.id,
            "last": to_json(self.last, Timestamp.to_json),
            "message_count": to_json(self.message_count),
            "run": self.run,
            "topic": to_json(self.topic),
        }


def receipt_stream_from_json(data: JsonValue) -> ReceiptStream:
    obj = exact_object(
        data,
        "receipt stream",
        {"clocks", "first", "id", "last", "message_count", "run", "topic"},
    )
    return ReceiptStream(
        id=parse_record_id(json_str(obj["id"], "id")),
        run=parse_record_id(json_str(obj["run"], "run")),
        topic=_knowledge(obj["topic"], _text),
        clocks=_ids(obj["clocks"], "clocks"),
        message_count=_knowledge(obj["message_count"], _count_value),
        first=_knowledge(obj["first"], timestamp_from_json),
        last=_knowledge(obj["last"], timestamp_from_json),
    )


@dataclass(frozen=True)
class ReceiptEntity:
    """A machine, site or asset the job found, with every id its declaration states."""

    id: RecordId
    record_kind: str
    identifiers: tuple[LogicalId, ...]

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        check_token("record_kind", self.record_kind)
        _check_members("identifiers", self.identifiers, LogicalId)
        keys = [(i.namespace, i.value) for i in self.identifiers]
        if keys != sorted(set(keys)):
            raise ValueError("identifiers must be unique and sorted")

    def to_json(self) -> JsonObject:
        return {
            "id": self.id,
            "identifiers": [identifier.to_json() for identifier in self.identifiers],
            "record_kind": self.record_kind,
        }


def receipt_entity_from_json(data: JsonValue) -> ReceiptEntity:
    obj = exact_object(data, "receipt entity", {"id", "identifiers", "record_kind"})
    return ReceiptEntity(
        id=parse_record_id(json_str(obj["id"], "id")),
        record_kind=json_str(obj["record_kind"], "record_kind"),
        identifiers=tuple(
            logical_id_from_json(item) for item in json_array(obj["identifiers"], "identifiers")
        ),
    )


@dataclass(frozen=True)
class ReceiptFinding:
    """A finding, as a line of the receipt; the record in its table holds the rest."""

    id: RecordId
    code: str
    category: FindingCategory
    severity: Severity
    message: str

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        check_text("code", self.code)
        if not isinstance(self.category, FindingCategory):
            raise TypeError(f"category must be a FindingCategory, got {self.category!r}")
        if not isinstance(self.severity, Severity):
            raise TypeError(f"severity must be a Severity, got {self.severity!r}")
        check_text("message", self.message)

    def to_json(self) -> JsonObject:
        return {
            "category": str(self.category),
            "code": self.code,
            "id": self.id,
            "message": self.message,
            "severity": str(self.severity),
        }


def receipt_finding_from_json(data: JsonValue) -> ReceiptFinding:
    obj = exact_object(data, "receipt finding", {"category", "code", "id", "message", "severity"})
    return ReceiptFinding(
        id=parse_record_id(json_str(obj["id"], "id")),
        code=json_str(obj["code"], "code"),
        category=enum_decoder(FindingCategory)(obj["category"]),
        severity=enum_decoder(Severity)(obj["severity"]),
        message=json_str(obj["message"], "message"),
    )


@dataclass(frozen=True)
class AmbiguousField:
    """A field whose state is ``Ambiguous``: the record, and an RFC 6901 pointer into its JSON."""

    record: RecordId
    pointer: str

    def __post_init__(self) -> None:
        parse_record_id(self.record)
        if not isinstance(self.pointer, str) or not self.pointer.startswith("/"):
            raise ValueError(
                f"pointer must be an RFC 6901 pointer to a field, got {self.pointer!r}"
            )

    def to_json(self) -> JsonObject:
        return {"pointer": self.pointer, "record": self.record}


def ambiguous_field_from_json(data: JsonValue) -> AmbiguousField:
    obj = exact_object(data, "ambiguous field", {"pointer", "record"})
    return AmbiguousField(
        parse_record_id(json_str(obj["record"], "record")), json_str(obj["pointer"], "pointer")
    )


# Findings are listed most severe first (ADR 0017 §9).
SEVERITY_ORDER: Final = (Severity.ERROR, Severity.WARNING, Severity.INFO)


def finding_order(finding: ReceiptFinding) -> tuple[int, str, str]:
    return (SEVERITY_ORDER.index(finding.severity), finding.code, finding.id)


@dataclass(frozen=True)
class IngestReceipt:
    """The deterministic core of the receipt: what Neptune did, derived from the package alone.

    - ``sources``: every location seen holding bytes, sorted by location, with the transforms
      that read it (none: seen but not decoded). ``absent``: locations seen to hold none.
    - ``transforms``: every producer, sorted by id: the adapters selected and their versions,
      configs and libraries, which is what a replay must match.
    - ``records``: how many records of each kind the package holds.
    - ``clocks``: every clock, by field and scope, sorted by id.
    - ``runs``, ``streams``: what was recorded and its declared time coverage, sorted by id.
    - ``entities``: machines, sites and assets with their stated ids, sorted by id.
    - ``findings``: every finding, most severe first, then by code and id.
    - ``ambiguous``: every field whose state is ``Ambiguous``, sorted by record and pointer.

    ``id`` is ``record_id("ingest_receipt", …)`` over everything else, so the core carries its
    own hash (``neptune.store.receipt``). Bindings of runs to configurations join with MVL-38.
    """

    kind: ClassVar[str] = "ingest_receipt"
    id: RecordId
    sources: tuple[ReceiptSource, ...]
    absent: tuple[SourceLocation, ...]
    transforms: tuple[ReceiptTransform, ...]
    records: tuple[tuple[str, int], ...]
    clocks: tuple[ReceiptClock, ...]
    runs: tuple[ReceiptRun, ...]
    streams: tuple[ReceiptStream, ...]
    entities: tuple[ReceiptEntity, ...]
    findings: tuple[ReceiptFinding, ...]
    ambiguous: tuple[AmbiguousField, ...]

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        _check_members("sources", self.sources, ReceiptSource)
        locations = [source.location.key for source in self.sources]
        if locations != sorted(set(locations)):
            raise ValueError("sources must be unique and sorted by location")
        if not isinstance(self.absent, tuple):
            raise TypeError("absent must be a tuple of locations")
        absent = [location.key for location in self.absent]
        if absent != sorted(set(absent)) or set(absent) & set(locations):
            raise ValueError("absent locations must be unique, sorted and not also present")
        _check_members("transforms", self.transforms, ReceiptTransform)
        _check_ids("transform ids", tuple(t.id for t in self.transforms))
        _check_sorted_pairs("records", self.records)
        for kind, count in self.records:
            check_token("record kind", kind)
            _check_count(f"{kind} records", count)
        for field, items, member in (
            ("clocks", self.clocks, ReceiptClock),
            ("runs", self.runs, ReceiptRun),
            ("streams", self.streams, ReceiptStream),
            ("entities", self.entities, ReceiptEntity),
        ):
            _check_members(field, items, member)
            _check_ids(f"{field} ids", tuple(item.id for item in items))
        _check_members("findings", self.findings, ReceiptFinding)
        if list(self.findings) != sorted(self.findings, key=finding_order):
            raise ValueError("findings are listed most severe first, then by code and id")
        _check_members("ambiguous", self.ambiguous, AmbiguousField)
        keys = [(field.record, field.pointer) for field in self.ambiguous]
        if keys != sorted(set(keys)):
            raise ValueError("ambiguous fields must be unique and sorted")

    def content_json(self) -> JsonObject:
        """Everything but ``id``: what the id is derived from."""
        return {
            "absent": [location.to_json() for location in self.absent],
            "ambiguous": [field.to_json() for field in self.ambiguous],
            "clocks": [clock.to_json() for clock in self.clocks],
            "entities": [entity.to_json() for entity in self.entities],
            "findings": [finding.to_json() for finding in self.findings],
            "records": dict(self.records),
            "runs": [run.to_json() for run in self.runs],
            "sources": [source.to_json() for source in self.sources],
            "streams": [stream.to_json() for stream in self.streams],
            "transforms": [transform.to_json() for transform in self.transforms],
        }

    def to_json(self) -> JsonObject:
        return envelope(self.kind, {**self.content_json(), "id": self.id})


def ingest_receipt_from_json(data: JsonValue) -> IngestReceipt:
    """Parse strictly; ``neptune.store.receipt.check_receipt`` recomputes the id."""
    obj = record_object(
        data,
        IngestReceipt.kind,
        {
            "absent",
            "ambiguous",
            "clocks",
            "entities",
            "findings",
            "id",
            "records",
            "runs",
            "sources",
            "streams",
            "transforms",
        },
    )
    return IngestReceipt(
        id=parse_record_id(json_str(obj["id"], "id")),
        sources=tuple(receipt_source_from_json(s) for s in json_array(obj["sources"], "sources")),
        absent=tuple(location_from_json(a) for a in json_array(obj["absent"], "absent")),
        transforms=tuple(
            receipt_transform_from_json(t) for t in json_array(obj["transforms"], "transforms")
        ),
        records=_counts(obj["records"], "records"),
        clocks=tuple(receipt_clock_from_json(c) for c in json_array(obj["clocks"], "clocks")),
        runs=tuple(receipt_run_from_json(r) for r in json_array(obj["runs"], "runs")),
        streams=tuple(receipt_stream_from_json(s) for s in json_array(obj["streams"], "streams")),
        entities=tuple(
            receipt_entity_from_json(e) for e in json_array(obj["entities"], "entities")
        ),
        findings=tuple(
            receipt_finding_from_json(f) for f in json_array(obj["findings"], "findings")
        ),
        ambiguous=tuple(
            ambiguous_field_from_json(a) for a in json_array(obj["ambiguous"], "ambiguous")
        ),
    )


# --- Volatile envelope -------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptEnvelope:
    """What changes between two runs of the same job (ADR 0022 §4). Never in the manifest.

    ``receipt`` names the core it accompanies. ``started`` and ``finished`` are the host's
    wall-clock readings, RFC 3339 in UTC. ``root`` is the ingest root as the host names it, which
    is what a referenced source's location is relative to. ``durations`` are seconds per phase.
    """

    kind: ClassVar[str] = "ingest_receipt_envelope"
    receipt: RecordId
    job: str
    started: str
    finished: str
    host: str
    root: str
    durations: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        parse_record_id(self.receipt)
        for name in ("job", "host", "root"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a str, got {type(value).__name__}")
            check_text(name, value)
        for name in ("started", "finished"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _UTC.fullmatch(value):
                raise ValueError(f"{name} must be RFC 3339 in UTC, got {value!r}")
        _check_sorted_pairs("durations", self.durations)
        for phase, seconds in self.durations:
            check_token("phase", phase)
            if not isinstance(seconds, float) or not seconds >= 0.0:
                raise ValueError(
                    f"duration of {phase} must be non-negative seconds, got {seconds!r}"
                )

    def to_json(self) -> JsonObject:
        return envelope(
            self.kind,
            {
                "durations": dict(self.durations),
                "finished": self.finished,
                "host": self.host,
                "job": self.job,
                "receipt": self.receipt,
                "root": self.root,
                "started": self.started,
            },
        )


def receipt_envelope_from_json(data: JsonValue) -> ReceiptEnvelope:
    obj = record_object(
        data,
        ReceiptEnvelope.kind,
        {"durations", "finished", "host", "job", "receipt", "root", "started"},
    )
    durations = obj["durations"]
    if not isinstance(durations, Mapping):
        raise ValueError("durations must be a JSON object of phase to seconds")
    pairs = []
    for phase, seconds in durations.items():
        if not isinstance(seconds, float):
            raise ValueError(f"duration of {phase} must be a float, got {seconds!r}")
        pairs.append((phase, seconds))
    return ReceiptEnvelope(
        receipt=parse_record_id(json_str(obj["receipt"], "receipt")),
        job=json_str(obj["job"], "job"),
        started=json_str(obj["started"], "started"),
        finished=json_str(obj["finished"], "finished"),
        host=json_str(obj["host"], "host"),
        root=json_str(obj["root"], "root"),
        durations=tuple(sorted(pairs)),
    )
