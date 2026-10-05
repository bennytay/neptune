"""Ambiguous links, human assertions and retraction in the identity consolidator (ADR 0008).

- An ``IdentityLink`` the compiler marked ``Ambiguous`` (its right side, or a shared identifier)
  yields ``same_as_candidate`` pairs, one claim each way per candidate, each citing that
  candidate's own evidence; never ``same_as`` and never a collapse to one candidate.
- A person's ``same_identity`` assertion grounds ``same_as`` while no effective ``retract`` names
  its declared id; a retraction of a retraction restores it; a loop is undecided and reported.
- ``distinct_identity`` suppresses candidates between its ids and contests a ``same_as``.
- A stated instant on a clock that declares itself civil lands on the shared ``CivilClock``.
"""

from collections.abc import Mapping, Sequence
from fractions import Fraction

from memory_identity_records import (
    STATED,
    Record,
    assertion,
    at,
    civil_domain,
    ledger,
    link,
    thread,
    window,
)
from neptune.identity.ids import record_id
from neptune.model.assertion import AssertionType
from neptune.model.ids import LogicalId
from neptune.model.time import Epoch, Timescale
from neptune_memory.consolidate.base import Consolidation, run_consolidator
from neptune_memory.consolidate.identity import (
    SAME_AS,
    SAME_AS_CANDIDATE,
    IdentityConsolidator,
    node_ref,
    same_as_candidates,
)
from neptune_memory.schema.claim import Claim
from neptune_memory.schema.interval import OPEN, CivilClock, LedgerTx, ledger_tx
from neptune_memory.schema.nodes import NodeType

SAME, DISTINCT, RETRACT = (
    AssertionType.SAME_IDENTITY,
    AssertionType.DISTINCT_IDENTITY,
    AssertionType.RETRACT,
)
MACHINE = NodeType.MACHINE
TX1 = ledger_tx(1)

# A humanoid fleet: the maintenance log names one unit by a controller slot that two units used.
SLOT = LogicalId("controller", "slot-b")
UNIT_1 = LogicalId("serial", "H1-0001")
UNIT_2 = LogicalId("serial", "H1-0002")
UNIT_3 = LogicalId("serial", "H1-0003")


def _fleet(*extra: Record) -> dict[str, list[Record]]:
    return {
        "pkg-units": [thread(UNIT_1, "unit 1 log"), thread(UNIT_2, "unit 2 log")],
        "pkg-unit-3": [thread(UNIT_3, "unit 3 log")],
        "pkg-maint": [thread(SLOT, "maintenance.log"), *extra],
    }


def _run(packages: Mapping[str, Sequence[Record]], tx: LedgerTx = TX1) -> Consolidation:
    return run_consolidator(IdentityConsolidator(), ledger(packages), (), {}, recorded_at=tx)


def _of(result: Consolidation, predicate: str) -> list[Claim]:
    return [c for c in result.claims if c.predicate == predicate]


def _pairs(claims: Sequence[Claim]) -> set[tuple[str, str]]:
    return {(c.subject.node_id, c.object.node_id) for c in claims}  # type: ignore[union-attr]


def _codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


# --- Ambiguous links ---------------------------------------------------------------------------

AMBIGUOUS = link("maintenance.log line 12", SLOT, (UNIT_1, UNIT_2))


def test_an_ambiguous_link_is_a_candidate_each_way_per_candidate_never_same_as() -> None:
    result = _run(_fleet(AMBIGUOUS))
    assert not _of(result, SAME_AS) and not result.findings
    candidates = _of(result, SAME_AS_CANDIDATE)
    slot, one, two = "controller:slot-b", "serial:H1-0001", "serial:H1-0002"
    assert _pairs(candidates) == {(slot, one), (one, slot), (slot, two), (two, slot)}
    for claim in candidates:
        assert claim.assertion_kind is STATED
        assert claim.provenance.records == (AMBIGUOUS["id"],)
    # Each candidate cites the line and its own place in it: the evidence for that reading.
    by_pair = {(c.subject.node_id, c.object.node_id): c.provenance.evidence for c in candidates}  # type: ignore[union-attr]
    assert by_pair[(slot, one)] != by_pair[(slot, two)]
    assert by_pair[(slot, one)] == by_pair[(one, slot)]
    readings = same_as_candidates(result.claims, node_ref(MACHINE, SLOT))
    assert readings == (
        node_ref(MACHINE, SLOT),
        node_ref(MACHINE, UNIT_1),
        node_ref(MACHINE, UNIT_2),
    )  # the distinct reading and both candidates: nothing collapsed


def test_an_ambiguous_shared_identifier_makes_a_known_right_side_a_candidate() -> None:
    shared = link(
        "unit 1 log header",
        SLOT,
        UNIT_1,
        identifier=(LogicalId("mac", "0a:01"), LogicalId("mac", "0a:07")),
    )
    result = _run(_fleet(shared))
    assert not _of(result, SAME_AS)
    assert _pairs(_of(result, SAME_AS_CANDIDATE)) == {
        ("controller:slot-b", "serial:H1-0001"),
        ("serial:H1-0001", "controller:slot-b"),
    }


