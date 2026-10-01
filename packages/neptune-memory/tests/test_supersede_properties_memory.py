"""The superseding determinism specification (ADR 0002 §4 as superseded by ADR 0005), as properties.

P1 order-free: the history depends on the set of claims, not their order (byte-identical JSON).
P2 idempotent: resolving a resolved history returns it unchanged.
P3 nothing deleted: every assertion appears in the history with its content intact.
P4 as_of is history: the versions current at tx are exactly what resolving only the claims
   recorded by tx makes current.
P5 consistent: no two current claims on one ``one`` predicate, subject and clock overlap with
   different objects.
P6 total order: arrival is (recorded_at, priority, id); a full tie is impossible.
P7 uncontested parts survive (split closures): without a lineage upgrade, every instant of every
   ``one`` assertion stays held by a current version on its fact and clock.
P8 retirement: no current version, split pieces included, belongs to a replaced lineage.

Claim sets come in two kinds: stable lineages, and ones where ``memory.a`` is upgraded as
transactions advance, so retirement interleaves with splitting.
"""

from dataclasses import replace
from itertools import permutations
from random import Random

from hypothesis import given, settings
from hypothesis import strategies as st

from memory_schema_builders import BOOT_CLOCK, INFERRED, OBSERVED, SECONDS, STATED, claim, node
from neptune.identity.canonical_json import dumps
from neptune.model.ids import RecordId
from neptune.model.knowledge import Knowledge, Known, Unknown
from neptune_memory.schema.claim import Claim, ClaimId
from neptune_memory.schema.interval import OPEN, CivilClock, Open, ledger_tx
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES, Cardinality
from neptune_memory.schema.supersede import (
    Lineage,
    Resolution,
    ResolutionFinding,
    arrival_key,
    as_of,
    assertions,
    is_closure,
    lineage_of,
    resolve,
)

PRIORITIES = {"memory.a": 1, "memory.b": 2}
MACHINES = [node(NodeType.MACHINE, "humanoid-1"), node(NodeType.MACHINE, "auv-9")]
SITES = [node(NodeType.SITE, s) for s in ("dock", "pier", "plant")]
POLICIES = [node(NodeType.POLICY, p) for p in ("speed-limit", "geofence")]
MAX_TX = 4
CONFIDENCES: list[Knowledge[float]] = [Known(0.6), Unknown()]
# Mostly one civil clock, sometimes a boot clock: mismatches must be refused, not coerced.
CLOCKS: list[CivilClock | RecordId] = [SECONDS, SECONDS, SECONDS, BOOT_CLOCK]


@st.composite
def claims(draw: st.DrawFn) -> Claim:
    one = draw(st.booleans())
    start = draw(st.integers(0, 6))
    kind = draw(st.sampled_from([OBSERVED, STATED, INFERRED]))
    return claim(
        draw(st.sampled_from(MACHINES)),
        "located_at" if one else "governed_by",
        draw(st.sampled_from(SITES if one else POLICIES)),
        start,
        draw(st.one_of(st.just(OPEN), st.integers(start + 1, 9))),
        tx=draw(st.integers(0, MAX_TX)),
        kind=kind,
        confidence=draw(st.sampled_from(CONFIDENCES)) if kind == INFERRED else None,
        consolidator=draw(st.sampled_from(sorted(PRIORITIES))),
        ev=draw(st.integers(0, 2)),
        clock=draw(st.sampled_from(CLOCKS)),
    )


