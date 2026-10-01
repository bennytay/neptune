"""What every parser of one source shares: the emitter, the budget and the configured limits."""

import codecs
import zlib
from collections.abc import Sequence
from dataclasses import dataclass

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
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.provenance import Locator

# Fixed structures that are the measured geometry and layout of the raster; every other table is
# something the file's writer or camera declares (EXIF, XMP, ICC, text, density, gamma, ...).
_MEASURED = frozenset({"IHDR", "acTL", "VP8X", "VP8", "VP8L", "ANIM", "PNM header"})


def _measured(name: str) -> bool:
    return name in _MEASURED or name.startswith(("SOF", "BITMAP", "OS22X"))


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
        values: Sequence[CellInput],
    ) -> RecordId | None:
        """A fixed structure as a table of one row: ``columns`` are the specification's names."""
        self.budget.entry()
        kind = AssertionKind.OBSERVED if _measured(name) else AssertionKind.STATED
        table = self.out.table(locator, Known(name), columns, name, kind)
        if table is not None:
            self.out.row(table, locator, 0, values)
        return table

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
        self, raw: bytes, more: bool, codec: str, locator: Sequence[Locator]
    ) -> CellInput:
        """A text as a cell: at most ``max_value_bytes`` of it, ``Unknown`` if not ``codec``.

        ``more`` says bytes follow ``raw`` that the caller did not read. A cut text is kept as far
        as it goes and ``image.value_not_copied`` cites it; the rest stays in the source's bytes.
        """
        keep = self.max_value_bytes
        if len(raw) > keep:
            raw, more = raw[:keep], True
        try:
            text = codecs.getincrementaldecoder(codec)().decode(raw, final=not more)
        except UnicodeDecodeError:
            self.out.finding(VALUE_UNREADABLE, locator, f"the text is not {codec}")
            return Unknown()
        if more:
            self.out.finding(
                VALUE_NOT_COPIED,
                locator,
                f"a text of more than {keep} bytes is cut to its first {keep} (max_value_bytes);"
                " the rest stays cited in the bytes",
                {"max_value_bytes": keep},
            )
        return text
