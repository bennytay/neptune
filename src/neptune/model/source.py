"""Source entities: which bytes exist and where they were seen.

Semantics and the dedup policy are in ADR 0009 and ``docs/provenance-and-identity.md``.
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

    def to_json(self) -> JsonObject:
        return {"kind": "local", "path": self.path}


SourceLocation: TypeAlias = LocalPath | ExternalObjectRef


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
    revision seen at a location and otherwise holds exactly the id of the previous one. ``id`` is
    derived from the other three fields by ``neptune.identity.revisions.revision_id``.
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
