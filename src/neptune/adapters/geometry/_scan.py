"""Bounded reading: the bytes a parser walks, line by line or in blocks, and what that may cost.

Every read is a range of the source checked against its size first, and a scan spends a byte
budget (``max_scan_bytes``), so a hostile file costs at most the limit however it is built. A line
longer than ``MAX_LINE`` is reported once and skipped, never buffered.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import SourceReader, read_pieces

MAX_LINE: Final = 64 * 1024
BLOCK: Final = 1024 * 1024


class LimitHit(Exception):
    """A configured limit stopped a parse: ``option`` at ``limit``."""

    def __init__(self, option: str, limit: int) -> None:
        super().__init__(f"{option} ({limit}) reached")
        self.option = option
        self.limit = limit


class Unreadable(Exception):
    """The bytes are not a readable file of the format they start as; no record is made."""

    def __init__(self, message: str, offset: int = 0, length: int | None = None) -> None:
        super().__init__(message)
        self.offset = offset
        self.length = length


@dataclass(frozen=True)
class Line:
    """One line, without its end of line: where it starts, its bytes, and whether it was too long
    to keep (``data`` is then empty)."""

    offset: int
    data: bytes
    overlong: bool = False


class Scanner:
    """Reads ranges of one source, spending one shared budget of ``max_bytes``."""

    def __init__(self, source: SourceReader, max_bytes: int) -> None:
        self.source = source
        self.max_bytes = max_bytes
        self.spent = 0

    def _spend(self, count: int) -> None:
        self.spent += count
        if self.spent > self.max_bytes:
            raise LimitHit("max_scan_bytes", self.max_bytes)

    def read(self, offset: int, length: int) -> bytes:
        """Exactly ``length`` bytes at ``offset``; ``Unreadable`` if the source ends first."""
        if offset < 0 or length < 0 or offset + length > self.source.size:
            raise Unreadable("a structure runs past the end of the file", offset, length)
        self._spend(length)
        return b"".join(read_pieces(self.source, offset, offset + length))

    def blocks(self, start: int, end: int, size: int = BLOCK) -> Iterator[bytes]:
        """``[start, end)`` in order, at most ``size`` bytes at a time."""
        for piece in read_pieces(self.source, start, min(end, self.source.size), size):
            self._spend(len(piece))
            yield piece

    def lines(self, start: int, end: int) -> Iterator[Line]:
        """Every line of ``[start, end)``, ``\\n`` or ``\\r\\n`` ended; the last may be unended."""
        buffer = b""
        at = start  # offset of buffer[0]
        skipping = False
        for piece in self.blocks(start, end):
            buffer += piece
            position = 0
            while True:
                newline = buffer.find(b"\n", position)
                if newline < 0:
                    break
                if skipping:
                    skipping = False
                else:
                    yield Line(at + position, buffer[position:newline].rstrip(b"\r"))
                position = newline + 1
            buffer = buffer[position:]
            at += position
            if len(buffer) > MAX_LINE:
                if not skipping:
                    yield Line(at, b"", overlong=True)
                skipping = True
                at += len(buffer)
                buffer = b""
        if buffer and not skipping:
            yield Line(at, buffer.rstrip(b"\r"))
