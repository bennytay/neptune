"""ADR 0003 §7's property tests over the pure thread order, resolver and merge (P1-P8).

Catalogs of 1-6 packages with distinct registration keys and 0-40 entries, drawn with forced
ties (equal starts, equal ends, one registration key for many entries), open ends, ends on other
clocks, the same record id in two packages, SemVer chains with prereleases, build metadata,
invalid versions and different adapters, and monotone and non-monotone mappings with validity
windows. ``derandomize=True`` keeps CI reproducible. P9 (the worked examples) is
test_ledger_threads.py, on the real catalog.
"""

import hashlib
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from hypothesis import given, settings
from hypothesis import strategies as st

from neptune.model.knowledge import Ambiguous, Candidate, Known, NotApplicable, NotCovered, Unknown
from neptune_ledger.api import codec
from neptune_ledger.api.types import (
    AsRegisteredBy,
    LatestTransform,
    Partition,
    Pinned,
    Preference,
    ThreadEntry,
    TimePoint,
    TransactionKey,
    WorldTime,
)
from neptune_ledger.threads import merge as merge_module
from neptune_ledger.threads.merge import ClockMapping, Window, hops, merge, paths
from neptune_ledger.threads.merge import Path as MergePath
from neptune_ledger.threads.order import (
    Chain,
    Member,
    chain_of,
    collapse,
    history_entries,
    lineage_sets,
    native_key,
    resolve,
    transaction_key,
    undominated,
    world_partitions,
)

PROFILE: Final = settings(derandomize=True, max_examples=200, deadline=None)
CLOCKS: Final = tuple(f"rec:sha256:{c * 64}" for c in "abcd")
SOURCES: Final = tuple(f"sha256:{c * 64}" for c in "ef")
# Transforms: (adapter chain), some comparable, some not; one with an invalid version.
CHAINS: Final[dict[str, Chain]] = {
    "rec:sha256:" + "1" * 64: (("urdf", "1.0.0"),),
    "rec:sha256:" + "2" * 64: (("urdf", "1.0.0+build.7"),),
    "rec:sha256:" + "3" * 64: (("urdf", "2.0.0-rc.1"),),
    "rec:sha256:" + "4" * 64: (("urdf", "2.0.0"),),
    "rec:sha256:" + "5" * 64: (("sdf", "9.0.0"),),
    "rec:sha256:" + "6" * 64: (("mcap", "0.3.0"), ("norm", "1.0.0")),
    "rec:sha256:" + "7" * 64: (("mcap", "0.4.0"), ("norm", "1.0.0")),
    "rec:sha256:" + "8" * 64: (("urdf", "v3"),),
}
TRANSFORMS: Final = tuple(sorted(CHAINS))
KINDS: Final = ("run", "stream", "calibration", "hardware_configuration", "machine")


@dataclass(frozen=True)
class Body:
    """What one record id states, the same in every package that holds it (ADR 0005 §2)."""

    record_id: str
    kind: str
    transform: str
    source: str
    world: Any


def _world(draw: Any, timed: bool) -> Any:
    shapes = ["closed", "open", "other_clock", "absent"]
    shape = draw(st.sampled_from(shapes if timed else ["none", "unknown", *shapes]))
    if shape == "none":
        return NotApplicable()
    if shape == "unknown":
        return Unknown()
    clock = draw(st.sampled_from(CLOCKS[:3]))
    s = draw(st.integers(0, 6))
    if shape == "closed":
        end: Any = Known(TimePoint(clock, s + draw(st.integers(0, 3))))
    elif shape == "other_clock":
        end = Known(TimePoint(draw(st.sampled_from([c for c in CLOCKS if c != clock])), 1))
    elif shape == "absent":
        end = NotCovered()
    else:
        end = Unknown()
    return Known(WorldTime(TimePoint(clock, s), end))


