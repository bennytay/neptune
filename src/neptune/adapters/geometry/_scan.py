"""Bounded reading: the bytes a parser walks, line by line or in blocks, and what that may cost.

Every read is a range of the source checked against its size first, and a scan spends a byte
budget (``max_scan_bytes``), so a hostile file costs at most the limit however it is built. A line
longer than ``MAX_LINE`` is reported once and skipped, never buffered.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import SourceReader, read_pieces

MAX_LINE: Final = 64 * 1024
BLOCK: Final = 1024 * 1024
BOM: Final = b"\xef\xbb\xbf"
LINE_COST: Final = 32  # the least a line is charged against the scan budget, however short
_LINE: Final = re.compile(rb"[^\r\n]+")
_BREAK: Final = re.compile(rb"[\r\n]")
_NUMBER: Final = re.compile(rb"[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?")
_INDEX: Final = re.compile(rb"-?[0-9]+")


def parse_number(token: bytes) -> float | None:
    """``token`` as the decimal number text formats write (``1``, ``-0.5``, ``.5``, ``1e-3``), or
    ``None``. Python's own ``float`` also reads ``1_0``, ``nan``, ``infinity`` and padded forms,
    which none of these formats write: they are not numbers here."""
    return float(token) if _NUMBER.fullmatch(token) else None


def parse_index(token: bytes) -> int | None:
    """``token`` as a signed decimal integer of at most 18 digits, or ``None`` (no ``1_0``)."""
    return int(token) if _INDEX.fullmatch(token) and len(token) <= 18 else None


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
        """Every non-empty line of ``[start, end)``; ``\\n``, ``\\r\\n`` and ``\\r`` end one.

        Runs of line ends are skipped at C speed, and each line is charged at least
        ``LINE_COST`` bytes, so a file of short lines costs about what its size says.
        """
        carry = b""  # a line that may continue in the next block
        carry_at = start
        skipping = False  # inside an over-long line: nothing is kept until it ends
        first = True
        for piece in self.blocks(start, end):
            base = carry_at
            if first and start == 0 and piece.startswith(BOM):
                piece, base = piece[len(BOM) :], len(BOM)  # a byte order mark is not text
            first = False
            data = carry + piece
            carry = b""
            position = 0
            if skipping:
                found = _BREAK.search(data)
                if found is None:
                    carry_at = base + len(data)
                    continue
                skipping, position = False, found.start()
            carry_at = base + len(data)
            for line in _LINE.finditer(data, position):
                length = line.end() - line.start()
                if line.end() == len(data):  # it may go on in the next block
                    carry, carry_at = line.group(), base + line.start()
                    break
                self._charge(length)
                if length > MAX_LINE:
                    yield Line(base + line.start(), b"", overlong=True)
                else:
                    yield Line(base + line.start(), line.group())
            if len(carry) > MAX_LINE:
                yield Line(carry_at, b"", overlong=True)
                skipping, carry, carry_at = True, b"", carry_at + len(carry)
        if carry and not skipping:
            self._charge(len(carry))
            yield Line(carry_at, carry)

    def _charge(self, length: int) -> None:
        if length < LINE_COST:
            self._spend(LINE_COST - length)
