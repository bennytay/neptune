"""What every parser of one source shares: the emitter, the budget and the configured limits."""

import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from neptune.adapters.image._emit import (
    LIMIT_EXCEEDED,
    TRUNCATED,
    VALUE_NOT_COPIED,
    VALUE_UNREADABLE,
    CellInput,
    Emitter,
)
from neptune.adapters.image._space import Budget, LimitHit, Truncated
from neptune.model.ids import RecordId
from neptune.model.knowledge import AssertionKind, Known, NotCovered, Unknown
from neptune.model.provenance import Locator, RowCell

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue

# Fixed structures that are the measured geometry and layout of the raster; every other table is
# something the file's writer or camera declares (EXIF, XMP, ICC, text, density, gamma, ...).
_MEASURED = frozenset({"IHDR", "acTL", "VP8X", "VP8", "VP8L", "PNM header", "BITMAPFILEHEADER"})


def _measured(name: str) -> bool:
    return name in _MEASURED or name.startswith("SOF")


FIRST_CELLS: Final = 16  # cells one finding names; the count says how many there were


@dataclass(frozen=True)
class NotCopied:
    """A value over ``max_value_bytes``: its cell is ``NotCovered`` and a finding cites the cell.

    ``length`` is its size in bytes when the parser knows it; a compressed text is only known to
    be longer than the cap.
    """

    length: int | None


class Skipped:
    """The cells of one table that were not copied, as one finding: a count and the first few."""

    def __init__(self) -> None:
        self.count = 0
        self.first: list[tuple[int, int, NotCopied]] = []

    def add(self, row: int, column: int, value: NotCopied) -> None:
        self.count += 1
        if len(self.first) < FIRST_CELLS:
            self.first.append((row, column, value))


@dataclass(frozen=True)
class Context:
    """One ``ingest`` call's parsing state. ``out`` collects records and findings."""

    out: Emitter
    budget: Budget
    whole: tuple[Locator, ...]
    max_value_bytes: int
    max_metadata_bytes: int
    max_pixels: int

    def stopped(self, hit: LimitHit) -> None:
        """A limit stopped parsing: one finding for the source, however often it is hit."""
        self.out.finding(
            LIMIT_EXCEEDED,
            self.whole,
            f"{hit.option} ({hit.limit}) stopped parsing; what was read before it is kept",
            {"limit": hit.limit, "option": hit.option},
        )

    def cut(self, exc: Truncated, locator: Sequence[Locator], what: str) -> None:
        """A structure that runs past the bytes holding it: a finding, and reading it stops."""
        self.out.finding(
            TRUNCATED,
            locator,
            f"{what} runs past the {exc.size} bytes that hold it",
            {"length": exc.length, "offset": exc.offset, "size": exc.size},
        )

    def structure(
        self,
        locator: Sequence[Locator],
        name: str,
        columns: tuple[str, ...],
        values: Sequence[CellInput | NotCopied],
    ) -> RecordId | None:
        """A fixed structure as a table of one row: ``columns`` are the specification's names.

        A ``NotCopied`` value is a ``NotCovered`` cell, and one finding cites those cells.
        """
        self.budget.entry()
        kind = AssertionKind.OBSERVED if _measured(name) else AssertionKind.STATED
        table = self.out.table(locator, Known(name), columns, name, kind)
        if table is not None:
            skipped = Skipped()
            cells: list[CellInput] = []
            for column, value in enumerate(values):
                if isinstance(value, NotCopied):
                    skipped.add(0, column, value)
                    cells.append(NotCovered())
                else:
                    cells.append(value)
            self.out.row(table, locator, 0, cells)
            self.not_copied(locator, columns, skipped)
        return table

    def not_copied(
        self, table_locator: Sequence[Locator], columns: tuple[str, ...], skipped: Skipped
    ) -> None:
        """One ``image.value_not_copied`` for a table's over-cap cells, citing the first cell."""
        if not skipped.count:
            return
        row, column, _ = skipped.first[0]
        subject = (*table_locator, RowCell(row, column, columns[column]))
        cells = []
        for at_row, at_column, value in skipped.first:
            cell: dict[str, JsonValue] = {"column": at_column, "row": at_row}
            if value.length is not None:
                cell["length"] = value.length
            cells.append(cell)
        self.out.finding(
            VALUE_NOT_COPIED,
            subject,
            f"{skipped.count} values over max_value_bytes ({self.max_value_bytes}) are not copied:"
            " their cells are NotCovered and the bytes hold the values",
            {"cells": cells, "count": skipped.count, "max_value_bytes": self.max_value_bytes},
        )

    def inflate(self, data: bytes) -> bytes:
        """``data`` as a zlib stream inflated to at most ``max_metadata_bytes``.

        The inflated bytes of all streams of one source are bounded too (``Budget.inflated``).
        Raises ``LimitHit`` past either limit (a decompression bomb costs at most the limit) and
        ``zlib.error`` for a damaged stream; a stream cut short is also ``zlib.error``.
        """
        room = min(self.max_metadata_bytes, self.budget.inflate_room)
        inflater = zlib.decompressobj()
        out = inflater.decompress(data, room + 1)
        if len(out) > room:
            self.budget.inflated(
                len(out)
            )  # a bomb spends what it cost, so many cannot all be tried
            if room < self.max_metadata_bytes:
                raise LimitHit(
                    "the inflate total (4 x max_metadata_bytes)", self.budget.inflate_total
                )
            raise LimitHit("max_metadata_bytes", self.max_metadata_bytes)
        if not inflater.eof:
            raise zlib.error("the zlib stream ends before its end marker")
        self.budget.inflated(len(out))
        return out

    def inflate_prefix(self, data: bytes, keep: int) -> tuple[bytes, bool]:
        """The first ``keep`` bytes of a zlib stream, and whether more follow them.

        Only ``keep + 1`` bytes are ever produced: a text cell needs no more, however far the
        stream would inflate. ``zlib.error`` if the stream is damaged or ends early.
        """
        inflater = zlib.decompressobj()
        out = inflater.decompress(data, keep + 1)
        if len(out) <= keep and not inflater.eof:
            raise zlib.error("the zlib stream ends before its end marker")
        return out[:keep], len(out) > keep

    def text_cell(
        self,
        raw: bytes,
        more: bool,
        codec: str,
        locator: Sequence[Locator],
        total: int | None = None,
    ) -> CellInput | NotCopied:
        """A text as a cell: all of it, or ``NotCopied`` if it is over ``max_value_bytes``.

        ``raw`` is at most ``max_value_bytes + 1`` bytes of it, ``more`` says bytes follow, and
        ``total`` is its size if known. A text in a different ``codec`` is ``Unknown``.
        """
        if more or len(raw) > self.max_value_bytes:
            return NotCopied(total)
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            self.out.finding(VALUE_UNREADABLE, locator, f"the text is not {codec}")
            return Unknown()