def test_a_candidate_already_joined_by_same_as_is_not_repeated() -> None:
    confirmed = assertion("ASR-10", SAME, (SLOT, UNIT_1), authored_at=at(400))
    result = _run(_fleet(AMBIGUOUS, confirmed))
    assert _pairs(_of(result, SAME_AS)) == {("controller:slot-b", "serial:H1-0001")}
    assert _pairs(_of(result, SAME_AS_CANDIDATE)) == {
        ("controller:slot-b", "serial:H1-0002"),
        ("serial:H1-0002", "controller:slot-b"),
    }


def test_a_candidate_with_no_thread_is_a_finding_and_the_others_stay() -> None:
    ghost = LogicalId("serial", "H1-9999")
    result = _run(_fleet(link("maintenance.log line 13", SLOT, (UNIT_1, ghost))))
    assert _codes(result) == ["identity.dangling_link"]
    assert len(_of(result, SAME_AS_CANDIDATE)) == 2


def test_a_window_with_an_end_but_no_start_its_subject_can_place_is_a_finding() -> None:
    other_clock = record_id("test.clock", {"name": "maintenance-pc"})
    timed = link(
        "maintenance.log line 14",
        SLOT,
        UNIT_1,
        validity=window(None, at(50, other_clock), clock=other_clock),
    )
    result = _run(_fleet(timed))
    assert not result.claims
    assert _codes(result) == ["identity.untimeable_window"]


# --- Assertions --------------------------------------------------------------------------------


def test_a_same_identity_over_three_ids_joins_each_to_the_lowest() -> None:
    said = assertion("ASR-11", SAME, (UNIT_3, SLOT, UNIT_2), authored_at=at(5))
    result = _run(_fleet(said))
    assert _pairs(_of(result, SAME_AS)) == {
        ("controller:slot-b", "serial:H1-0002"),
        ("controller:slot-b", "serial:H1-0003"),
    }


def test_record_ids_in_a_scope_name_evidence_and_are_not_read_as_things() -> None:
    a_record = record_id("run", {"n": "unit 1 run"})
    said = assertion("ASR-12", SAME, (a_record, UNIT_1, SLOT), authored_at=at(5))
    result = _run(_fleet(said))
    assert len(_of(result, SAME_AS)) == 1 and not result.findings


def test_an_unreadable_type_or_scope_is_a_finding_and_no_claim() -> None:
    result = _run(
        _fleet(
            assertion("ASR-13", None, (UNIT_1, SLOT)),
            assertion("ASR-14", SAME, None),
            assertion("ASR-15", SAME, (UNIT_1,)),
        )
    )
    assert not result.claims
    assert _codes(result) == [
        "identity.assertion_scope",
        "identity.assertion_scope",
        "identity.assertion_unread",
    ]


def test_distinct_identity_suppresses_candidates_and_contests_a_same_as() -> None:
    apart = assertion("ASR-16", DISTINCT, (SLOT, UNIT_2), authored_at=at(6))
    result = _run(_fleet(AMBIGUOUS, apart))
    assert _pairs(_of(result, SAME_AS_CANDIDATE)) == {
        ("controller:slot-b", "serial:H1-0001"),
        ("serial:H1-0001", "controller:slot-b"),
    }
    joined = assertion("ASR-17", SAME, (SLOT, UNIT_2), authored_at=at(7))
    contested = _run(_fleet(apart, joined))
    assert len(_of(contested, SAME_AS)) == 1  # evidence is never dropped for a contrary one
    (finding,) = contested.findings
    assert finding.code == "identity.contested"
    assert finding.records == tuple(sorted((apart["id"], joined["id"])))  # type: ignore[type-var]


def test_a_same_as_chain_across_a_declared_distinct_pair_is_contested() -> None:
    """slot-b = H1-0001 (operator) and H1-0001 = H1-0002 (register): the walk would join slot-b
    and H1-0002, which a person declared distinct. Both stand; the contest says so."""
    apart = assertion("ASR-18", DISTINCT, (SLOT, UNIT_2), authored_at=at(6))
    first = assertion("ASR-19", SAME, (SLOT, UNIT_1), authored_at=at(7))
    result = _run(_fleet(apart, first, link("unit register row 2", UNIT_1, UNIT_2)))
    assert len(_of(result, SAME_AS)) == 2
    (finding,) = result.findings
    assert finding.code == "identity.contested"
    assert finding.records == (apart["id"],)  # no direct ground between the two
    assert finding.details["nodes"] == ["controller:slot-b", "serial:H1-0002"]


# --- Retraction --------------------------------------------------------------------------------

CONFIRMED = assertion("ASR-20", SAME, (SLOT, UNIT_3), authored_at=at(10))
RETRACTION = assertion(
    "ASR-21", RETRACT, (), retracts=LogicalId("ops-console", "ASR-20"), authored_at=at(20)
)


