"""What every reader of one source shares: the emitter, the scan budget, the limits, the bounds."""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from neptune.adapters.geometry._emit import (
    COUNT_MISMATCH,
    LIMIT_EXCEEDED,
    MALFORMED,
    NON_FINITE,
    TRUNCATED,
    Emitter,
    Prop,
    known,
    missing,
)
from neptune.adapters.geometry._scan import LimitHit, Scanner
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind
from neptune.model.provenance import ByteRange, Locator

AXES: Final = ("x", "y", "z")
FIRST: Final = 8  # keywords or reasons one finding names


@dataclass(frozen=True)
class Context:
    """One ``ingest`` call's state. ``out`` collects records and findings."""

    out: Emitter
    scan: Scanner
    size: int
    max_vertices: int
    max_header_bytes: int
    max_json_bytes: int
    max_json_depth: int
    max_entries: int
    max_value_bytes: int

    @property
    def whole(self) -> tuple[Locator, ...]:
        return (ByteRange(0, self.size),)

    def span(self, offset: int, length: int) -> tuple[Locator, ...]:
        return (ByteRange(offset, max(length, 0)),)

    def limit(self, hit: LimitHit, where: tuple[Locator, ...] | None = None) -> None:
        self.out.finding(
            LIMIT_EXCEEDED,
            where or self.whole,
            f"{hit.option} ({hit.limit}) stopped the scan: its counts and bounds are NotCovered"
            " and its dependencies are those read before it",
            {"limit": hit.limit, "option": hit.option},
        )

    def truncated(
        self, where: tuple[Locator, ...], what: str, details: Mapping[str, JsonValue] | None = None
    ) -> None:
        self.out.finding(TRUNCATED, where, f"the file ends inside {what}", details)

    def mismatch(
        self,
        where: tuple[Locator, ...],
        what: str,
        declared: int,
        measured: int,
    ) -> None:
        self.out.finding(
            COUNT_MISMATCH,
            where,
            f"{what} is declared as {declared} and measured as {measured}",
            {"declared": declared, "measured": measured, "what": what},
        )


class Problems:
    """Statements that break their format, counted by reason: one finding per reason."""

    def __init__(self) -> None:
        self._count: dict[str, int] = {}
        self._first: dict[str, tuple[Locator, ...] | int] = {}

    def add(self, reason: str, offset: int, where: tuple[Locator, ...] | None = None) -> None:
        """One statement at ``offset`` (or at ``where``, a finer citation) broke ``reason``."""
        self._count[reason] = self._count.get(reason, 0) + 1
        self._first.setdefault(reason, offset if where is None else where)

    def report(self, ctx: Context, code: str = MALFORMED) -> None:
        for reason in sorted(self._count):
            first = self._first[reason]
            where = ctx.span(first, 1) if isinstance(first, int) else first
            ctx.out.finding(
                code,
                where,
                f"{self._count[reason]} {reason}; the first is cited",
                {"count": self._count[reason], "reason": reason},
            )


class Bounds:
    """The axis-aligned bounds of the finite vertices added, as the file writes them."""

    def __init__(self) -> None:
        self.low = [math.inf] * 3
        self.high = [-math.inf] * 3
        self.count = 0
        self.skipped = 0
        self.first_skipped: int | None = None

    def add(self, x: float, y: float, z: float, offset: int) -> None:
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            self.skip(offset)
            return
        self.count += 1
        low, high = self.low, self.high
        if x < low[0]:
            low[0] = x
        if x > high[0]:
            high[0] = x
        if y < low[1]:
            low[1] = y
        if y > high[1]:
            high[1] = y
        if z < low[2]:
            low[2] = z
        if z > high[2]:
            high[2] = z

    def skip(self, offset: int) -> None:
        self.skipped += 1
        if self.first_skipped is None:
            self.first_skipped = offset

    def props(self, ctx: Context, where: tuple[Locator, ...], *, complete: bool) -> list[Prop]:
        """``bounds_min`` and ``bounds_max`` (observed), or why there are none: ``NotCovered`` when
        the scan was cut short, ``NotApplicable`` when no vertex is finite."""
        kind = AssertionKind.OBSERVED
        if self.skipped and self.first_skipped is not None:
            ctx.out.finding(
                NON_FINITE,
                ctx.span(self.first_skipped, 1),
                f"{self.skipped} vertices have NaN or infinite coordinates: not in the bounds",
                {"count": self.skipped},
            )
        if not complete:
            return [missing("bounds_min", "not_covered", where, kind),
                    missing("bounds_max", "not_covered", where, kind)]  # fmt: skip
        if not self.count:
            return [missing("bounds_min", "not_applicable", where, kind),
                    missing("bounds_max", "not_applicable", where, kind)]  # fmt: skip
        return [
            known("bounds_min", self.low, where, kind),
            known("bounds_max", self.high, where, kind),
        ]


def measured_count(
    name: str, value: int, where: tuple[Locator, ...], *, complete: bool, kind: AssertionKind
) -> Prop:
    """A count the adapter made by reading: ``NotCovered`` if the scan did not finish."""
    if complete:
        return known(name, (value,), where, kind)
    return missing(name, "not_covered", where, kind)


def clean_text(text: str, limit: int) -> str | None:
    """``text`` if a record can hold it as a name: non-empty, valid Unicode (a JSON string may hold
    a lone surrogate), no control character and at most ``limit`` bytes; else ``None``."""
    if not text.strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return None
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return text if len(encoded) <= limit else None


def bytes_text(data: bytes, limit: int) -> str | None:
    """``data`` as a name (``clean_text``), ``None`` if it is not UTF-8 or not usable."""
    try:
        return clean_text(data.decode("utf-8"), limit)
    except UnicodeDecodeError:
        return None


def operand(data: bytes, keyword_length: int) -> tuple[bytes, int]:
    """What follows the keyword that starts ``data`` (after its leading whitespace), stripped, and
    where that starts in ``data``."""
    lead = len(data) - len(data.lstrip())
    rest = data[lead + keyword_length :]
    stripped = rest.lstrip()
    return stripped.rstrip(), lead + keyword_length + len(rest) - len(stripped)


def floats(tokens: Iterable[bytes]) -> list[float] | None:
    try:
        return [float(token) for token in tokens]
    except ValueError:
        return None
