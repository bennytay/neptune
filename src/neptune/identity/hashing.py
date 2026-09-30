"""Tier-1 content ids: streaming sha256 over whole sources plus per-chunk hashes.

See ADR 0003 and ADR 0009.
"""

import hashlib
from typing import BinaryIO, Final

from neptune.model.ids import ContentId
from neptune.model.source import SourceArtifact

DEFAULT_CHUNK_SIZE: Final = 8 * 1024 * 1024
_READ_SIZE: Final = 1024 * 1024


def content_id(data: bytes) -> ContentId:
    """Content id of an in-memory byte string."""
    return ContentId("sha256:" + hashlib.sha256(data).hexdigest())


def digest_stream(stream: BinaryIO, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> SourceArtifact:
    """Hash ``stream`` from its current position to EOF in bounded memory.

    Chunk boundaries fall at exact multiples of ``chunk_size`` regardless of how many bytes each
    ``read`` returns, so short reads never change the result.
    """
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be > 0: {chunk_size}")
    whole = hashlib.sha256()
    chunk = hashlib.sha256()
    in_chunk = 0
    size = 0
    chunks: list[ContentId] = []
    while True:
        block = stream.read(min(_READ_SIZE, chunk_size - in_chunk))
        if not block:
            break
        whole.update(block)
        chunk.update(block)
        in_chunk += len(block)
        size += len(block)
        if in_chunk == chunk_size:
            chunks.append(ContentId("sha256:" + chunk.hexdigest()))
            chunk = hashlib.sha256()
            in_chunk = 0
    if in_chunk:
        chunks.append(ContentId("sha256:" + chunk.hexdigest()))
    return SourceArtifact(
        content_id=ContentId("sha256:" + whole.hexdigest()),
        size=size,
        chunk_size=chunk_size,
        chunks=tuple(chunks),
    )
