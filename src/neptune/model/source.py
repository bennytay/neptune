"""Source entities: which bytes exist and where they were seen.

Semantics and the dedup policy are in ADRs 0009 and 0010 and ``docs/provenance-and-identity.md``.
"""

from dataclasses import dataclass
from typing import TypeAlias

from neptune.model.ids import (
    ContentId,
    ExternalObjectRef,
    RecordId,
    check_text,
    parse_content_id,
    parse_record_id,
)
from neptune.model.jsonvalue import JsonObject


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
        return {
            "chunk_size": self.chunk_size,
            "chunks": list(self.chunks),
            "content_id": self.content_id,
            "size": self.size,
        }


@dataclass(frozen=True)
class SourceRevision:
    """One location observed holding one artifact's bytes.

    Revisions of a location form an append-only chain: ``supersedes`` is empty for the first
    revision seen at a location and otherwise holds exactly the id of the previous revision or
    absence. ``id`` is derived from the other three fields by
    ``neptune.identity.revisions.revision_id``.
    """

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
        return {
            "content_id": self.content_id,
            "id": self.id,
            "location": self.location.to_json(),
            "supersedes": list(self.supersedes),
        }


@dataclass(frozen=True)
class SourceAbsence:
    """A location that held bytes is observed to hold none (ADR 0010).

    Asserted only where the scan could see: never under an unreadable directory or a symlinked
    ancestor. Always supersedes exactly one revision; bytes reappearing supersede the absence.
    ``id`` is derived by ``neptune.identity.revisions.absence_id``.
    """

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
        return {
            "id": self.id,
            "location": self.location.to_json(),
            "supersedes": list(self.supersedes),
        }
