"""Build withdrawal (ADR 0007 §5, ADR 0016 §2): the resolver given the builds behind its claims.

P1, P2, P4 and P5 of ``test_supersede_properties_memory.py`` hold with builds in the input, and:

P9 withdrawal: every current version is a version of an assertion that the latest build of its
   consolidator emitted.
P10 completeness: every ``many`` assertion the latest build of its consolidator emitted has a
   current version, including one an earlier build withdrew (a restatement).

Histories are drawn as builds: at each transaction each consolidator may run, emitting a subset
of a fixed pool of claims, so claims appear, disappear and come back; ``memory.a`` may move to a
new version every two transactions.
"""

from dataclasses import replace
from random import Random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from memory_schema_builders import (
    BOOT_CLOCK,
    CONFIG,
    INFERRED,
    OBSERVED,
    SECONDS,
    STATED,
    claim,
    node,
)
from neptune.identity.canonical_json import dumps
from neptune.model.ids import RecordId
from neptune.model.knowledge import Knowledge, Known, Unknown
from neptune_memory.schema.claim import Claim, ClaimId
from neptune_memory.schema.interval import OPEN, CivilClock, Open, ledger_tx
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES, Cardinality
from neptune_memory.schema.supersede import (
    Build,
    LineageError,
    Resolution,
    _contest,
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
CONFIDENCES: list[Knowledge[float]] = [Known(0.6), Unknown()]
CLOCKS: list[CivilClock | RecordId] = [SECONDS, SECONDS, SECONDS, BOOT_CLOCK]
MAX_TX = 4
AMR, AUV = MACHINES  # a humanoid and an AUV: no morphology assumed
History = tuple[list[Claim], list[Build]]


@st.composite
def pooled(draw: st.DrawFn) -> Claim:
    one = draw(st.booleans())
    start = draw(st.integers(0, 6))
    kind = draw(st.sampled_from([OBSERVED, STATED, INFERRED]))
    return claim(
        draw(st.sampled_from(MACHINES)),
        "located_at" if one else "governed_by",
        draw(st.sampled_from(SITES if one else POLICIES)),
        start,
        draw(st.one_of(st.just(OPEN), st.integers(start + 1, 9))),
        tx=0,
        kind=kind,
        confidence=draw(st.sampled_from(CONFIDENCES)) if kind == INFERRED else None,
        consolidator=draw(st.sampled_from(sorted(PRIORITIES))),
        ev=draw(st.integers(0, 2)),
        clock=draw(st.sampled_from(CLOCKS)),
    )


def emit(item: Claim, tx: int, version: str) -> Claim:
    provenance = replace(item.provenance, consolidator_version=version)
    return replace(item, recorded_at=ledger_tx(tx), provenance=provenance)


@st.composite
def histories(draw: st.DrawFn) -> History:
    pool = draw(st.lists(pooled(), max_size=7))
    upgrading = draw(st.booleans())
    claims: list[Claim] = []
    builds: list[Build] = []
    for tx in range(MAX_TX + 1):
        for cid in sorted(PRIORITIES):
            if not draw(st.booleans()):
                continue
            version = str(1 + tx // 2) if upgrading and cid == "memory.a" else "1"
            mine = [c for c in pool if c.provenance.consolidator_id == cid]
            chosen = [emit(c, tx, version) for c in mine if draw(st.booleans())]
            claims.extend(chosen)
            ids = tuple(sorted({c.id for c in chosen}))
            builds.append(Build(cid, version, CONFIG, ledger_tx(tx), ids))
    return claims, builds


def run(claims: list[Claim] | tuple[Claim, ...], builds: list[Build]) -> Resolution:
    return resolve(claims, CORE_PREDICATES, PRIORITIES, builds)


def as_bytes(result: Resolution) -> bytes:
    return dumps(
        {
            "claims": [c.to_json() for c in result.claims],
            "findings": [f.to_json() for f in result.findings],
        }
    )


def latest_builds(builds: list[Build], tx: int = MAX_TX) -> dict[str, Build]:
    latest: dict[str, Build] = {}
    for build in sorted(builds, key=lambda b: b.recorded_at):
        if build.recorded_at <= tx:
            latest[build.consolidator_id] = build
    return latest


def roots(resolution: Resolution) -> dict[ClaimId, Claim]:
    """The assertion each version is a version of: closures and restatements followed back."""
    history = {c.id: c for c in resolution.claims}
    out: dict[ClaimId, Claim] = {}
    for version in resolution.claims:
        root = version
        while is_closure(root):
            root = history[root.supersedes[0]]
        out[version.id] = root
    return out


@settings(derandomize=True, max_examples=300)
@given(histories(), st.randoms(use_true_random=False))
def test_p1_order_free_with_builds(history: History, rnd: Random) -> None:
    claims, builds = history
    shuffled, reordered = list(claims), list(builds)
    rnd.shuffle(shuffled)
    rnd.shuffle(reordered)
    assert as_bytes(run(shuffled, reordered)) == as_bytes(run(claims, builds))


@settings(derandomize=True, max_examples=300)
@given(histories())
def test_p2_idempotent_with_builds(history: History) -> None:
    claims, builds = history
    once = run(claims, builds)
    assert run(once.claims, builds) == once


@settings(derandomize=True, max_examples=300)
@given(histories())
def test_p4_as_of_sees_history_with_builds(history: History) -> None:
    claims, builds = history
    resolution = run(claims, builds)
    for tx in range(MAX_TX + 1):
        seen = as_of(resolution, ledger_tx(tx))
        prefix = run(
            [c for c in claims if c.recorded_at <= tx], [b for b in builds if b.recorded_at <= tx]
        )
        assert seen.claims == tuple(c for c in prefix.claims if c.is_current)
        assert seen.findings == tuple(f for f in prefix.findings if f.is_current)


@settings(derandomize=True, max_examples=300)
@given(histories())
def test_p5_current_claims_never_contradict_with_builds(history: History) -> None:
    live = [c for c in run(*history).claims if c.is_current]
    for i, a in enumerate(live):
        for b in live[i + 1 :]:
            if CORE_PREDICATES.spec(a.predicate).cardinality is Cardinality.MANY:
                continue
            same_fact = (a.subject, a.predicate) == (b.subject, b.predicate)
            same_clock = a.valid_from.domain_id == b.valid_from.domain_id
            if same_fact and same_clock and a.object != b.object:
                assert not a.valid.overlaps(b.valid), (a.to_json(), b.to_json())


@settings(derandomize=True, max_examples=300)
@given(histories())
def test_p9_every_current_version_was_emitted_by_its_latest_build(history: History) -> None:
    claims, builds = history
    resolution = run(claims, builds)
    root_of = roots(resolution)
    for tx in range(MAX_TX + 1):
        latest = latest_builds(builds, tx)
        for version in as_of(resolution, ledger_tx(tx)).claims:
            root = root_of[version.id]
            build = latest[root.provenance.consolidator_id]
            assert lineage_of(root) == build.lineage
            assert root.id in build.claims


@settings(derandomize=True, max_examples=300)
@given(histories())
def test_p10_every_emitted_many_claim_is_current(history: History) -> None:
    claims, builds = history
    resolution = run(claims, builds)
    root_of = roots(resolution)
    for tx in range(MAX_TX + 1):
        current = {root_of[v.id].id for v in as_of(resolution, ledger_tx(tx)).claims}
        by_id = {c.id: c for c in claims}
        for build in latest_builds(builds, tx).values():
            for claim_id in build.claims:
                if CORE_PREDICATES.spec(by_id[claim_id].predicate).cardinality is Cardinality.MANY:
                    assert claim_id in current


# --- Worked examples ----------------------------------------------------------------------------


def build_of(tx: int, *claims: Claim, cid: str = "memory.a", version: str = "1") -> Build:
    return Build(cid, version, CONFIG, ledger_tx(tx), tuple(sorted({c.id for c in claims})))


def at(item: Claim, tx: int) -> Claim:
    return replace(item, recorded_at=ledger_tx(tx))


POLICY = claim(AMR, "governed_by", POLICIES[0], 0, tx=1, consolidator="memory.a")


def test_a_claim_the_next_build_does_not_emit_is_withdrawn_at_that_build() -> None:
    resolution = run([POLICY], [build_of(1, POLICY), build_of(2)])
    (version,) = resolution.claims
    assert version.id == POLICY.id and version.superseded_at == 2
    assert as_of(resolution, ledger_tx(1)).claims == (replace(version, superseded_at=OPEN),)
    assert as_of(resolution, ledger_tx(2)).claims == ()


def test_without_builds_nothing_is_withdrawn() -> None:
    (version,) = resolve([POLICY], CORE_PREDICATES, PRIORITIES).claims
    assert version.is_current


def test_a_claim_emitted_again_after_withdrawal_is_restated_never_reopened() -> None:
    builds = [build_of(1, POLICY), build_of(2), build_of(3, POLICY)]
    resolution = run([POLICY, at(POLICY, 3)], builds)
    original, restated = resolution.claims
    assert (original.id, original.recorded_at, original.superseded_at) == (POLICY.id, 1, 2)
    assert is_closure(restated) and restated.supersedes == (POLICY.id,)
    assert (restated.recorded_at, restated.superseded_at) == (3, OPEN)
    assert restated.content_json()["object"] == POLICY.content_json()["object"]
    assert restated.provenance.evidence == POLICY.provenance.evidence
    assert [c.id for c in as_of(resolution, ledger_tx(2)).claims] == []
    # A second withdrawal and restatement never repeats an id.
    again = [*builds, build_of(4), build_of(5, POLICY)]
    history = run([POLICY, at(POLICY, 3), at(POLICY, 5)], again).claims
    assert len({c.id for c in history}) == len(history) == 3


def test_a_withdrawn_winner_frees_what_it_cut() -> None:
    """The operator's bay 4 cut the log's dock from t=5; when the bay 4 claim is withdrawn the
    dock holds its whole interval again, from the withdrawing build on."""
    dock = claim(AUV, "located_at", SITES[0], 0, tx=1, kind=OBSERVED, consolidator="memory.a")
    bay = claim(AUV, "located_at", SITES[1], 5, tx=1, kind=OBSERVED, consolidator="memory.b")
    builds = [build_of(1, dock), build_of(1, bay, cid="memory.b"), build_of(2, cid="memory.b")]
    resolution = run([dock, bay], builds)
    before = as_of(resolution, ledger_tx(1)).claims
    assert {(c.object, c.valid_from.ticks, c.valid_to) for c in before} == {
        (SITES[0], 0, SECONDS.at(5)),
        (SITES[1], 5, OPEN),
    }
    (after,) = as_of(resolution, ledger_tx(2)).claims
    assert (after.object, after.valid_from.ticks, after.valid_to, after.recorded_at) == (
        SITES[0],
        0,
        OPEN,
        2,
    )
    assert roots(resolution)[after.id].id == dock.id


def test_an_unchanged_fact_keeps_its_versions_when_a_rival_is_withdrawn_elsewhere() -> None:
    """Withdrawing a claim that cut nothing re-versions nothing."""
    dock = claim(AUV, "located_at", SITES[0], 0, 4, tx=1, consolidator="memory.a")
    pier = claim(AUV, "located_at", SITES[1], 6, tx=1, consolidator="memory.b")
    builds = [build_of(1, dock), build_of(1, pier, cid="memory.b"), build_of(2, cid="memory.b")]
    resolution = run([dock, pier], builds)
    assert [c.id for c in resolution.claims if c.is_current] == [dock.id]


def test_an_empty_build_of_a_new_lineage_retires_the_old_one() -> None:
    resolution = run([POLICY], [build_of(1, POLICY), build_of(2, version="2")])
    (version,) = resolution.claims
    assert version.superseded_at == 2


@pytest.mark.parametrize(
    ("claims", "builds", "message"),
    [
        ([POLICY], [build_of(1, POLICY), build_of(1, POLICY)], "two builds"),
        ([], [build_of(1, POLICY)], "names claims"),
        ([at(POLICY, 2)], [build_of(1, POLICY), build_of(2, POLICY)], "names claims"),
        ([POLICY], [build_of(1)], "outside a build"),
        ([POLICY, at(POLICY, 2)], [build_of(1, POLICY)], "outside a build"),
        ([POLICY], [build_of(1, POLICY, version="2")], "two lineages"),
    ],
    ids=["twice", "unknown", "not-yet-recorded", "not-emitted", "unbuilt-tx", "other-lineage"],
)
def test_builds_that_disagree_with_the_claims_are_refused(
    claims: list[Claim], builds: list[Build], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        run(claims, builds)


def test_an_empty_build_cannot_bring_a_replaced_lineage_back() -> None:
    builds = [build_of(1, POLICY), build_of(2, version="2"), build_of(3)]
    with pytest.raises(LineageError) as caught:
        run([POLICY], builds)
    assert caught.value.code == "lineage_reuse"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"consolidator_id": "memory.supersede"},
        {"claims": ("claim:sha256:" + "0" * 64, "claim:sha256:" + "0" * 64)},
        {"claims": ["claim:sha256:" + "0" * 64]},
        {"claims": ("nope",)},
        {"recorded_at": -1},
    ],
    ids=["resolver", "duplicate", "list", "bad-id", "negative-tx"],
)
def test_a_malformed_build_is_refused(kwargs: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "consolidator_id": "memory.a",
        "version": "1",
        "config_hash": CONFIG,
        "recorded_at": ledger_tx(1),
        "claims": (),
        **kwargs,
    }
    with pytest.raises((TypeError, ValueError)):
        Build(**fields)  # type: ignore[arg-type]


