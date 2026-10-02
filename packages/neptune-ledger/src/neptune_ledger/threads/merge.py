"""Cross-clock merge onto a caller-named reference clock (ADR 0003 §3, ADR 0006 §8).

Only when the caller names a reference clock and ``ClockMapping`` ids. Clocks are nodes, usable
named mappings are undirected edges, and all arithmetic is exact (``Fraction``) until the final
``floor``/``ceil``. A mapping is usable iff it is affine and monotone increasing
(``f(t) = a·t + b`` with ``a > 0``). Paths to the reference rank by total bound, then by
mapping-id sequence; each entry takes the best-ranked path whose hops' validity windows hold its
whole interval, or stays in its native partition. The search for it follows only hops whose
window holds the interval, within a step budget (ADR 0010 §6). Stored ticks are never rewritten.
"""

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Final

from neptune_ledger.api.types import (
    CatalogFinding,
    MappedInterval,
    MappingPath,
    Partition,
    ThreadEntry,
)
from neptune_ledger.threads.order import native_key, partition_key, timed

# A validity window in one clock's ticks: ``(start, end)``, start inclusive and end exclusive
# (root ADR 0050 §3), ``None`` on an open side. A window of ``None`` cannot be checked, so no
# interval lies inside it.
Window = tuple[Fraction | int | None, Fraction | int | None] | None


@dataclass(frozen=True)
class ClockMapping:
    """A ``ClockMapping`` as the merge reads it: ``f(t) = slope·t + offset`` from ``source``
    ticks to ``target`` ticks, valid for source ticks in ``window``, with a residual ``bound`` in
    target ticks. ``unsupported`` says why the record cannot be read as such a function (a field
    that is not Known), which makes the mapping unusable; so does a slope that is not positive."""

    mapping_id: str
    source: str
    target: str
    slope: Fraction
    offset: Fraction
    bound: Fraction
    window: Window
    unsupported: str | None = None

    @property
    def usable(self) -> bool:
        return (
            self.unsupported is None
            and self.slope > 0
            and self.bound >= 0
            and self.source != self.target
        )


@dataclass(frozen=True)
class Hop:
    """One mapping walked forward or backward, from ``source`` to ``target``."""

    mapping_id: str
    source: str
    target: str
    slope: Fraction
    offset: Fraction
    bound: Fraction
    window: Window  # on ``source``

    def apply(self, t: Fraction) -> Fraction:
        return self.slope * t + self.offset