def test_a_retracted_same_identity_grounds_nothing_from_the_retraction_s_build() -> None:
    before = _run(_fleet(CONFIRMED), ledger_tx(1))
    (said,) = _of(before, SAME_AS)
    assert said.provenance.records == (CONFIRMED["id"],)
    after = _run({**_fleet(CONFIRMED), "pkg-ops-2": [RETRACTION]}, ledger_tx(2))
    # The build at the retraction's transaction no longer emits the claim; build withdrawal
    # (ADR 0007 §5, MVL-132) then supersedes it at tx 2, so the link's interval closes in
    # transaction time while as_of 1 still shows it. Nothing is deleted or edited.
    assert not after.claims and not after.findings


def test_a_retraction_of_the_retraction_restores_the_assertion() -> None:
    undo = assertion(
        "ASR-22", RETRACT, (), retracts=LogicalId("ops-console", "ASR-21"), authored_at=at(30)
    )
    result = _run({**_fleet(CONFIRMED), "pkg-ops-2": [RETRACTION, undo]})
    (claim,) = _of(result, SAME_AS)
    assert claim.provenance.records == (CONFIRMED["id"],)  # the same claim id as before
    assert claim.id == _of(_run(_fleet(CONFIRMED)), SAME_AS)[0].id


def test_a_retraction_loop_is_undecided_reported_and_grounds_nothing() -> None:
    # ASR-24 retracts the confirmation; another entry, filed under the confirmation's own id
    # (root ADR 0062 keeps duplicate ids as declared), retracts ASR-24: neither can stand first.
    loop_a = assertion(
        "ASR-23",
        RETRACT,
        (),
        identifier=LogicalId("ops-console", "ASR-20"),
        retracts=LogicalId("ops-console", "ASR-24"),
    )
    loop_b = assertion("ASR-24", RETRACT, (), retracts=LogicalId("ops-console", "ASR-20"))
    result = _run({**_fleet(CONFIRMED), "pkg-ops-2": [loop_a, loop_b]})
    assert not result.claims
    assert _codes(result) == ["identity.retraction_undecided"]
    assert result.findings[0].records == (CONFIRMED["id"],)


def test_a_self_retraction_is_undecided() -> None:
    twin = assertion(
        "ASR-25",
        RETRACT,
        (),
        identifier=LogicalId("ops-console", "ASR-20"),
        retracts=LogicalId("ops-console", "ASR-20"),
    )
    result = _run({**_fleet(CONFIRMED), "pkg-ops-2": [twin]})
    assert not result.claims
    assert _codes(result) == ["identity.retraction_undecided"]


def test_a_retraction_of_an_unknown_id_retracts_nothing_and_is_reported() -> None:
    stray = assertion("ASR-26", RETRACT, (), retracts=LogicalId("ops-console", "ASR-404"))
    result = _run({**_fleet(CONFIRMED), "pkg-ops-2": [stray]})
    assert len(_of(result, SAME_AS)) == 1
    assert _codes(result) == ["identity.retraction_unmatched"]


def test_a_retraction_chain_of_any_length_resolves_without_recursion() -> None:
    """Each retract withdraws the previous one: the last stands, so they alternate down to the
    confirmation, which stands when the chain has an even number of retractions."""
    for length, stands in ((4000, True), (4001, False)):
        chain = [
            assertion(
                f"R-{n}",
                RETRACT,
                (),
                retracts=LogicalId("ops-console", "ASR-20" if n == 0 else f"R-{n - 1}"),
            )
            for n in range(length)
        ]
        result = _run({**_fleet(CONFIRMED), "pkg-ops-2": chain})
        assert bool(_of(result, SAME_AS)) is stands and not result.findings


# --- Clocks ------------------------------------------------------------------------------------


def test_a_stated_instant_on_a_declared_civil_clock_lands_on_the_civil_timeline() -> None:
    domain, domain_id = civil_domain("assertions.json entry 0")
    said = assertion("ASR-30", SAME, (SLOT, UNIT_1), authored_at=at(1_790_762_400, domain_id))
    placed = _run(_fleet(said, domain))
    (claim,) = _of(placed, SAME_AS)
    civil = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(1))
    assert claim.valid_from == civil.at(1_790_762_400) and claim.valid_to == OPEN
    # Without the domain record the instant stays on the clock it was stated on.
    (unplaced,) = _of(_run(_fleet(said)), SAME_AS)
    assert unplaced.valid_from == at(1_790_762_400, domain_id)


def test_one_clock_id_with_two_definitions_places_nothing_and_is_a_conflict() -> None:
    domain, domain_id = civil_domain("assertions.json entry 0")
    finer = {
        **domain,
        "resolution": {"knowledge": "known", "value": {"denominator": 1000, "numerator": 1}},
    }
    said = assertion("ASR-31", SAME, (SLOT, UNIT_1), authored_at=at(1_790_762_400, domain_id))
    result = _run({**_fleet(said, domain), "pkg-other": [finer]})
    assert [f.code for f in result.findings] == ["identity.record_conflict"]
    (claim,) = _of(result, SAME_AS)
    assert claim.valid_from == at(1_790_762_400, domain_id)  # never on a guessed tick length
