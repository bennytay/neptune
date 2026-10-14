"""Times in their clock's own declared terms (ADR 0022): readable, never converted.

A packet carries Memory's ``clock_description`` of each clock its items' times are on
(``ContextPacket.clocks``, Memory ADR 0029). ``ClockBook`` reads them and renders a tick:

- **a wall clock** (a ``civil_time_zone`` record names it) whose ticks count from the Unix epoch
  at a stated tick length: the civil date and time those ticks count on that wall clock, at the
  precision the tick length states (``2026-09-14 14:32:38``), then the zone exactly as declared
  (``local wall time, America/New_York as declared``) or ``local wall time, zone not stated``.
  The ticks count from 1970-01-01T00:00:00 of the wall clock itself (root ADR 0023 §2), so this
  is reading the count, never moving it to UTC or to any other zone;
- **a POSIX clock** (Unix epoch, POSIX timescale, no zone record): the date and time the count
  names, labelled ``UTC (POSIX time)``, which is what that timescale counts;
- **anything else**: the ticks, with why they are not shown as a date in plain words (the epoch
  is the device's boot, the tick length is not stated, ...). A clock Memory does not describe
  says so.

Two clocks are never related here: each time is shown on its own clock with its own declared
zone, side by side. In a fact line a clock is its answer key (``on clock C1``); the answer's
Clocks list gives each key its full id, a short readable label (its time field and, with a source
index, the file it is read from: ``"Stopped" clock in "downtime_log.csv"``), what is declared
about it and the evidence that declares it. Source text in a label (a field, a file name, a scope)
is quoted.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Final

from neptune_memory.schema.claim import TypedLiteral
from neptune_memory.schema.clock_description import CIVIL_CLOCK_FIELD, ClockDescription

from neptune.model.knowledge import Ambiguous, Known, NotApplicable
from neptune.model.time import INT64_MIN, Epoch, Timescale

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from neptune_memory.schema.claim import Claim

    from neptune.model.ids import RecordId
    from neptune.model.provenance import EvidenceRef
    from neptune_context.sources import SourceIndex

_DAY: Final = 86_400
_EPOCH: Final = datetime.date(1970, 1, 1)
# A tick length finer than this many decimal places is shown as ticks.
_MAX_DIGITS: Final = 9
_EPOCH_WHY: Final = {
    Epoch.BOOT: "its ticks count from the recording device's boot, which no date names",
    Epoch.FIRST_SAMPLE: "its ticks count from its own first sample, which no date names",
    Epoch.SIMULATION_START: "its ticks count from the simulation's start, which no date names",
    Epoch.GPS: "its ticks count from the GPS epoch; only Unix-epoch counts are shown as dates",
}


def _tick_length(resolution: Fraction) -> str:
    """A tick length in plain words: ``1 s``, ``1 min``, ``1 day``, ``1 ms``, ``1/30 s``."""
    named = {
        Fraction(_DAY): "1 day",
        Fraction(3600): "1 h",
        Fraction(60): "1 min",
        Fraction(1): "1 s",
        Fraction(1, 1000): "1 ms",
        Fraction(1, 10**6): "1 µs",
        Fraction(1, 10**9): "1 ns",
    }
    if resolution in named:
        return named[resolution]
    return f"{resolution.numerator}/{resolution.denominator} s"


def _digits(resolution: Fraction) -> int | None:
    """Decimal places a tick length needs, or ``None`` when it is not a power of ten's part."""
    for digits in range(_MAX_DIGITS + 1):
        if (resolution * 10**digits).denominator == 1:
            return digits
    return None