@st.composite
def upgrading_claims(draw: st.DrawFn) -> Claim:
    """A claim whose ``memory.a`` lineage moves to a new version every two transactions."""
    item = draw(claims())
    if item.provenance.consolidator_id != "memory.a":
        return item
    version = str(1 + item.recorded_at // 2)
    return replace(item, provenance=replace(item.provenance, consolidator_version=version))


STABLE_SETS = st.lists(claims(), max_size=8)
CLAIM_SETS = st.one_of(STABLE_SETS, st.lists(upgrading_claims(), max_size=8))
HORIZON = 12  # past every bounded valid_to the strategy draws


def run(items: list[Claim] | tuple[Claim, ...]) -> Resolution:
    return resolve(items, CORE_PREDICATES, PRIORITIES)


def as_bytes(result: Resolution) -> bytes:
    return dumps(
        {
            "claims": [c.to_json() for c in result.claims],
            "findings": [f.to_json() for f in result.findings],
        }
    )


def clear(claims: tuple[Claim, ...]) -> list[Claim]:
    return sorted((replace(c, superseded_at=OPEN) for c in claims), key=lambda c: c.id)


def clear_findings(findings: tuple[ResolutionFinding, ...]) -> list[ResolutionFinding]:
    cleared = (replace(f, superseded_at=OPEN) for f in findings)
    return sorted(cleared, key=lambda f: (f.claim, f.code, f.others))


@settings(derandomize=True, max_examples=300)
@given(CLAIM_SETS, st.randoms(use_true_random=False))
def test_p1_order_free(items: list[Claim], rnd: Random) -> None:
    shuffled = list(items)
    rnd.shuffle(shuffled)
    assert as_bytes(run(shuffled)) == as_bytes(run(items))


def test_p1_order_free_exhaustively_on_a_contested_set() -> None:
    amr = MACHINES[0]
    contested = [
        claim(amr, "located_at", SITES[0], 0, tx=1, consolidator="memory.a"),
        claim(amr, "located_at", SITES[1], 0, tx=1, consolidator="memory.b"),
        claim(amr, "located_at", SITES[2], 3, 8, tx=1, kind=INFERRED, consolidator="memory.a"),
        claim(amr, "located_at", SITES[0], 5, tx=0, consolidator="memory.b", ev=1),
        claim(amr, "located_at", SITES[1], 2, tx=2, kind=OBSERVED, consolidator="memory.a"),
    ]
    expected = as_bytes(run(contested))
    for order in permutations(contested):
        assert as_bytes(run(list(order))) == expected


@settings(derandomize=True, max_examples=300)
@given(CLAIM_SETS)
def test_p2_idempotent(items: list[Claim]) -> None:
    once = run(items)
    assert run(once.claims) == once
    assert run([*items, *items]) == once  # duplicates are one claim
    assert assertions(once.claims) == assertions(items)


@settings(derandomize=True, max_examples=300)
@given(CLAIM_SETS)
def test_p3_nothing_deleted(items: list[Claim]) -> None:
    history = {c.id: c for c in run(items).claims}
    for item in items:
        kept = history[item.id]
        assert kept.content_json() == item.content_json()
        assert kept.recorded_at <= item.recorded_at
    for version in history.values():
        assert set(version.supersedes) <= history.keys()
        if is_closure(version):
            (narrowed,) = version.supersedes
            assert history[narrowed].superseded_at == version.recorded_at
            root = root_of(version, history)
            assert root.id in {i.id for i in items}
            assert version.provenance.evidence[: len(root.provenance.evidence)] == (
                root.provenance.evidence
            )
            assert version.object == root.object and version.assertion_kind == root.assertion_kind
            assert root.valid.minus([version.valid]) != (root.valid,)  # a piece of the root
            assert not version.valid.minus([root.valid])


def root_of(version: Claim, history: dict[ClaimId, Claim]) -> Claim:
    """The assertion a closure version is a piece of: follow ``supersedes`` past closures."""
    while is_closure(version):
        (narrowed,) = version.supersedes
        version = history[narrowed]
    return version


@settings(derandomize=True, max_examples=300)
@given(CLAIM_SETS)
def test_p4_as_of_sees_history(items: list[Claim]) -> None:
    resolution = run(items)
    for tx in range(MAX_TX + 1):
        seen = as_of(resolution, ledger_tx(tx))
        prefix = run([c for c in items if c.recorded_at <= tx])
        assert clear(seen.claims) == clear(tuple(c for c in prefix.claims if c.is_current))
        # Findings replay too: as_of shows exactly the findings a replay to tx leaves active.
        live = [f for f in prefix.findings if isinstance(f.superseded_at, Open)]
        assert clear_findings(seen.findings) == clear_findings(tuple(live))


@settings(derandomize=True, max_examples=300)
@given(CLAIM_SETS)
def test_p5_current_claims_never_contradict(items: list[Claim]) -> None:
    live = [c for c in run(items).claims if c.is_current]
    for i, a in enumerate(live):
        for b in live[i + 1 :]:
            if CORE_PREDICATES.spec(a.predicate).cardinality is Cardinality.MANY:
                continue
            same_fact = (a.subject, a.predicate) == (b.subject, b.predicate)
            same_clock = a.valid_from.domain_id == b.valid_from.domain_id
            if same_fact and same_clock and a.object != b.object:
                assert not a.valid.overlaps(b.valid), (a.to_json(), b.to_json())


@settings(derandomize=True, max_examples=200)
@given(CLAIM_SETS)
def test_p6_arrival_order_is_total(items: list[Claim]) -> None:
    distinct = {c.id: c for c in items}.values()
    keys = [arrival_key(c, PRIORITIES) for c in distinct]
    assert len(set(keys)) == len(keys)


def ticks_held(claims: list[Claim], fact: tuple[object, str, str]) -> set[int]:
    held: set[int] = set()
    for c in claims:
        if (c.subject, c.predicate, c.valid_from.domain_id) == fact:
            end = HORIZON if isinstance(c.valid_to, Open) else c.valid_to.ticks
            held.update(range(c.valid_from.ticks, end))
    return held


@settings(derandomize=True, max_examples=300)
@given(STABLE_SETS)
def test_p7_uncontested_parts_survive(items: list[Claim]) -> None:
    live = [c for c in run(items).claims if c.is_current]
    for item in items:
        if CORE_PREDICATES.spec(item.predicate).cardinality is Cardinality.MANY:
            continue
        fact = (item.subject, item.predicate, item.valid_from.domain_id)
        end = HORIZON if isinstance(item.valid_to, Open) else item.valid_to.ticks
        assert set(range(item.valid_from.ticks, end)) <= ticks_held(live, fact)


@settings(derandomize=True, max_examples=300)
@given(st.lists(upgrading_claims(), max_size=8))
def test_p8_replaced_lineages_keep_nothing_current(items: list[Claim]) -> None:
    history = {c.id: c for c in run(items).claims}
    latest: dict[str, Lineage] = {}
    for item in sorted(items, key=lambda c: c.recorded_at):
        latest[item.provenance.consolidator_id] = lineage_of(item)
    for version in history.values():
        if version.is_current:
            root = root_of(version, history)
            assert lineage_of(root) == latest[root.provenance.consolidator_id]