@st.composite
def catalogs(draw: Any, timed: bool = False) -> list[Member]:
    count = draw(st.integers(1, 6))
    seqs = sorted(draw(st.sets(st.integers(1, 50), min_size=count, max_size=count)))
    packages = [(f"sha256:{n:064x}", seq) for n, seq in enumerate(seqs)]
    bodies = [
        Body(
            f"rec:sha256:{n + 100:064x}",
            draw(st.sampled_from(KINDS)),
            draw(st.sampled_from(TRANSFORMS)),
            draw(st.sampled_from(SOURCES)),
            _world(draw, timed),
        )
        for n in range(draw(st.integers(0, 16)))
    ]
    pairs = draw(
        st.sets(
            st.tuples(st.integers(0, max(len(bodies) - 1, 0)), st.integers(0, count - 1)),
            max_size=40,
        )
    )
    out = []
    for body_index, package_index in sorted(pairs):
        if not bodies:
            break
        body = bodies[body_index]
        package, seq = packages[package_index]
        roles = draw(st.sets(st.sampled_from(["cites", "part_of", "subject"]), min_size=1))
        out.append(
            Member(
                package_id=package,
                record_id=body.record_id,
                kind=body.kind,
                roles=tuple(sorted(roles)),
                registration=TransactionKey(seq, f"2026-10-02T00:00:{seq:02d}.000000Z"),
                transform_id=body.transform,
                source=body.source,
                world=body.world,
            )
        )
    return out


def flat(partitions: Sequence[Partition]) -> list[tuple[str, tuple[str, ...]]]:
    return [(e.record_id, e.packages) for p in partitions for e in p.entries]


def as_json(partitions: Sequence[Partition]) -> bytes:
    return b"".join(codec.dumps(p) for p in partitions)


# --- P1-P6: order ------------------------------------------------------------------------------


@PROFILE
@given(catalogs(), st.randoms(use_true_random=False))
def test_p1_input_order_never_changes_the_result(members: list[Member], rnd: Any) -> None:
    shuffled = list(members)
    rnd.shuffle(shuffled)
    assert as_json(world_partitions(history_entries(shuffled))) == as_json(
        world_partitions(history_entries(members))
    )
    packages = sorted({m.package_id for m in members})
    preferences: list[Preference] = [LatestTransform(), *(AsRegisteredBy(p) for p in packages)]
    for preference in preferences:
        a = resolve(lineage_sets(members), preference, CHAINS)
        b = resolve(lineage_sets(shuffled), preference, CHAINS)
        assert a[0] == b[0]
        assert as_json(world_partitions(collapse(a[1]))) == as_json(
            world_partitions(collapse(b[1]))
        )


@PROFILE
@given(catalogs())
def test_p3_the_entry_order_is_a_strict_total_order(members: list[Member]) -> None:
    entries = history_entries(members)
    keys = [transaction_key(e) for e in entries]
    assert len(set(keys)) == len(keys), "no two distinct entries compare equal"
    for partition in world_partitions(entries):
        keys = [native_key(e) for e in partition.entries]
        assert len(set(keys)) == len(keys)
        assert keys == sorted(keys)
        assert all(not (a < b and b < a) for a in keys for b in keys), "antisymmetric"


@PROFILE
@given(catalogs())
def test_p4_every_partition_holds_one_clock_and_clocks_are_isolated(members: list[Member]) -> None:
    partitions = world_partitions(history_entries(members))
    kinds = [p.kind for p in partitions]
    assert "untimed" not in kinds[:-1]
    for partition in partitions:
        assert partition.entries, "never an empty partition"
        if partition.kind == "clock":
            assert {e.world.value.clock for e in partition.entries} == {partition.clock_key}  # type: ignore[union-attr]
    if not partitions or partitions[0].kind != "clock":
        return
    kept = partitions[0].clock_key
    alone = [m for m in members if isinstance(m.world, Known) and m.world.value.clock == kept]
    (only,) = world_partitions(history_entries(alone))
    assert only.entries == partitions[0].entries, "other clocks never move this clock's order"


@PROFILE
@given(catalogs(), st.integers(1, 50))
def test_p5_history_only_grows(members: list[Member], t1: int) -> None:
    earlier = flat(
        world_partitions(history_entries(m for m in members if m.registration.tx_seq <= t1))
    )
    later = flat(world_partitions(history_entries(members)))
    position = {item: i for i, item in enumerate(later)}
    assert [position[item] for item in earlier] == sorted(position[item] for item in earlier)


