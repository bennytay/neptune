"""Readers that hand one source's bytes to an adapter (``neptune.adapters.contract.SourceReader``).

- ``BytesReader`` holds a source in memory and hashes it once, so what it serves is exactly the
  artifact its content id names. Tests, the sandbox and small sources use it.
- ``LocalReader`` reads a local source in place, checking each piece against the artifact's chunk
  hashes before serving it. Large sources use it: nothing is copied.
"""

import os
from collections import OrderedDict

from neptune.discovery.source import LocalSource
from neptune.identity.hashing import content_id
from neptune.model.ids import ContentId
from neptune.model.source import LocalPath, RawLocalPath, SourceArtifact


class BytesReader:
    """A source's bytes in memory. ``expected`` is checked against the bytes when given."""

    def __init__(self, data: bytes, expected: ContentId | None = None) -> None:
        if not isinstance(data, bytes):
            raise TypeError(f"data must be bytes, got {type(data).__name__}")
        self._data = data
        self._content_id = content_id(data)
        if expected is not None and expected != self._content_id:
            raise ValueError(f"the bytes are {self._content_id}, not {expected}")

    @property
    def content_id(self) -> ContentId:
        return self._content_id

    @property
    def size(self) -> int:
        return len(self._data)

    def read(self, offset: int, length: int) -> bytes:
        for name, value in (("offset", offset), ("length", length)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
        if offset > len(self._data):
            raise ValueError(f"offset {offset} is past the end of {len(self._data)} bytes")
        return self._data[offset : offset + length]


class SourceChangedError(Exception):
    """A source's bytes no longer match the artifact they were hashed as: never read silently."""


class LocalReader:
    """A local source read where it lies, every piece checked against its artifact (ADR 0026).

    Nothing is copied to disk. Reads go through the artifact's chunks (8 MiB by default): each is
    read whole, hashed and compared with ``artifact.chunks`` before any byte of it is served, and
    the last ``cache`` checked chunks are kept. A file that changed since it was hashed raises
    ``SourceChangedError``, so an adapter can never decode bytes its citations do not name.
    """

    def __init__(
        self,
        source: LocalSource,
        location: LocalPath | RawLocalPath,
        artifact: SourceArtifact,
        cache: int = 4,
    ) -> None:
        if cache < 1:
            raise ValueError(f"cache must hold at least one chunk: {cache}")
        self._artifact = artifact
        self._file = source.open(location)
        size = os.fstat(self._file.fileno()).st_size
        if size != artifact.size:
            self._file.close()
            raise SourceChangedError(f"{location} holds {size} bytes, not {artifact.size}")
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._capacity = cache

    def __enter__(self) -> "LocalReader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._file.close()

    def fileno(self) -> int:
        """The read-only descriptor the reader reads through: the one a sandboxed call keeps."""
        return self._file.fileno()

    @property
    def content_id(self) -> ContentId:
        return self._artifact.content_id

    @property
    def size(self) -> int:
        return self._artifact.size

    def _chunk(self, index: int) -> bytes:
        if index in self._cache:
            self._cache.move_to_end(index)
            return self._cache[index]
        width = self._artifact.chunk_size
        start = index * width
        expected = min(width, self._artifact.size - start)
        data = b""
        while len(data) < expected:
            piece = os.pread(self._file.fileno(), expected - len(data), start + len(data))
            if not piece:
                break
            data += piece
        if content_id(data) != self._artifact.chunks[index]:
            raise SourceChangedError(
                f"{self._artifact.content_id}: chunk {index} changed since it was hashed"
            )
        self._cache[index] = data
        if len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        return data

    def read(self, offset: int, length: int) -> bytes:
        for name, value in (("offset", offset), ("length", length)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
        if offset > self.size:
            raise ValueError(f"offset {offset} is past the end of {self.size} bytes")
        end = min(offset + length, self.size)
        width = self._artifact.chunk_size
        pieces = []
        while offset < end:
            index, within = divmod(offset, width)
            chunk = self._chunk(index)
            piece = chunk[within : within + (end - offset)]
            pieces.append(piece)
            offset += len(piece)
        return b"".join(pieces)
