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
zone, side by side. Each clock gets a short readable label (its time field and, with a source
index, the file it is read from: ``"Stopped" clock of "downtime_log.csv"``) instead of its
record id; ``clock_lines`` lists every label with its full id, what is declared about it and the
evidence that declares it. Source text in a label (a field, a file name, a scope) is quoted.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Final

from neptune_memory.schema.claim import TypedLiteral
from neptune_memory.schema.clock_description import CIVIL_CLOCK_FIELD, ClockDescription
from neptune_memory.schema.interval import Open

from neptune.model.knowledge import Ambiguous, Known, NotApplicable
from neptune.model.time import INT64_MIN, Epoch, Timescale

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from neptune_memory.schema.claim import Claim
    from neptune_memory.schema.interval import Interval

    from neptune.model.ids import RecordId
    from neptune.model.provenance import EvidenceRef
    from neptune.model.time import Timestamp
    from neptune_context.sources import SourceIndex

CLOCKS: Final = (
    "Clocks (each time above is shown on its own clock, as that clock is declared; times are"
    " never converted between clocks or zones):"
)
_DAY: Final = 86_400
_EPOCH: Final = datetime.date(1970, 1, 1)
# A tick length finer than this many decimal places is shown as ticks.
_MAX_DIGITS: Final = 9
_EPOCH_WHY: Final = {
    Epoch.BOOT: "its ticks count from the recording device's boot, which no date names",
    Epoch.FIRST_SAMPLE: "its ticks count from its own first sample, which no date names",
    Epoch.SIMULATION_START: "its ticks count from the simulation's start, which no date names",
    Epoch.GPS: "its ticks count from the GPS epoch; Neptune shows only Unix-epoch counts as dates",
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
    """Why a clock's ticks are not shown as a date, in plain words."""
    epoch, resolution = description.epoch, description.resolution
    if not isinstance(resolution, Known):
        return "the clock's tick length is not stated"
    length = _tick_length(resolution.value)
    if not isinstance(epoch, Known):
        return f"the clock's epoch is not stated; 1 tick = {length}"
    if epoch.value is not Epoch.UNIX:
        return f"{_EPOCH_WHY.get(epoch.value, 'its epoch names no date')}; 1 tick = {length}"
    if not description.is_wall_clock:
        timescale = description.timescale
        if isinstance(timescale, Known):
            return (
                f"it counts {timescale.value} time, which Neptune shows as a date only for a"
                f" POSIX clock; 1 tick = {length}"
            )
        return (
            "the source states neither a timescale nor a zone for it, so whether it counts UTC"
            f" or a local wall clock is not known; 1 tick = {length}"
        )
    return f"a tick of {length} is not shown as a time of day"


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

    def at(self, at: Timestamp) -> str:
        """One instant: readable in its clock's declared terms, or ticks and why not."""
        return f"{self._when(at.ticks, at.domain_id)} on {self.label(at.domain_id)}"

    def _when(self, ticks: int, clock: RecordId) -> str:
        known = self._clocks.get(clock)
        if known is None:
            return f"tick {ticks} (Memory does not describe this clock)"
        if ticks == INT64_MIN:
            return "the start of the clock's time"
        text, note = self._reading(ticks, known.description)
        return f"tick {ticks} ({note})" if text is None else f"{text} ({note})"

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
        return None, f"not shown as a date: {_unreadable(description)}"

    def interval(self, valid: Interval) -> str:
        """A claim's valid interval: ``at T`` for one tick, else ``from T until T'`` or
        ``from T, open-ended``; then the clock it is on."""
        start = valid.start
        clock = start.domain_id
        end = valid.end
        where = self.label(clock)
        known = self._clocks.get(clock)
        if isinstance(end, Open):
            return f"valid from {self._when(start.ticks, clock)}, open-ended, on {where}"
        if end.ticks == start.ticks + 1 and known is not None and start.ticks != INT64_MIN:
            return f"valid at {self._when(start.ticks, clock)} on {where}"
        return (
            f"valid from {self._when(start.ticks, clock)} until"
            f" {self._when(end.ticks, clock)} on {where}"
        )

    def lines(self, cite: Callable[[tuple[EvidenceRef, ...]], str]) -> list[str]:
        """One line per described clock, by label: its full id, what is declared about it and
        the evidence that declares it."""
        out = []
        for clock, known in sorted(self._clocks.items(), key=lambda c: (c[1].label, c[0])):
            out.append(
                f"- {known.label} is {clock}: {describe(known.description, self._quote)}."
                f" {cite(known.claim.provenance.evidence)}"
            )
        return out

    def __bool__(self) -> bool:
        return bool(self._clocks)

    def evidence_refs(self) -> tuple[EvidenceRef, ...]:
        """Every evidence ref the clock lines cite, in the clock lines' order."""
        refs: dict[EvidenceRef, None] = {}
        for _, known in sorted(self._clocks.items(), key=lambda c: (c[1].label, c[0])):
            refs.update(dict.fromkeys(known.claim.provenance.evidence))
        return tuple(refs)


def describe(description: ClockDescription, quote: Callable[[str], str]) -> str:
    """What is declared about a clock, in plain words; every state as it is."""
    resolution, epoch, timescale = description.resolution, description.epoch, description.timescale
    length = (
        f"{_tick_length(resolution.value)} ticks"
        if isinstance(resolution, Known)
        else "tick length not stated"
    )
    if isinstance(epoch, Known) and epoch.value is Epoch.UNIX:
        origin = (
            "counted from 1970-01-01 00:00:00 on that wall clock"
            if description.is_wall_clock
            else "counted from the Unix epoch"
        )
    elif isinstance(epoch, Known):
        origin = f"counted from the {epoch.value} epoch"
    else:
        origin = "epoch not stated"
    parts = [f"{length}, {origin}"]
    if description.is_wall_clock:
        zone = description.civil_time_zone
        declared = description.zone_declared_by
        by = f" by {quote(declared.value)}" if isinstance(declared, Known) else ""
        if isinstance(zone, Known):
            parts.append(f"local wall time, zone {zone.value} as declared{by}")
        elif isinstance(zone, Ambiguous):
            names = " or ".join(str(c.value) for c in zone.candidates)
            parts.append(f"local wall time, zone declared ambiguously{by} as {names}")
        else:
            parts.append("local wall time, zone not stated by the source")
    elif isinstance(timescale, Known):
        parts.append(f"timescale {timescale.value}")
    else:
        parts.append("timescale not stated, and no civil zone is declared for it")
    return "; ".join(parts)


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