def civil_text(ticks: int, resolution: Fraction) -> str | None:
    """The date and time ``ticks`` counts from 1970-01-01T00:00:00 on its own clock, 86,400 s
    a day, at the precision the tick length states; ``None`` when it cannot be shown exactly
    (a tick length that is no decimal part of a second, or a date outside years 1-9999)."""
    digits = _digits(resolution)
    if digits is None:
        return None
    seconds = Fraction(ticks) * resolution
    whole = seconds.numerator // seconds.denominator
    days, of_day = divmod(whole, _DAY)
    try:
        date = _EPOCH + datetime.timedelta(days=days)
    except OverflowError:
        return None
    if resolution % _DAY == 0:
        return date.isoformat()
    hours, rest = divmod(of_day, 3600)
    minutes, secs = divmod(rest, 60)
    if resolution % 60 == 0:
        return f"{date.isoformat()} {hours:02d}:{minutes:02d}"
    text = f"{date.isoformat()} {hours:02d}:{minutes:02d}:{secs:02d}"
    if digits:
        fraction = (seconds - whole) * 10**digits
        text += f".{int(fraction):0{digits}d}"
    return text


@dataclass(frozen=True)
class _Clock:
    """One described clock: its label, its description and the claim that states it."""

    label: str
    description: ClockDescription
    claim: Claim


def _zone_note(description: ClockDescription) -> str:
    zone = description.civil_time_zone
    if isinstance(zone, Known):
        return f"local wall time, {zone.value} as declared"
    if isinstance(zone, Ambiguous):
        names = " or ".join(str(c.value) for c in zone.candidates)
        return f"local wall time, zone declared ambiguously as {names}"
    return "local wall time, zone not stated"


def _unreadable(description: ClockDescription) -> str:
    """Why a clock's ticks are not shown as dates, in plain words."""
    epoch, resolution = description.epoch, description.resolution
    if not isinstance(resolution, Known):
        return "its tick length is not stated"
    if not isinstance(epoch, Known):
        return "its epoch is not stated"
    if epoch.value is not Epoch.UNIX:
        return _EPOCH_WHY.get(epoch.value, "its epoch names no date")
    if not description.is_wall_clock:
        timescale = description.timescale
        if isinstance(timescale, Known):
            return (
                f"it counts {timescale.value} time; only POSIX and wall clocks are shown as dates"
            )
        return "it states neither a timescale nor a zone, so UTC or local wall time is not known"
    return f"a {_tick_length(resolution.value)} tick is not a time of day"


class ClockBook:
    """The clocks of one packet, described and labelled. Deterministic: labels depend only on
    the packet's clock descriptions and the source index."""

    def __init__(
        self,
        clocks: Sequence[Claim],
        quote: Callable[[str], str],
        sources: SourceIndex | None = None,
    ) -> None:
        self._quote = quote
        described: list[tuple[RecordId, ClockDescription, Claim]] = []
        for claim in clocks:
            obj = claim.object
            if isinstance(obj, TypedLiteral) and isinstance(obj.value, ClockDescription):
                described.append((claim.subject.node_id, obj.value, claim))  # type: ignore[arg-type]
        bases = {
            clock: _label(description, claim.provenance.evidence, sources, quote)
            for clock, description, claim in described
        }
        counts: dict[str, int] = {}
        for base in bases.values():
            counts[base] = counts.get(base, 0) + 1
        seen: dict[str, int] = {}
        self._clocks: dict[RecordId, _Clock] = {}
        for clock, description, claim in sorted(described, key=lambda d: d[0]):
            label = bases[clock]
            if counts[label] > 1:
                seen[label] = seen.get(label, 0) + 1
                label = f"{label} #{seen[label]}"
            self._clocks[clock] = _Clock(label, description, claim)

    def label(self, clock: RecordId) -> str:
        """The clock's readable label; its record id when Memory does not describe it."""
        known = self._clocks.get(clock)
        return f"clock {clock}" if known is None else known.label

    def when(self, ticks: int, clock: RecordId) -> str:
        """One tick: readable in its clock's declared terms, or the bare tick (the Clocks list
        says why it is not a date: ``reading_note``)."""
        known = self._clocks.get(clock)
        if known is None:
            return f"tick {ticks}"
        if ticks == INT64_MIN:
            return "the start of the clock's time"
        text, note = self._reading(ticks, known.description)
        return f"tick {ticks}" if text is None else f"{text} ({note})"

    def _reading(self, ticks: int, description: ClockDescription) -> tuple[str | None, str]:
        epoch, resolution, timescale = (
            description.epoch,
            description.resolution,
            description.timescale,
        )
        if isinstance(epoch, Known) and epoch.value is Epoch.UNIX and isinstance(resolution, Known):
            if description.is_wall_clock:
                text = civil_text(ticks, resolution.value)
                if text is not None:
                    return text, _zone_note(description)
            elif isinstance(timescale, Known) and timescale.value is Timescale.POSIX:
                text = civil_text(ticks, resolution.value)
                if text is not None:
                    return f"{text} UTC", "POSIX time, as declared"
        return None, _unreadable(description)

    def span(self, clock: RecordId, start: int, end: int | None, on: str) -> str:
        """A claim's valid interval on ``clock`` (named ``on``): ``valid at T`` for one tick of a
        described clock (an instant, Memory ADR 0013), else ``valid from T until T'`` or
        ``valid from T, open-ended``. Both ends are on the one clock (Memory's Interval)."""
        first = self.when(start, clock)
        if end is None:
            return f"valid from {first} {on}, open-ended"
        if end == start + 1 and clock in self._clocks and start != INT64_MIN:
            return f"valid at {first} {on}"
        return f"valid from {first} until {self.when(end, clock)} {on}"

    def entry(self, clock: RecordId) -> tuple[str, tuple[EvidenceRef, ...]]:
        """What the Clocks list says about a clock: its readable label and what is declared
        about it, and the evidence that declares it (none when Memory does not describe it)."""
        known = self._clocks.get(clock)
        if known is None:
            return "not described in Memory, so its ticks are shown as ticks", ()
        text = f"the {known.label}: {describe(known.description, self._quote)}"
        return text, known.claim.provenance.evidence


