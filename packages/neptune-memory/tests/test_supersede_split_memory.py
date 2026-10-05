"""Split closures, bi-temporal findings and the resolver's config hash (ADR 0005)."""

from dataclasses import replace
from itertools import permutations

import pytest

from memory_schema_builders import BOOT_CLOCK, INFERRED, OBSERVED, STATED, at, claim, node
from neptune.model.time import DomainMismatchError
from neptune_memory.schema.claim import Claim, ClaimId
from neptune_memory.schema.interval import OPEN, Interval, Open, ledger_tx
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES
from neptune_memory.schema.supersede import (
    RESOLVER_ID,
    RESOLVER_VERSION,
    FindingCode,
    Resolution,
    as_of,
    assertions,
    is_closure,
    resolve,
    resolver_config,
    resolver_config_hash,
)

PRIORITIES = {"memory.a": 1, "memory.b": 2, "memory.test": 1}
ARM = node(NodeType.SENSOR, "ur10e-07/wrist-camera")
HUMANOID = node(NodeType.MACHINE, "humanoid-4")
BAYS = [node(NodeType.ZONE, f"bay-{n}") for n in range(5)]
CALS = [node(NodeType.CONFIGURATION, f"cal-{n}") for n in range(5)]

Span = tuple[int, int | Open]


def run(*items: Claim, priorities: dict[str, int] = PRIORITIES) -> Resolution:
    return resolve(items, CORE_PREDICATES, priorities)


def held(result: Resolution, obj: object, tx: int | None = None) -> list[Span]:
    """The valid intervals versions current at ``tx`` (default: now) hold for ``obj``, in order."""
    live = (
        [c for c in result.claims if c.is_current]
        if tx is None
        else as_of(result, ledger_tx(tx)).claims
    )
    spans = [
        (c.valid_from.ticks, c.valid_to if isinstance(c.valid_to, Open) else c.valid_to.ticks)
        for c in live
        if c.object == obj
    ]
    return sorted(spans, key=lambda s: s[0])


def located(obj: int, start: int, end: int | Open = OPEN, **kw: object) -> Claim:
    return claim(HUMANOID, "located_at", BAYS[obj], start, end, **kw)  # type: ignore[arg-type]


# --- Interval.minus ---------------------------------------------------------------------------


def span(start: int, end: int | Open = OPEN) -> Interval:
    return Interval(at(start), end if isinstance(end, Open) else at(end))


@pytest.mark.parametrize(
    ("cuts", "left"),
    [
        ([], [(0, 10)]),
        ([(10, 12)], [(0, 10)]),  # touching is not overlapping: half-open
        ([(2, 4)], [(0, 2), (4, 10)]),
        ([(0, 4)], [(4, 10)]),  # equal start
        ([(6, OPEN)], [(0, 6)]),
        ([(6, 8), (2, 4)], [(0, 2), (4, 6), (8, 10)]),  # unsorted cuts
        ([(0, 10)], []),
        ([(0, 5), (3, OPEN)], []),
    ],
)
def test_interval_minus(cuts: list[Span], left: list[Span]) -> None:
    result = span(0, 10).minus(span(*c) for c in cuts)
    assert result == tuple(span(*piece) for piece in left)


def test_interval_minus_on_open_intervals_and_other_clocks() -> None:
    assert span(0).minus([span(2, 4)]) == (span(0, 2), span(4))
    with pytest.raises(DomainMismatchError):
        span(0, 10).minus([Interval(at(2, BOOT_CLOCK), OPEN)])


# --- Split closures ---------------------------------------------------------------------------


@pytest.mark.parametrize("stated_first", [True, False])
def test_an_inferred_claims_uncontested_tail_survives(stated_first: bool) -> None:
    stated = located(0, 0, 3, tx=1 if stated_first else 2, kind=STATED)
    guess = located(1, 2, tx=2 if stated_first else 1, kind=INFERRED, ev=1)
    result = run(stated, guess)
    assert held(result, BAYS[0]) == [(0, 3)]
    assert held(result, BAYS[1]) == [(3, OPEN)]