def test_findings_end_with_the_versions_they_name() -> None:
    """A clock mismatch between a withdrawn claim and a standing one ends at the withdrawal."""
    boot = claim(AMR, "located_at", SITES[0], 0, tx=1, consolidator="memory.a", clock=CLOCKS[3])
    civil = claim(AMR, "located_at", SITES[1], 0, tx=1, consolidator="memory.b")
    builds = [build_of(1, boot), build_of(1, civil, cid="memory.b"), build_of(2)]
    (finding,) = run([boot, civil], builds).findings
    assert finding.recorded_at == 1 and finding.superseded_at == 2
    assert not isinstance(finding.superseded_at, Open)


def test_a_build_that_skips_transactions_withdraws_at_its_own() -> None:
    """The humanoid's policy holds through transaction 2, when its consolidator does not run."""
    item = claim(AMR, "governed_by", POLICIES[1], 0, tx=1, consolidator="memory.b")
    resolution = run([item], [build_of(1, item, cid="memory.b"), build_of(3, cid="memory.b")])
    assert resolution.claims[0].superseded_at == 3


@settings(derandomize=True, max_examples=300)
@given(st.lists(pooled(), max_size=8))
def test_replacement_contests_exactly_as_first_placement(pool: list[Claim]) -> None:
    """``_contest``, which re-placement runs, gives each assertion the pieces ``resolve`` placed."""
    items = [replace(c, recorded_at=ledger_tx(i % 3)) for i, c in enumerate(pool)]
    resolution = resolve(items, CORE_PREDICATES, PRIORITIES)
    root_of = roots(resolution)
    distinct = {c.id: c for c in assertions(items)}.values()
    facts = {(c.subject, c.predicate) for c in distinct if c.predicate == "located_at"}
    for fact in facts:
        standing = sorted(
            (c for c in distinct if (c.subject, c.predicate) == fact),
            key=lambda c: arrival_key(c, PRIORITIES),
        )
        expected = {
            root.id: sorted(dumps(p.to_json()) for p in pieces)
            for root, pieces in ((r, _contest(standing).get(r.id, [])) for r in standing)
        }
        placed: dict[ClaimId, list[bytes]] = {r.id: [] for r in standing}
        for version in resolution.claims:
            if version.is_current and (version.subject, version.predicate) == fact:
                placed[root_of[version.id].id].append(dumps(version.valid.to_json()))
        assert {k: sorted(v) for k, v in placed.items()} == expected
