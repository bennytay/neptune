"""Readers that hand one source's bytes to an adapter (``neptune.adapters.contract.SourceReader``).

``BytesReader`` holds a source in memory and hashes it once, so what it serves is exactly the
artifact its content id names. Tests, the sandbox and small sources use it. A reader over large
local files that verifies the bytes it serves chunk by chunk is the store's (MVL-16).
"""

from neptune.identity.hashing import content_id
from neptune.model.ids import ContentId


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