def describe(description: ClockDescription, quote: Callable[[str], str]) -> str:
    """What is declared about a clock, in plain words; every state as it is."""
    resolution, epoch, timescale = description.resolution, description.epoch, description.timescale
    length = (
        f"{_tick_length(resolution.value)} ticks"
        if isinstance(resolution, Known)
        else "tick length not stated"
    )
    readable = (
        isinstance(epoch, Known) and epoch.value is Epoch.UNIX and isinstance(resolution, Known)
    )
    if description.is_wall_clock:
        zone = description.civil_time_zone
        declared = description.zone_declared_by
        by = f" by {quote(declared.value)}" if isinstance(declared, Known) else ""
        if isinstance(zone, Known):
            stated = f"zone {zone.value} as declared{by}"
        elif isinstance(zone, Ambiguous):
            names = " or ".join(str(c.value) for c in zone.candidates)
            stated = f"zone declared ambiguously{by} as {names}"
        else:
            stated = "zone not stated"
        text = f"local wall clock, {length}; {stated}"
    elif readable and isinstance(timescale, Known) and timescale.value is Timescale.POSIX:
        return f"POSIX clock (UTC), {length} from the Unix epoch"
    else:
        text = length
    shown = readable and _digits(resolution.value) is not None  # type: ignore[union-attr]
    if shown and description.is_wall_clock:
        return text
    return f"{text}; shown as ticks, not dates: {_unreadable(description)}"


def _label(
    description: ClockDescription,
    evidence: Sequence[EvidenceRef],
    sources: SourceIndex | None,
    quote: Callable[[str], str],
) -> str:
    """``"<field>" clock`` and, where known, ``of "<scope>"`` and ``in "<file>"``: what the
    records state about where the ticks are read from, never an invented name. A shared civil
    clock (Memory's ``CivilClock``) is named by its definition: ``shared POSIX clock (1 s)``."""
    if description.field == CIVIL_CLOCK_FIELD and isinstance(description.role, NotApplicable):
        timescale, resolution = description.timescale, description.resolution
        if isinstance(timescale, Known) and isinstance(resolution, Known):
            return f"shared {str(timescale.value).upper()} clock ({_tick_length(resolution.value)})"
    parts = [f"{quote(description.field)} clock"]
    if description.scope:
        parts.append(f"of {quote('/'.join(description.scope))}")
    if sources is not None:
        files = sorted(
            {path.rsplit("/", 1)[-1] for ref in evidence for path in sources.get(ref.source)}
        )
        if files:
            parts.append(f"in {' or '.join(quote(f) for f in files)}")
    return " ".join(parts)