def hops(mapping: ClockMapping) -> tuple[Hop, Hop]:
    """The forward hop and its inverse ``f⁻¹(t) = (t - b) / a``, bound ``bound / a``, whose
    window is the forward window's image under ``f`` (ADR 0003 §3.2). ``f`` is increasing, so
    the image of a half-open window is half-open and an open side stays open."""
    a, b = mapping.slope, mapping.offset
    window = mapping.window
    image: Window = None
    if window is not None:
        lo, hi = window
        image = (None if lo is None else a * lo + b, None if hi is None else a * hi + b)
    forward = Hop(mapping.mapping_id, mapping.source, mapping.target, a, b, mapping.bound, window)
    backward = Hop(
        mapping.mapping_id, mapping.target, mapping.source, 1 / a, -b / a, mapping.bound / a, image
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
            if not _inside(lo, hi, hop.window):
                return None
            lo, hi = hop.apply(lo) - hop.bound, hop.apply(hi) + hop.bound
        return math.floor(lo), math.ceil(hi)


def _inside(lo: Fraction, hi: Fraction, window: Window) -> bool:
    """``[lo, hi]`` lies entirely inside the half-open ``window`` (ADR 0050 §3)."""
    if window is None:
        return False
    start, end = window
    return (start is None or lo >= start) and (end is None or hi < end)


def _edges(mappings: Sequence[ClockMapping]) -> dict[str, list[Hop]]:
    """The usable mappings as hops, both ways, per source clock, in mapping-id order."""
    edges: dict[str, list[Hop]] = {}
    for mapping in mappings:
        if mapping.usable:
            for hop in hops(mapping):
                edges.setdefault(hop.source, []).append(hop)
    for found in edges.values():
        found.sort(key=lambda h: (h.mapping_id.encode("utf-8"), h.target.encode("utf-8")))
    return edges


def paths(clock: str, reference: str, mappings: Sequence[ClockMapping]) -> list[Path]:
    """Every simple path of usable mappings from ``clock`` to ``reference``, best first.

    The specification of ADR 0003 §3.3 written out: exponential in the length of a chain of
    parallel mappings, so ``merge`` never calls it. The property tests use it as the oracle that
    ``merge``'s pruned search must agree with.
    """
    edges = _edges(mappings)

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


# The work one merge may do: a step is one hop whose window is checked against an interval. A
# search that runs out leaves its entry on its own clock with a mapping_out_of_range finding,
# never an exception or an unbounded read (ADR 0005 §5's budget; ADR 0010 §6).
MAX_ENTRY_STEPS: Final = 10_000
MAX_MERGE_STEPS: Final = 1_000_000
# How many of the paths an out-of-range entry tried its finding names.
MAX_PATHS_NAMED: Final = 16


@dataclass
class _Budget:
    left: int


@dataclass(frozen=True)
class _Search:
    """One entry's search: the best usable path and its interval, the path prefixes whose last
    hop's window did not hold the interval, and whether the step budget ran out."""

    best: tuple[Path, tuple[int, int]] | None
    tried: tuple[tuple[str, ...], ...]
    exhausted: bool


def _search(
    clock: str, reference: str, s: int, edges: Mapping[str, Sequence[Hop]], budget: _Budget
) -> _Search:
    """The highest-ranked usable path for an entry starting at ``s`` (ADR 0003 §3.3-3.4).

    A depth-first walk that follows a hop only when its validity window holds the interval as it
    stands, which is exactly what makes a path usable, so every complete walk is a usable path
    and the best-ranked of them is the answer ``paths`` + ``Path.interval`` give. Windows prune
    piecewise mappings (one per sync window) to the hops that cover the entry.
    """
    if clock == reference:
        return _Search((Path(()), (s, s)), (), False)
    best: tuple[Path, tuple[int, int]] | None = None
    tried: set[tuple[str, ...]] = set()
    steps = 0
    start = Fraction(s)
    stack: list[tuple[str, frozenset[str], tuple[Hop, ...], Fraction, Fraction]] = [
        (clock, frozenset({clock}), (), start, start)
    ]
    while stack:
        at, seen, used, lo, hi = stack.pop()
        for hop in reversed(edges.get(at, ())):
            if hop.target in seen:
                continue
            if steps >= MAX_ENTRY_STEPS or budget.left <= 0:
                return _Search(None, tuple(sorted(tried)), True)
            steps += 1
            budget.left -= 1
            if not _inside(lo, hi, hop.window):
                tried.add(tuple(h.mapping_id for h in (*used, hop)))
                continue
            path = (*used, hop)
            next_lo, next_hi = hop.apply(lo) - hop.bound, hop.apply(hi) + hop.bound
            if hop.target == reference:
                found = Path(path)
                if best is None or found.rank() < best[0].rank():
                    best = (found, (math.floor(next_lo), math.ceil(next_hi)))
                continue
            stack.append((hop.target, seen | {hop.target}, path, next_lo, next_hi))
    return _Search(best, tuple(sorted(tried)), False)


def _reaches(clock: str, reference: str, edges: Mapping[str, Sequence[Hop]]) -> bool:
    """Whether any path of usable mappings joins ``clock`` to ``reference``, windows aside."""
    seen, frontier = {clock}, [clock]
    while frontier:
        at = frontier.pop()
        if at == reference:
            return True
        for hop in edges.get(at, ()):
            if hop.target not in seen:
                seen.add(hop.target)
                frontier.append(hop.target)
    return False


def merge(
    partitions: Sequence[Partition], reference: str, mappings: Sequence[ClockMapping]
) -> tuple[tuple[Partition, ...], list[CatalogFinding]]:
    """Merge every clock partition that a usable path joins to ``reference`` (ADR 0003 §3.1-5).

    Entries that a path covers go to one ``merged`` partition, sorted by ``(lo, hi, clock key
    bytes, native key)`` and each carrying its interval and path. Entries of a clock that some
    path reaches but whose windows hold no path for them stay in their clock partition and get a
    ``mapping_out_of_range`` finding naming the paths tried; so do entries whose search runs past
    the step budget. Unusable mappings are reported ``unsupported_mapping``.
    """
    findings = [
        CatalogFinding(
            "unsupported_mapping",
            m.mapping_id,
            f"{m.unsupported or 'not an affine, increasing map between two clocks'}; not used",
        )
        for m in sorted(mappings, key=lambda m: m.mapping_id.encode("utf-8"))
        if not m.usable
    ]
    edges = _edges(mappings)
    budget = _Budget(MAX_MERGE_STEPS)
    reaches: dict[str, bool] = {}
    searched: dict[tuple[str, int], _Search] = {}
    merged: list[tuple[tuple[object, ...], ThreadEntry]] = []
    out: list[Partition] = []
    untimed: list[Partition] = []
    for partition in partitions:
        if partition.kind != "clock" or partition.clock_key is None:
            untimed.append(partition)
            continue
        clock = partition.clock_key
        if clock not in reaches:
            reaches[clock] = clock == reference or _reaches(clock, reference, edges)
        stay: list[ThreadEntry] = []
        for entry in partition.entries:
            if not reaches[clock]:
                stay.append(entry)
                continue
            world = timed(entry)
            assert world is not None
            ticks = world.start.ticks
            if (clock, ticks) not in searched:
                searched[clock, ticks] = _search(clock, reference, ticks, edges, budget)
            result = searched[clock, ticks]
            if result.best is None:
                stay.append(entry)
                findings.append(_out_of_range(entry, result))
                continue
            path, (lo, hi) = result.best
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


def _out_of_range(entry: ThreadEntry, search: _Search) -> CatalogFinding:
    named = sorted(search.tried, key=lambda ids: tuple(i.encode("utf-8") for i in ids))
    if search.exhausted:
        detail = (
            f"the path search stopped at its step budget ({MAX_ENTRY_STEPS} per entry,"
            f" {MAX_MERGE_STEPS} per merge); the entry stays on its clock"
        )
    else:
        detail = "no path's validity windows cover this entry"
    if len(named) > MAX_PATHS_NAMED:
        detail += f"; {MAX_PATHS_NAMED} of {len(named)} paths tried are named"
    return CatalogFinding(
        "mapping_out_of_range",
        entry.record_id,
        f"{detail} (package {entry.packages[0]})",
        paths_tried=tuple(MappingPath(ids) for ids in named[:MAX_PATHS_NAMED]) or None,
    )
