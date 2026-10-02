"""Cross-clock merge onto a caller-named reference clock (ADR 0003 §3, ADR 0006 §8).

Only when the caller names a reference clock and ``ClockMapping`` ids. Clocks are nodes, usable
named mappings are undirected edges, and all arithmetic is exact (``Fraction``) until the final
``floor``/``ceil``. A mapping is usable iff it is affine and monotone increasing
(``f(t) = a·t + b`` with ``a > 0``). Each clock ranks its simple paths to the reference by total
bound, then by mapping-id sequence; each entry takes the best path whose hops' validity windows
hold its whole interval, or stays in its native partition. Stored ticks are never rewritten.
"""

import math
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction

from neptune_ledger.api.types import (
    CatalogFinding,
    MappedInterval,
    MappingPath,
    Partition,
    ThreadEntry,
)
from neptune_ledger.threads.order import native_key, partition_key, timed


@dataclass(frozen=True)
class ClockMapping:
    """A ``ClockMapping`` as the merge reads it: ``f(t) = slope·t + offset`` from ``source``
    ticks to ``target`` ticks, valid for source ticks in ``window`` (inclusive), with a declared
    residual ``bound`` in target ticks (0 when none is declared). ``affine`` is False for any
    other function shape, which makes the mapping unusable."""

    mapping_id: str
    source: str
    target: str
    slope: Fraction
    offset: Fraction
    bound: Fraction
    window: tuple[int, int]
    affine: bool = True

    @property
    def usable(self) -> bool:
        return self.affine and self.slope > 0 and self.bound >= 0 and self.source != self.target


@dataclass(frozen=True)
class Hop:
    """One mapping walked forward or backward, from ``source`` to ``target``."""

    mapping_id: str
    source: str
    target: str
    slope: Fraction
    offset: Fraction
    bound: Fraction
    window: tuple[Fraction, Fraction]  # on ``source``

    def apply(self, t: Fraction) -> Fraction:
        return self.slope * t + self.offset


def hops(mapping: ClockMapping) -> tuple[Hop, Hop]:
    """The forward hop and its inverse ``f⁻¹(t) = (t - b) / a``, bound ``bound / a``, whose
    window is the forward window's image under ``f`` (ADR 0003 §3.2)."""
    a, b = mapping.slope, mapping.offset
    lo, hi = mapping.window
    forward = Hop(
        mapping.mapping_id,
        mapping.source,
        mapping.target,
        a,
        b,
        mapping.bound,
        (Fraction(lo), Fraction(hi)),
    )
    backward = Hop(
        mapping.mapping_id,
        mapping.target,
        mapping.source,
        1 / a,
        -b / a,
        mapping.bound / a,
        (a * lo + b, a * hi + b),
    )
    return forward, backward


@dataclass(frozen=True)
class Path:
    hops: tuple[Hop, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(h.mapping_id for h in self.hops)

    @property
    def total_bound(self) -> Fraction:
        """Each hop's bound carried into reference ticks by the slopes of the hops after it."""
        total, scale = Fraction(0), Fraction(1)
        for hop in reversed(self.hops):
            total += hop.bound * scale
            scale *= hop.slope
        return total

    def rank(self) -> tuple[Fraction, tuple[bytes, ...]]:
        return self.total_bound, tuple(i.encode("utf-8") for i in self.ids)

    def interval(self, s: int) -> tuple[int, int] | None:
        """``[s, s]`` through every hop, widened by each bound, or None when an interval falls
        outside a hop's validity window (no extrapolation, no clipping; ADR 0003 §3.4)."""
        lo = hi = Fraction(s)
        for hop in self.hops:
            if lo < hop.window[0] or hi > hop.window[1]:
                return None
            lo, hi = hop.apply(lo) - hop.bound, hop.apply(hi) + hop.bound
        return math.floor(lo), math.ceil(hi)


def paths(clock: str, reference: str, mappings: Sequence[ClockMapping]) -> list[Path]:
    """Every simple path of usable mappings from ``clock`` to ``reference``, best first."""
    edges: dict[str, list[Hop]] = {}
    for mapping in mappings:
        if mapping.usable:
            for hop in hops(mapping):
                edges.setdefault(hop.source, []).append(hop)

    def walk(at: str, seen: frozenset[str], used: tuple[Hop, ...]) -> Iterator[Path]:
        if at == reference:
            yield Path(used)
            return
        for hop in edges.get(at, ()):
            if hop.target not in seen:
                yield from walk(hop.target, seen | {hop.target}, (*used, hop))

    if clock == reference:
        return [Path(())]
    return sorted(walk(clock, frozenset({clock}), ()), key=Path.rank)


def merge(
    partitions: Sequence[Partition], reference: str, mappings: Sequence[ClockMapping]
) -> tuple[tuple[Partition, ...], list[CatalogFinding]]:
    """Merge every clock partition that a usable path joins to ``reference`` (ADR 0003 §3.1-5).

    Entries that a path covers go to one ``merged`` partition, sorted by ``(lo, hi, clock key
    bytes, native key)`` and each carrying its interval and path. Entries with paths but none
    usable stay in their clock partition and get a ``mapping_out_of_range`` finding naming the
    paths tried; unusable mappings are reported ``unsupported_mapping``.
    """
    findings = [
        CatalogFinding(
            "unsupported_mapping",
            m.mapping_id,
            "not an affine, monotone increasing mapping between two clocks; not used",
        )
        for m in sorted(mappings, key=lambda m: m.mapping_id.encode("utf-8"))
        if not m.usable
    ]
    ranked: dict[str, list[Path]] = {}
    merged: list[tuple[tuple[object, ...], ThreadEntry]] = []
    out: list[Partition] = []
    untimed: list[Partition] = []
    for partition in partitions:
        if partition.kind != "clock" or partition.clock_key is None:
            untimed.append(partition)
            continue
        clock = partition.clock_key
        if clock not in ranked:
            ranked[clock] = paths(clock, reference, mappings)
        stay: list[ThreadEntry] = []
        for entry in partition.entries:
            world = timed(entry)
            assert world is not None
            chosen = _first_usable(ranked[clock], world.start.ticks)
            if chosen is None:
                stay.append(entry)
                if ranked[clock]:
                    findings.append(_out_of_range(entry, ranked[clock]))
                continue
            path, (lo, hi) = chosen
            mapped = MappedInterval(reference, lo, hi, path.ids)
            key = (lo, hi, clock.encode("utf-8"), native_key(entry))
            merged.append((key, replace(entry, mapped=mapped)))
        if stay:
            out.append(Partition("clock", tuple(stay), clock))
    if merged:
        merged.sort(key=lambda pair: pair[0])
        out.append(Partition("merged", tuple(e for _, e in merged), reference))
    out.sort(key=partition_key)
    findings.sort(key=lambda f: (f.code, f.subject.encode("utf-8"), f.detail))
    return (*out, *untimed), findings


def _first_usable(ranked: Iterable[Path], s: int) -> tuple[Path, tuple[int, int]] | None:
    for path in ranked:
        interval = path.interval(s)
        if interval is not None:
            return path, interval
    return None


def _out_of_range(entry: ThreadEntry, tried: Sequence[Path]) -> CatalogFinding:
    return CatalogFinding(
        "mapping_out_of_range",
        entry.record_id,
        f"no path's validity windows cover this entry in package {entry.packages[0]}",
        paths_tried=tuple(MappingPath(p.ids) for p in tried if p.ids),
    )
