"""The bytes a parser reads, how a range of them is cited, and the budget parsing spends.

A ``Space`` is either a range of the source, read in place, or a payload the transform decoded
(an inflated zlib stream, ICC segments joined in order) held in memory. Both check bounds before
they read, so a declared length past the end is a ``Truncated`` that the caller turns into a
finding, never an allocation or a short read. Citations are exact: a range of the source is a
``ByteRange`` of the source; a range of a payload is the citation of the structure that carries
it, the ``image:payload`` step, and a ``ByteRange`` inside the payload.
"""

from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import SourceReader, read_pieces
from neptune.model.provenance import AdapterLocator, ByteRange, Locator, adapter_locator

PAYLOAD_STEP: Final = "image:payload"


def payload_step(decode: str) -> AdapterLocator:
    """The ``image:payload`` step: what the structure cited before it decodes to."""
    return adapter_locator(PAYLOAD_STEP, {"decode": decode})


class Truncated(Exception):
    """``[offset, offset + length)`` runs past the ``size`` bytes a space holds."""

    def __init__(self, offset: int, length: int, size: int) -> None:
        super().__init__(f"[{offset}, {offset + length}) is past the {size} bytes held")
        self.offset = offset
        self.length = length
        self.size = size


class LimitHit(Exception):
    """A configured limit stopped parsing: ``option`` at ``limit``."""

    def __init__(self, option: str, limit: int) -> None:
        super().__init__(f"{option} ({limit}) reached")
        self.option = option
        self.limit = limit


VALUES_PER_ENTRY: Final = 256  # array items (strip offsets) one emitted row may stand for


class Budget:
    """What one source may cost: structures walked and table rows emitted (``max_*`` options)."""

    def __init__(self, structures: int, entries: int) -> None:
        self.max_structures = structures
        self.max_entries = entries
        self._structures = structures
        self._entries = entries
        self._values = entries * VALUES_PER_ENTRY

    def structure(self) -> None:
        if self._structures <= 0:
            raise LimitHit("max_structures", self.max_structures)
        self._structures -= 1

    def entry(self) -> None:
        if self._entries <= 0:
            raise LimitHit("max_entries", self.max_entries)
        self._entries -= 1

    def values(self, count: int) -> None:
        """Spend ``count`` array items checked but not emitted (strip offsets and byte counts)."""
        if count > self._values:
            raise LimitHit("max_entries", self.max_entries)
        self._values -= count


@dataclass(frozen=True)
class Space:
    """``size`` bytes from ``base`` of the source, or of ``data`` when it is a decoded payload.

    ``prefix`` cites a payload's carrier and its ``image:payload`` step; it is empty for the
    source itself. Offsets passed to the methods count from the space's own start.
    """

    source: SourceReader
    base: int
    size: int
    data: bytes | None = None
    prefix: tuple[Locator, ...] = ()

    @classmethod
    def file(cls, source: SourceReader) -> "Space":
        return cls(source, 0, source.size)

    @classmethod
    def payload(cls, source: SourceReader, data: bytes, prefix: tuple[Locator, ...]) -> "Space":
        return cls(source, 0, len(data), data, prefix)

    def fits(self, offset: int, length: int) -> bool:
        return offset >= 0 and length >= 0 and offset + length <= self.size

    def read(self, offset: int, length: int) -> bytes:
        """Exactly ``length`` bytes from ``offset``, or ``Truncated``."""
        if not self.fits(offset, length):
            raise Truncated(offset, length, self.size)
        start = self.base + offset
        if self.data is not None:
            return self.data[start : start + length]
        if not length:
            return b""
        return b"".join(read_pieces(self.source, start, start + length))

    def cite(self, offset: int, length: int) -> tuple[Locator, ...]:
        """The exact citation of ``[offset, offset + length)``."""
        return (*self.prefix, ByteRange(self.base + offset, length))

    def window(self, offset: int, length: int) -> "Space":
        """``[offset, offset + length)`` as a space of its own, offsets counted from its start."""
        if not self.fits(offset, length):
            raise Truncated(offset, length, self.size)
        return Space(self.source, self.base + offset, length, self.data, self.prefix)

    def whole(self) -> tuple[Locator, ...]:
        return self.cite(0, self.size)