def test_a_bounded_winner_does_not_erase_the_losers_later_evidence() -> None:
    long = claim(ARM, "has_calibration", CALS[0], 0, 10, tx=1, kind=OBSERVED)
    short = claim(ARM, "has_calibration", CALS[1], 2, 4, tx=2, kind=OBSERVED, ev=1)
    result = run(long, short)
    assert held(result, CALS[0]) == [(0, 2), (4, 10)]
    assert held(result, CALS[1]) == [(2, 4)]
    pieces = [c for c in result.claims if is_closure(c)]
    assert len({p.id for p in pieces}) == 2
    for piece in pieces:
        assert piece.supersedes == (long.id,)
        assert piece.recorded_at == 2 and piece.assertion_kind == OBSERVED
        assert piece.provenance.evidence == (*long.provenance.evidence, *short.provenance.evidence)
        assert (piece.provenance.consolidator_id, piece.provenance.consolidator_version) == (
            RESOLVER_ID,
            RESOLVER_VERSION,
        )
    assert {c.id: c for c in result.claims}[short.id].supersedes == (long.id,)


def test_equal_valid_from_keeps_the_part_after_the_winner() -> None:
    first = located(0, 0, 10, tx=1)
    second = located(1, 0, 4, tx=2, ev=1)  # full tie on rank and start: later arrival wins
    result = run(first, second)
    assert held(result, BAYS[1]) == [(0, 4)]
    assert held(result, BAYS[0]) == [(4, 10)]


def test_open_intervals_split_around_a_bounded_winner() -> None:
    guess = located(0, 0, tx=1, kind=INFERRED)
    seen = located(1, 2, 4, tx=2, kind=OBSERVED, ev=1)
    result = run(guess, seen)
    assert held(result, BAYS[0]) == [(0, 2), (4, OPEN)]


def test_three_way_overlap() -> None:
    a = located(0, 0, 10, tx=1)
    b = located(1, 2, 6, tx=2, ev=1)
    c = located(2, 4, 8, tx=3, ev=2)
    result = run(a, b, c)
    # A split piece competes with its original valid_from: c (from 4) beats a's [4, 10) piece.
    assert held(result, BAYS[0]) == [(0, 2), (8, 10)]
    assert held(result, BAYS[1]) == [(2, 4)]
    assert held(result, BAYS[2]) == [(4, 8)]


@pytest.mark.parametrize("guess_tx", [1, 5])
def test_several_winners_carve_one_loser_into_many_pieces(guess_tx: int) -> None:
    guess = located(0, 0, 20, tx=guess_tx, kind=INFERRED)
    winners = [located(n + 1, s, s + 2, tx=2 + n, ev=n + 1) for n, s in enumerate((2, 6, 10))]
    result = run(guess, *winners)
    # Carved one winner at a time (guess first) or all at once on arrival (guess last): the same.
    assert held(result, BAYS[0]) == [(0, 2), (4, 6), (8, 10), (12, 20)]
    for n, s in enumerate((2, 6, 10)):
        assert held(result, BAYS[n + 1]) == [(s, s + 2)]
    assert not result.findings  # partly current: not overridden on arrival


def test_a_fully_covered_arrival_is_overridden_and_says_by_whom() -> None:
    w1, w2 = located(1, 0, 5, tx=1), located(2, 5, tx=1, ev=1)
    guess = located(0, 2, 9, tx=2, kind=INFERRED, ev=2)
    result = run(w1, w2, guess)
    (finding,) = result.findings
    assert finding.code is FindingCode.OVERRIDDEN_ON_ARRIVAL
    assert (finding.claim, finding.others) == (guess.id, tuple(sorted((w1.id, w2.id))))
    assert (finding.recorded_at, finding.superseded_at) == (2, OPEN)
    assert held(result, BAYS[0]) == []


def test_split_semantics_are_order_free_and_idempotent() -> None:
    contested = [
        located(0, 0, 20, tx=1, kind=INFERRED),
        located(1, 2, 4, tx=1, consolidator="memory.a", ev=1),
        located(2, 3, 12, tx=2, consolidator="memory.b", ev=2),
        located(3, 0, 6, tx=2, kind=OBSERVED, ev=3),
        located(1, 15, tx=3, consolidator="memory.b", ev=4),
    ]
    expected = run(*contested)
    for order in permutations(contested):
        assert run(*order) == expected
    assert run(*expected.claims) == expected
    assert assertions(expected.claims) == assertions(contested)


def test_split_piece_ids_are_derived_not_assigned() -> None:
    long = located(0, 0, 10, tx=1)
    short = located(1, 2, 4, tx=2, ev=1)
    ids = sorted(c.id for c in run(long, short).claims if is_closure(c))
    assert ids == sorted(c.id for c in run(short, long).claims if is_closure(c))
    # Pieces of two corroborating claims with the same evidence never share an id.
    twin = located(0, 0, 10, tx=1, consolidator="memory.a")
    pieces = [c for c in run(long, twin, short).claims if is_closure(c)]
    assert len(pieces) == len({p.id for p in pieces}) == 4


