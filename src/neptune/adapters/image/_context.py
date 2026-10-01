"""What every parser of one source shares: the emitter, the budget and the configured limits."""

import zlib
from collections.abc import Sequence
from dataclasses import dataclass

from neptune.adapters.image._emit import LIMIT_EXCEEDED, TRUNCATED, CellInput, Emitter
from neptune.adapters.image._space import Budget, LimitHit, Truncated
from neptune.model.ids import RecordId
from neptune.model.knowledge import Known
from neptune.model.provenance import Locator


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
        table = self.out.table(locator, Known(name), columns, name)
        if table is not None:
            self.out.row(table, locator, 0, values)
        return table

    def inflate(self, data: bytes) -> bytes:
        """``data`` as a zlib stream inflated to at most ``max_metadata_bytes``.

        Raises ``LimitHit`` past the limit (a decompression bomb costs at most the limit) and
        ``zlib.error`` for a damaged stream; a stream cut short is also ``zlib.error``.
        """
        inflater = zlib.decompressobj()
        out = inflater.decompress(data, self.max_metadata_bytes + 1)
        if len(out) > self.max_metadata_bytes:
            raise LimitHit("max_metadata_bytes", self.max_metadata_bytes)
        if not inflater.eof:
            raise zlib.error("the zlib stream ends before its end marker")
        return out