@PROFILE
@given(catalogs(), st.integers(1, 5), st.integers(0, 100))
def test_p6_registration_keys_only_break_ties(
    members: list[Member], scale: int, shift: int
) -> None:
    moved = [
        replace(
            m,
            registration=TransactionKey(
                m.registration.tx_seq * scale + shift, m.registration.tx_time
            ),
        )
        for m in members
    ]
    assert flat(world_partitions(history_entries(moved))) == flat(
        world_partitions(history_entries(members))
    )
    for partition in world_partitions(history_entries(members)):
        if partition.kind == "clock":
            starts = [e.world.value.start.ticks for e in partition.entries]  # type: ignore[union-attr]
            assert starts == sorted(starts), "distinct starts order by start, whatever the keys"


def test_p2_bytes_are_identical_across_processes_and_hash_seeds(tmp_path: Path) -> None:
    script = tmp_path / "order_bytes.py"
    script.write_text(
        "import hashlib, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from test_ledger_thread_order_properties import fixed_bytes\n"
        "print(hashlib.sha256(fixed_bytes()).hexdigest())\n"
    )
    here = str(Path(__file__).parent)
    digests = {
        subprocess.run(
            [sys.executable, str(script), here],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("0", "1", "4242")
    }
    assert len(digests) == 1
    assert digests == {hashlib.sha256(fixed_bytes()).hexdigest() + "\n"}


def fixed_bytes() -> bytes:
    """A fixed catalog with ties and three clocks, ordered and resolved, as canonical JSON."""
    members = _fixed()
    sets = lineage_sets(members)
    resolved, selected = resolve(sets, LatestTransform(), CHAINS)
    return (
        as_json(world_partitions(history_entries(members)))
        + as_json(world_partitions(collapse(selected)))
        + b"".join(codec.dumps(s) for s in resolved)
    )


def _fixed() -> list[Member]:
    out = []
    for n in range(24):
        clock = CLOCKS[n % 3]
        world = Known(WorldTime(TimePoint(clock, n % 4), Known(TimePoint(clock, n % 5 + 4))))
        out.append(
            Member(
                package_id=f"sha256:{n % 3:064x}",
                record_id=f"rec:sha256:{(n * 7) % 11:064x}",
                kind=KINDS[n % 5],
                roles=("cites",),
                registration=TransactionKey(1 + n % 3, "2026-10-02T00:00:00.000000Z"),
                transform_id=TRANSFORMS[n % len(TRANSFORMS)],
                source=SOURCES[n % 2],
                world=world if n % 4 else NotApplicable(),
            )
        )
    return list({(m.package_id, m.record_id): m for m in out}.values())


# --- P8: the resolver --------------------------------------------------------------------------


@PROFILE
@given(catalogs())
def test_p8_the_resolver(members: list[Member]) -> None:
    sets = lineage_sets(members)
    history = {(m.package_id, m.record_id) for m in members}
    packages = sorted({m.package_id for m in members})
    preferences: list[Preference] = [
        LatestTransform(),
        Pinned(TRANSFORMS[0]),
        *(AsRegisteredBy(p) for p in packages),
    ]
    for preference in preferences:
        resolved, selected = resolve(sets, preference, CHAINS)
        assert sorted((s.kind, s.source) for s in resolved) == sorted(sets), "each set once"
        assert {(m.package_id, m.record_id) for m in selected} <= history
        for lineage_set in resolved:
            members_of = sets[(lineage_set.kind, lineage_set.source)]
            match preference, lineage_set.resolution:
                case Pinned(transform_id=t), Known(value=v):
                    assert v == t
                case AsRegisteredBy(package_id=p), Known(value=v):
                    assert v in {m.transform_id for m in members_of if m.package_id == p}
                case LatestTransform(), Known(value=v):
                    assert undominated(lineage_set.transforms, CHAINS) == [v]
                case LatestTransform(), Ambiguous(candidates=candidates):
                    values = [c.value for c in candidates]
                    assert values == sorted(values, key=str.encode) and len(values) > 1
                    assert values == undominated(lineage_set.transforms, CHAINS)
                case _, resolution:
                    assert resolution == NotCovered() or isinstance(resolution, Ambiguous)
        if isinstance(preference, Pinned):
            assert {m.transform_id for m in selected} <= {preference.transform_id}


def test_p8_latest_transform_examples() -> None:
    v1a, v1b, v2 = (
        "rec:sha256:" + "a1" * 32,
        "rec:sha256:" + "b1" * 32,
        "rec:sha256:" + "c2" * 32,
    )
    chains: dict[str, Chain | None] = {
        v1a: (("ulog", "1.0.0"),),
        v1b: (("ulog", "1.0.0"),),
        v2: (("ulog", "2.0.0"),),
    }
    assert undominated([v1a, v1b, v2], chains) == [v2]
    assert undominated([v1a, v1b], chains) == sorted([v1a, v1b], key=str.encode)
    chains[v2] = None  # an upstream not registered: compares with nothing
    assert undominated([v1a, v2], chains) == sorted([v1a, v2], key=str.encode)
    assert undominated([TRANSFORMS[2], TRANSFORMS[3]], CHAINS) == [TRANSFORMS[3]], (
        "a release follows its prerelease"
    )
    assert undominated([TRANSFORMS[0], TRANSFORMS[1]], CHAINS) == [
        TRANSFORMS[0],
        TRANSFORMS[1],
    ], "build metadata has no precedence"
    assert sorted(undominated([TRANSFORMS[3], TRANSFORMS[4], TRANSFORMS[7]], CHAINS)) == sorted(
        [TRANSFORMS[3], TRANSFORMS[4], TRANSFORMS[7]]
    ), "another adapter, or a version that is not SemVer, is incomparable"
    assert undominated([TRANSFORMS[5], TRANSFORMS[6]], CHAINS) == [TRANSFORMS[6]]
    resolved, _ = resolve({("run", SOURCES[0]): []}, LatestTransform(), chains)
    assert resolved[0].resolution == NotCovered()
    assert Ambiguous((Candidate(v1a), Candidate(v1b))) == Ambiguous(
        (Candidate(v1a), Candidate(v1b))
    )


def test_chains_flatten_a_diamond_once_per_path_and_refuse_cycles() -> None:
    """A fusion transform whose two upstreams share an ancestor (ADR 0003 §4.4)."""
    adapters = {
        "a": ("mcap", "1.0.0"),
        "b": ("imu", "1.0.0"),
        "c": ("gps", "2.0.0"),
        "d": ("fuse", "0.1.0"),
    }
    upstream = {"b": ["a"], "c": ["a"], "d": ["b", "c"]}
    memo: dict[str, Chain | None] = {}
    assert chain_of("d", adapters, upstream, memo) == (
        ("mcap", "1.0.0"),
        ("imu", "1.0.0"),
        ("mcap", "1.0.0"),
        ("gps", "2.0.0"),
        ("fuse", "0.1.0"),
    )
    assert memo["b"] == (("mcap", "1.0.0"), ("imu", "1.0.0")), "shared work is kept"
    assert chain_of("d", adapters, {**upstream, "a": ["d"]}) is None, "a cycle has no chain"
    assert chain_of("d", {k: v for k, v in adapters.items() if k != "a"}, upstream) is None


# --- P7: merge ---------------------------------------------------------------------------------


@st.composite
def mapping_sets(draw: Any) -> list[ClockMapping]:
    out = []
    pairs = [(a, b) for a in CLOCKS[:3] for b in CLOCKS if a != b]
    for n in range(draw(st.integers(0, 6))):
        source, target = draw(st.sampled_from(pairs))
        lo = draw(st.integers(-8, 3))
        # Mostly wide windows, so entries merge (often through two hops); some narrow ones, so
        # intervals fall partly outside and paths become unusable.
        width = draw(st.one_of(st.integers(0, 4), st.integers(30, 80)))
        # Half-open, as root ADR 0050 §3 states windows; some sides open, some windows unknown.
        window: Window = (
            None if draw(st.integers(0, 9)) == 0 else lo,
            None if draw(st.integers(0, 9)) == 0 else lo + width,
        )
        if draw(st.integers(0, 14)) == 0:
            window = None
        out.append(
            ClockMapping(
                mapping_id=f"rec:sha256:{n + 900:064x}",
                source=source,
                target=target,
                slope=Fraction(draw(st.integers(-1, 4)), draw(st.integers(1, 3))),
                offset=Fraction(draw(st.integers(-6, 6)), draw(st.integers(1, 4))),
                bound=Fraction(draw(st.integers(0, 3)), draw(st.integers(1, 2))),
                window=window,
                unsupported=None if draw(st.integers(0, 4)) > 0 else "rate is unknown",
            )
        )
    return out


@settings(derandomize=True, max_examples=500, deadline=None)  # merges are the rarer draws
@given(catalogs(timed=True), mapping_sets(), st.sampled_from(CLOCKS[:3]))
def test_p7_merge(members: list[Member], mappings: list[ClockMapping], reference: str) -> None:
    native = world_partitions(history_entries(members))
    merged, findings = merge(native, reference, mappings)
    unusable = {m.mapping_id for m in mappings if not m.usable}
    assert {f.subject for f in findings if f.code == "unsupported_mapping"} == unusable
    out_of_range = {f.subject for f in findings if f.code == "mapping_out_of_range"}
    by_clock = {p.clock_key: p for p in native if p.kind == "clock"}
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for partition in merged:
        assert partition.entries
        if partition.kind != "merged":
            for entry in partition.entries:
                assert entry.mapped is None
                if partition.kind == "clock" and paths(
                    partition.clock_key or "", reference, mappings
                ):
                    assert entry.record_id in out_of_range
            continue
        assert partition.clock_key == reference
        keys = [(e.mapped.lo, e.mapped.hi) for e in partition.entries]  # type: ignore[union-attr]
        assert keys == sorted(keys)
        groups: dict[tuple[str, tuple[str, ...]], list[ThreadEntry]] = {}
        for entry in partition.entries:
            assert entry.mapped is not None
            world = entry.world.value  # type: ignore[union-attr]
            assert not set(entry.mapped.path) & unusable, "unsupported mappings are never used"
            if world.clock == reference:
                assert (entry.mapped.lo, entry.mapped.hi, entry.mapped.path) == (
                    world.start.ticks,
                    world.start.ticks,
                    (),
                )
            ranked = paths(world.clock, reference, mappings)
            usable = [p for p in ranked if p.interval(world.start.ticks) is not None]
            assert usable and usable[0].ids == entry.mapped.path, "the best usable path"
            assert usable[0].interval(world.start.ticks) == (entry.mapped.lo, entry.mapped.hi)
            groups.setdefault((world.clock, entry.mapped.path), []).append(entry)
            seen.add((entry.record_id, entry.packages))
        for (clock, _), group in groups.items():
            original = [
                e
                for e in by_clock[clock].entries
                if (e.record_id, e.packages) in {(g.record_id, g.packages) for g in group}
            ]
            assert [(g.record_id, g.packages) for g in group] == [
                (e.record_id, e.packages) for e in original
            ], "one clock through one path keeps its own order"
    assert sorted(flat(merged)) == sorted(flat(native)), "nothing is lost or invented"


@PROFILE
@given(mapping_sets(), st.integers(-50, 50))
def test_p7_inverting_a_mapping_returns_the_instant_exactly(
    mappings: list[ClockMapping], t: int
) -> None:
    for mapping in mappings:
        if mapping.usable:
            forward, backward = hops(mapping)
            assert backward.apply(forward.apply(Fraction(t))) == t
            if forward.window is None:
                assert backward.window is None, "an unknown window stays unknown"
                continue
            assert backward.window == tuple(
                None if side is None else forward.apply(Fraction(side)) for side in forward.window
            ), "the image of a half-open window; an open side stays open"


def test_p7_a_partly_covered_interval_makes_the_path_unusable() -> None:
    """Validity is checked on the interval as it stands before each hop, never clipped."""
    a, b, c = CLOCKS[:3]
    first = ClockMapping(
        "rec:sha256:" + "e1" * 32, a, b, Fraction(1), Fraction(0), Fraction(2), (0, 10)
    )
    second = ClockMapping(
        "rec:sha256:" + "e2" * 32, b, c, Fraction(1), Fraction(0), Fraction(0), (0, 10)
    )
    (path,) = paths(a, c, [first, second])
    assert path.interval(5) == (3, 7)
    assert path.interval(9) is None, "[7, 11] on b leaves the second window"
    assert path.interval(0) is None, "[-2, 2] on b leaves the second window"
    assert MergePath(()).interval(4) == (4, 4)


def test_p7_ties_go_to_the_smaller_mapping_ids_and_bounds_rank_first() -> None:
    a, b = CLOCKS[:2]
    ids = ["rec:sha256:" + d * 64 for d in "123"]
    loose = ClockMapping(ids[0], a, b, Fraction(1), Fraction(0), Fraction(5), (0, 100))
    tight_late = ClockMapping(ids[2], a, b, Fraction(1), Fraction(0), Fraction(1), (0, 100))
    tight_early = ClockMapping(ids[1], b, a, Fraction(1), Fraction(0), Fraction(1), (0, 100))
    ranked = paths(a, b, [loose, tight_late, tight_early])
    assert [p.ids for p in ranked] == [(ids[1],), (ids[2],), (ids[0],)]


# --- P7 at scale: the merge's search is pruned by windows and bounded --------------------------


def _entries_on(clock: str, starts: Sequence[int]) -> tuple[ThreadEntry, ...]:
    return tuple(
        ThreadEntry(
            f"rec:sha256:{j:064x}",
            "stream",
            ("subject",),
            ("sha256:" + "0" * 64,),
            TransactionKey(1, "2026-01-01T00:00:00Z"),
            "rec:sha256:" + "f" * 64,
            "sha256:" + "1" * 64,
            Known(WorldTime(TimePoint(clock, ticks), NotCovered())),
        )
        for j, ticks in enumerate(starts)
    )


def test_p7_piecewise_mappings_are_searched_only_where_their_windows_hold(
    monkeypatch: Any,
) -> None:
    """A sensor → host → PTP → GPS chain with one stated mapping per 1000-tick sync window (30
    per hop) and 2000 entries. Listing every path first made this 27 000 paths per entry; the
    windows leave one per entry. A budget of 100 steps per entry and 200 000 per merge holds."""
    monkeypatch.setattr(merge_module, "MAX_ENTRY_STEPS", 100)
    monkeypatch.setattr(merge_module, "MAX_MERGE_STEPS", 200_000)
    a, b, c, d = CLOCKS[:4]
    mappings = [
        ClockMapping(
            f"rec:sha256:{n:02x}{i:062x}",
            s,
            t,
            Fraction(1),
            Fraction(0),
            Fraction(0),
            (i * 1000, (i + 1) * 1000),
        )
        for n, (s, t) in enumerate(((a, b), (b, c), (c, d)))
        for i in range(30)
    ]
    entries = _entries_on(a, [j * 15 for j in range(2000)])
    merged, findings = merge([Partition("clock", entries, a)], d, mappings)
    assert findings == []
    (partition,) = merged
    assert partition.kind == "merged" and len(partition.entries) == 2000
    for entry in partition.entries:
        assert entry.mapped is not None
        window = entry.world.value.start.ticks // 1000  # type: ignore[union-attr]
        assert entry.mapped.path == tuple(f"rec:sha256:{n:02x}{window:062x}" for n in range(3))


def test_p7_a_search_past_its_budget_is_a_finding_not_a_stall() -> None:
    """Twelve hops of three parallel open-window mappings: 531 441 usable paths. The search
    stops at its step budget and the entry stays on its clock with a mapping_out_of_range
    finding; nothing raises and the read ends."""
    clocks = [f"rec:sha256:{0xC0 + i:064x}" for i in range(13)]
    mappings = [
        ClockMapping(
            f"rec:sha256:{i:032x}{j:032x}",
            clocks[i],
            clocks[i + 1],
            Fraction(1),
            Fraction(0),
            Fraction(0),
            (None, None),
        )
        for i in range(12)
        for j in range(3)
    ]
    entries = _entries_on(clocks[0], [5, 6])
    merged, findings = merge([Partition("clock", entries, clocks[0])], clocks[12], mappings)
    assert [p.kind for p in merged] == ["clock"]
    assert [f.code for f in findings] == ["mapping_out_of_range"] * 2
    assert all("step budget" in f.detail for f in findings)
    short = [m for m in mappings if m.source in clocks[:4]]  # three hops: 27 paths, all searched
    merged, findings = merge([Partition("clock", entries, clocks[0])], clocks[3], short)
    assert findings == [] and [p.kind for p in merged] == ["merged"]
    best = paths(clocks[0], clocks[3], short)[0]
    assert all(e.mapped and e.mapped.path == best.ids for e in merged[0].entries)
