"""The two time axes of a claim (ADR 0002 §3).

Valid time is when the claim holds in the world: a half-open ``Interval`` of compiler
``Timestamp``s on one clock, exactly as declared. Nothing converts it to UTC or another clock;
ordering two clocks raises the compiler's ``DomainMismatchError``. Civil time shared across sources
is a ``CivilClock``, whose ``domain_id`` names the same timeline wherever it is declared.

Transaction time is when Memory learned it: ``LedgerTx``, the Ledger's commit sequence number.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from functools import cached_property
from typing import TYPE_CHECKING, Final, NewType

from neptune.identity.ids import record_id
from neptune.model.time import INT64_MAX, DomainMismatchError, Epoch, Timescale, Timestamp

if TYPE_CHECKING:
    from collections.abc import Iterable

    from neptune.model.ids import RecordId
    from neptune.model.jsonvalue import JsonObject, JsonValue

# The Ledger's transaction clock: a non-negative commit sequence number, totally ordered, assigned
# by the Ledger and never by a wall clock (ADR 0002 §3).
LedgerTx = NewType("LedgerTx", int)


def ledger_tx(value: int) -> LedgerTx:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"a Ledger transaction is an int, got {type(value).__name__}")
    if not 0 <= value <= INT64_MAX:
        raise ValueError(f"a Ledger transaction is in [0, 2^63): {value}")
    return LedgerTx(value)


@dataclass(frozen=True)
class Open:
    """An interval end that has not been reached: valid until further notice, or still current."""

    def to_json(self) -> JsonValue:
        return "open"


OPEN: Final = Open()


@dataclass(frozen=True)
class Interval:
    """``[start, end)`` on ``start``'s clock; ``end`` is on the same clock, or ``OPEN``.

    Never empty: ``start < end``. Comparing with an interval or instant on another clock raises
    ``DomainMismatchError``; relating clocks is a ``ClockAlignment`` record, never an operator.
    """

    start: Timestamp
    end: Timestamp | Open

    def __post_init__(self) -> None:
        if not isinstance(self.start, Timestamp):
            raise TypeError(f"start must be a Timestamp, got {type(self.start).__name__}")
        if isinstance(self.end, Open):
            return
        if not isinstance(self.end, Timestamp):
            raise TypeError(f"end must be a Timestamp or OPEN, got {type(self.end).__name__}")
        if self.end.domain_id != self.start.domain_id:
            raise DomainMismatchError(self.start.domain_id, self.end.domain_id)
        if not self.start < self.end:
            raise ValueError(f"an interval needs start < end: {self.start.ticks}, {self.end.ticks}")

    @property
    def domain_id(self) -> RecordId:
        return self.start.domain_id

    def contains(self, instant: Timestamp) -> bool:
        if not self.start <= instant:  # raises DomainMismatchError across clocks
            return False
        return isinstance(self.end, Open) or instant < self.end

    def overlaps(self, other: Interval) -> bool:
        if other.domain_id != self.domain_id:
            raise DomainMismatchError(self.domain_id, other.domain_id)
        starts_before_other_ends = isinstance(other.end, Open) or self.start < other.end
        other_starts_before_self_ends = isinstance(self.end, Open) or other.start < self.end
        return starts_before_other_ends and other_starts_before_self_ends

    def minus(self, others: Iterable[Interval]) -> tuple[Interval, ...]:
        """The parts of this interval no interval in ``others`` covers, in valid-time order.

        Empty when ``others`` cover it entirely. Every interval in ``others`` must be on this
        interval's clock (else ``DomainMismatchError``).
        """
        pieces = [self]
        for cut in sorted(others, key=lambda i: i.start.ticks):
            if cut.domain_id != self.domain_id:
                raise DomainMismatchError(self.domain_id, cut.domain_id)
            kept: list[Interval] = []
            for piece in pieces:
                if not piece.overlaps(cut):
                    kept.append(piece)
                    continue
                if piece.start < cut.start:
                    kept.append(Interval(piece.start, cut.start))
                if not isinstance(cut.end, Open) and (
                    isinstance(piece.end, Open) or cut.end < piece.end
                ):
                    kept.append(Interval(cut.end, piece.end))
            pieces = kept
        return tuple(pieces)

    def to_json(self) -> JsonObject:
        return {"end": self.end.to_json(), "start": self.start.to_json()}


_CIVIL_TIMESCALES: Final = frozenset({Timescale.UTC, Timescale.TAI, Timescale.GPS, Timescale.POSIX})
_ABSOLUTE_EPOCHS: Final = frozenset({Epoch.UNIX, Epoch.GPS})


@dataclass(frozen=True)
class CivilClock:
    """A civil timeline every source can share: an absolute timescale, epoch and tick length.

    A consolidator maps a source's ``TimestampDomain`` onto one only when that domain's timescale,
    epoch and resolution are all ``Known`` and match it; a civil time with no stated zone is not
    absolute and stays on its source's own domain. Two civil clocks with different resolutions are
    different clocks: converting is a provenanced derivative, not a comparison.
    """

    timescale: Timescale
    epoch: Epoch
    resolution: Fraction  # exact seconds per tick

    def __post_init__(self) -> None:
        if self.timescale not in _CIVIL_TIMESCALES:
            raise ValueError(f"not a civil timescale: {self.timescale!r}")
        if self.epoch not in _ABSOLUTE_EPOCHS:
            raise ValueError(f"not an absolute epoch: {self.epoch!r}")
        if not isinstance(self.resolution, Fraction) or self.resolution <= 0:
            raise ValueError(f"resolution must be a positive Fraction: {self.resolution!r}")

    @cached_property
    def domain_id(self) -> RecordId:
        """The clock's id, derived from its definition: equal clocks, equal ids, any source."""
        return record_id(
            "memory.civil_clock",
            {
                "epoch": str(self.epoch),
                "resolution": {
                    "denominator": self.resolution.denominator,
                    "numerator": self.resolution.numerator,
                },
                "timescale": str(self.timescale),
            },
        )

    def at(self, ticks: int) -> Timestamp:
        return Timestamp(ticks, self.domain_id)
