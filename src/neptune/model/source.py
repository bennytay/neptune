"""Source entities: which bytes exist and where they were seen.

Semantics and the dedup policy are in ADRs 0009 and 0010 and ``docs/provenance-and-identity.md``.
These are ledger records (ADR 0017 §3): their ids come from their content and they carry no
provenance, because every other record's provenance points at them.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Final, TypeAlias

from neptune.model._fields import exact_object, json_array, json_int, json_str
from neptune.model.ids import (
    ContentId,
    ExternalObjectRef,
    RecordId,
    check_text,
    parse_content_id,
    parse_record_id,
)
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.record import Family, envelope, record_object


@dataclass(frozen=True)
class LocalPath:
    """A file location relative to the ingest root.

    ``/``-separated, with no ``.``, ``..`` or empty components. Stored exactly as the filesystem
    names it (no Unicode normalisation). A path is an observation of where bytes were seen; it never
    contributes to content identity.
    """

    path: str

    def __post_init__(self) -> None:
        check_text("path", self.path)
        if "\x00" in self.path:
            raise ValueError(f"path contains NUL: {self.path!r}")
        if any(part in ("", ".", "..") for part in self.path.split("/")):
            raise ValueError(
                f"path must be relative with no '', '.' or '..' components: {self.path!r}"
            )

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.path.split("/"))

    @property
    def key(self) -> tuple[str, ...]:
        return ("local", self.path)

    @property
    def raw(self) -> bytes:
        return self.path.encode("utf-8")

    def to_json(self) -> JsonObject:
        return {"kind": "local", "path": self.path}


@dataclass(frozen=True)
class RawLocalPath:
    """A ``LocalPath`` whose name is not valid UTF-8, kept as its exact bytes (ADR 0010).

    Same component rules as ``LocalPath``. A path that decodes as UTF-8 must be a ``LocalPath``, so
    each location has exactly one representation. Serialised as lowercase hex.
    """

    path: bytes

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("path must be non-empty")
        if b"\x00" in self.path:
            raise ValueError(f"path contains NUL: {self.path!r}")
        if any(part in (b"", b".", b"..") for part in self.path.split(b"/")):
            raise ValueError(
                f"path must be relative with no '', '.' or '..' components: {self.path!r}"
            )
        try:
            self.path.decode("utf-8")
        except UnicodeDecodeError:
            return
        raise ValueError(f"path is valid UTF-8; use LocalPath: {self.path!r}")

    @property
    def key(self) -> tuple[str, ...]:
        return ("local_raw", self.path.hex())

    @property
    def raw(self) -> bytes:
        return self.path

    def to_json(self) -> JsonObject:
        return {"kind": "local_raw", "path_hex": self.path.hex()}


def local_location(raw: bytes) -> LocalPath | RawLocalPath:
    """The one representation of a root-relative path given as bytes."""
    try:
        return LocalPath(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return RawLocalPath(raw)


SourceLocation: TypeAlias = LocalPath | RawLocalPath | ExternalObjectRef


@dataclass(frozen=True)
class SourceArtifact:
    """Tier-1 evidence: one distinct byte string, whatever its name or location.

    ``chunks`` are the content ids of consecutive ``chunk_size`` slices (the last may be shorter).
    They are verification metadata, not identity: a different chunk size never changes
    ``content_id``. An empty source has no chunks.
    """

    kind: ClassVar[str] = "source_artifact"
    family: ClassVar[Family] = Family.SOURCE
    content_id: ContentId
    size: int
    chunk_size: int
    chunks: tuple[ContentId, ...]

    def __post_init__(self) -> None:
        parse_content_id(self.content_id)
        for chunk in self.chunks:
            parse_content_id(chunk)
        if self.size < 0:
            raise ValueError(f"size must be >= 0: {self.size}")
        if self.chunk_size <= 0:
            raise ValueError(f"chunk_size must be > 0: {self.chunk_size}")
        expected = -(-self.size // self.chunk_size)
        if len(self.chunks) != expected:
            raise ValueError(
                f"{self.size} bytes in {self.chunk_size}-byte chunks needs {expected} chunk hashes,"
                f" got {len(self.chunks)}"
            )

    def to_json(self) -> JsonObject:
        return envelope(
            self.kind,
            {
                "chunk_size": self.chunk_size,
                "chunks": list(self.chunks),
                "content_id": self.content_id,
                "size": self.size,
            },
        )


@dataclass(frozen=True)
class SourceRevision:
    """One location observed holding one artifact's bytes.

    Revisions of a location form an append-only chain: ``supersedes`` is empty for the first
    revision seen at a location and otherwise holds exactly the id of the previous revision or
    absence. ``id`` is derived from the other three fields by
    ``neptune.identity.revisions.revision_id``.
    """

    kind: ClassVar[str] = "source_revision"
    family: ClassVar[Family] = Family.SOURCE
    id: RecordId
    location: SourceLocation
    content_id: ContentId
    supersedes: tuple[RecordId, ...]

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        parse_content_id(self.content_id)
        if len(self.supersedes) > 1:
            raise ValueError(f"a revision supersedes at most one revision: {self.supersedes}")
        for previous in self.supersedes:
            parse_record_id(previous)
            if previous == self.id:
                raise ValueError(f"revision cannot supersede itself: {self.id}")

    def to_json(self) -> JsonObject:
        return envelope(
            self.kind,
            {
                "content_id": self.content_id,
                "id": self.id,
                "location": self.location.to_json(),
                "supersedes": list(self.supersedes),
            },
        )


@dataclass(frozen=True)
class SourceAbsence:
    """A location that held bytes is observed to hold none (ADR 0010).

    Asserted only where the scan could see: never under an unreadable directory or a symlinked
    ancestor. Always supersedes exactly one revision; bytes reappearing supersede the absence.
    ``id`` is derived by ``neptune.identity.revisions.absence_id``.
    """

    kind: ClassVar[str] = "source_absence"
    family: ClassVar[Family] = Family.SOURCE
    id: RecordId
    location: SourceLocation
    supersedes: tuple[RecordId, ...]

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        if len(self.supersedes) != 1:
            raise ValueError(f"an absence supersedes exactly one revision: {self.supersedes}")
        parse_record_id(self.supersedes[0])
        if self.supersedes[0] == self.id:
            raise ValueError(f"absence cannot supersede itself: {self.id}")

    def to_json(self) -> JsonObject:
        return envelope(
            self.kind,
            {
                "id": self.id,
                "location": self.location.to_json(),
                "supersedes": list(self.supersedes),
            },
        )


# --- JSON --------------------------------------------------------------------------------------

_HEX_BYTES: Final = re.compile(r"(?:[0-9a-f]{2})+")


def external_object_ref_from_json(data: JsonValue) -> ExternalObjectRef:
    obj = exact_object(
        data, "external object", {"connector_id", "kind", "object_id", "revision_token"}
    )
    if obj["kind"] != "external":
        raise ValueError(f"expected an external object, got kind {obj['kind']!r}")
    return ExternalObjectRef(
        json_str(obj["connector_id"], "connector_id"),
        json_str(obj["object_id"], "object_id"),
        json_str(obj["revision_token"], "revision_token"),
    )


def location_from_json(data: JsonValue) -> SourceLocation:
    """Parse strictly: an unknown kind, a missing or extra key, or a malformed path raises."""
    if not isinstance(data, Mapping):
        raise ValueError(f"location must be a JSON object, got {type(data).__name__}")
    match data.get("kind"):
        case "local":
            obj = exact_object(data, "local location", {"kind", "path"})
            return LocalPath(json_str(obj["path"], "path"))
        case "local_raw":
            obj = exact_object(data, "raw local location", {"kind", "path_hex"})
            text = json_str(obj["path_hex"], "path_hex")
            if not _HEX_BYTES.fullmatch(text):
                raise ValueError(f"path_hex must be lowercase hex of whole bytes: {text!r}")
            return RawLocalPath(bytes.fromhex(text))
        case "external":
            return external_object_ref_from_json(data)
        case kind:
            raise ValueError(f"unknown location kind: {kind!r}")


def _supersedes(data: JsonValue) -> tuple[RecordId, ...]:
    return tuple(parse_record_id(json_str(v, "supersedes")) for v in json_array(data, "supersedes"))


def source_artifact_from_json(data: JsonValue) -> SourceArtifact:
    obj = record_object(data, SourceArtifact.kind, {"chunk_size", "chunks", "content_id", "size"})
    return SourceArtifact(
        content_id=parse_content_id(json_str(obj["content_id"], "content_id")),
        size=json_int(obj["size"], "size"),
        chunk_size=json_int(obj["chunk_size"], "chunk_size"),
        chunks=tuple(
            parse_content_id(json_str(chunk, "chunk"))
            for chunk in json_array(obj["chunks"], "chunks")
        ),
    )


def source_revision_from_json(data: JsonValue) -> SourceRevision:
    obj = record_object(data, SourceRevision.kind, {"content_id", "id", "location", "supersedes"})
    return SourceRevision(
        id=parse_record_id(json_str(obj["id"], "id")),
        location=location_from_json(obj["location"]),
        content_id=parse_content_id(json_str(obj["content_id"], "content_id")),
        supersedes=_supersedes(obj["supersedes"]),
    )


def source_absence_from_json(data: JsonValue) -> SourceAbsence:
    obj = record_object(data, SourceAbsence.kind, {"id", "location", "supersedes"})
    return SourceAbsence(
        id=parse_record_id(json_str(obj["id"], "id")),
        location=location_from_json(obj["location"]),
        supersedes=_supersedes(obj["supersedes"]),
    )