# --- Lineage retirement -----------------------------------------------------------------------


def upgraded(c: Claim, version: str) -> Claim:
    return replace(c, provenance=replace(c.provenance, consolidator_version=version))


def test_a_retired_lineage_retires_every_split_piece() -> None:
    old = located(0, 0, 20, tx=1, consolidator="memory.a")
    cut = located(1, 5, 10, tx=2, consolidator="memory.b", ev=1)
    late = located(2, 14, 16, tx=3, consolidator="memory.b", ev=2)  # splits the tail piece too
    new = upgraded(located(3, 30, tx=3, consolidator="memory.a", ev=3), "2")
    for priorities in ({"memory.a": 1, "memory.b": 2}, {"memory.a": 2, "memory.b": 1}):
        result = run(old, cut, late, new, priorities=priorities)
        pieces = [c for c in result.claims if is_closure(c) and c.object == BAYS[0]]
        assert pieces  # the split happened
        assert all(p.superseded_at in (ledger_tx(2), ledger_tx(3)) for p in pieces)
        assert held(result, BAYS[0]) == []  # nothing of the retired lineage is current
        assert held(result, BAYS[0], tx=2) == [(0, 5), (10, 20)]


# --- Findings carry transaction order ---------------------------------------------------------


def test_clock_mismatch_findings_follow_the_versions_they_name() -> None:
    civil = located(0, 0, tx=1)
    boot = located(1, 7, tx=2, kind=OBSERVED, clock=BOOT_CLOCK, ev=1)
    moved = located(2, 5, tx=3, ev=2)  # narrows civil to [0, 5) at tx 3
    result = run(civil, boot, moved)
    by_id: dict[ClaimId, Claim] = {c.id: c for c in result.claims}
    (piece,) = [c for c in result.claims if is_closure(c)]
    found = {(f.claim, f.others, f.recorded_at, f.superseded_at) for f in result.findings}
    assert found == {
        (boot.id, (civil.id,), 2, 3),  # ends when civil's open version is superseded
        (piece.id, (boot.id,), 3, OPEN),  # the piece is the version recorded later
        (moved.id, (boot.id,), 3, OPEN),
    }
    assert all(f.code is FindingCode.CLOCK_MISMATCH for f in result.findings)
    assert as_of(result, ledger_tx(1)).findings == ()
    (then,) = as_of(result, ledger_tx(2)).findings
    assert by_id[then.claim].is_current and not by_id[then.others[0]].is_current
    assert len(as_of(result, ledger_tx(3)).findings) == 2
    assert [f.to_json()["recorded_at"] for f in result.findings] == [2, 3, 3]


# --- The resolver's config hash ---------------------------------------------------------------


def test_closure_config_hash_covers_priorities_and_vocabulary() -> None:
    base = resolver_config_hash(CORE_PREDICATES, PRIORITIES)
    assert resolver_config(CORE_PREDICATES, PRIORITIES)["vocabulary_version"] == 6
    assert resolver_config_hash(CORE_PREDICATES, {**PRIORITIES, "memory.a": 3}) != base
    wider = CORE_PREDICATES.extend(
        replace(CORE_PREDICATES.spec("located_at"), version=2, domain=frozenset(NodeType))
    )
    assert resolver_config_hash(wider, PRIORITIES) != base

    long, short = located(0, 0, 10, tx=1), located(1, 2, 4, tx=2, ev=1)
    one = run(long, short)
    other = run(long, short, priorities={**PRIORITIES, "memory.a": 9})
    closures = {c.id for c in one.claims if is_closure(c)}
    assert closures.isdisjoint(c.id for c in other.claims if is_closure(c))
    assertions_ids = {c.id for c in one.claims if not is_closure(c)}
    assert assertions_ids == {c.id for c in other.claims if not is_closure(c)}
    # A history resolved under one configuration is re-resolved from its assertions.
    with pytest.raises(ValueError, match="does not produce"):
        run(*one.claims, priorities={**PRIORITIES, "memory.a": 9})
    assert run(*assertions(one.claims), priorities={**PRIORITIES, "memory.a": 9}) == other


@pytest.mark.parametrize("bad", [True, 1.5, "1"])
def test_priorities_must_be_integers(bad: object) -> None:
    with pytest.raises(ValueError, match="integers"):
        run(located(0, 0, tx=1), priorities={**PRIORITIES, "memory.test": bad})  # type: ignore[dict-item]
