"""Run configurations and authorisation coverage (ADR 0010 §3-§5), and hostile input.

Runs come from the compiler's ``Run`` records and are named by their Ledger thread node (declared
or anchored); what a run ran with comes only from ``SnapshotBinding`` records. Coverage is decided
on one clock or not at all.
"""

from collections.abc import Sequence
from fractions import Fraction
from typing import TYPE_CHECKING

import pytest

from memory_configuration_records import (
    binding,
    change,
    commissioning,
    configuration_thread,
    envelope,
    hardware,
    run,
    run_thread,
    threads,
    worked_example,
)
from memory_identity_records import CLOCK, Record, civil_domain, ledger
from neptune.identity import canonical_json
from neptune.model.alignment import ValidityWindow
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import Ambiguous, Candidate, Known, Unknown
from neptune.model.time import Epoch, Timescale, Timestamp
from neptune_memory.consolidate.base import Consolidation, Consolidator, rebuild, run_consolidator
from neptune_memory.consolidate.configuration import ConfigurationLineageConsolidator
from neptune_memory.consolidate.identity import IdentityConsolidator
from neptune_memory.schema.claim import Claim, LedgerRecordRef
from neptune_memory.schema.interval import OPEN, CivilClock, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue

TX = ledger_tx(9)
SECONDS = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(1))
RUN_CLOCK, RUN_CLOCK_ID = civil_domain("arm controller clock")
FORM_CLOCK, FORM_CLOCK_ID = civil_domain("site forms")
T0 = 1_790_000_000  # POSIX seconds, late September 2026

ARM = LogicalId("robot.serial", "20415")
CELL = LogicalId("plant.cell", "CELL-3")
CFG_A, CFG_B = (
    LogicalId("plant.configuration", "CELL3-CFG-A"),
    LogicalId("plant.configuration", "CELL3-CFG-B"),
)
RUN_1 = LogicalId("cell.run", "CELL-3/shift-1")
RUN_NODE = NodeRef(NodeType.RUN, "cell.run:CELL-3/shift-1")
CONFIG_A = NodeRef(NodeType.CONFIGURATION, "plant.configuration:CELL3-CFG-A")
CONFIG_B = NodeRef(NodeType.CONFIGURATION, "plant.configuration:CELL3-CFG-B")


def on_run(offset: int) -> Timestamp:
    return Timestamp(T0 + offset, RUN_CLOCK_ID)


def on_form(offset: int) -> Timestamp:
    return Timestamp(T0 + offset, FORM_CLOCK_ID)


def placed(offset: int) -> Timestamp:
    return SECONDS.at(T0 + offset)


def consolidate(
    *packages: Sequence[Record], config: dict[str, object] | None = None
) -> Consolidation:
    result = run_consolidator(
        ConfigurationLineageConsolidator(),
        ledger({f"package-{i}": list(records) for i, records in enumerate(packages)}),
        (),
        config or {},  # type: ignore[arg-type]
        recorded_at=TX,
    )
    assert not [f for f in result.findings if f.code.startswith("consolidate.")], result.findings
    return result


def of(result: Consolidation, predicate: str) -> list[Claim]:
    return [c for c in result.claims if c.predicate == predicate]


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def rid(record: Record) -> RecordId:
    return record["id"]  # type: ignore[return-value]


URDF_A, URDF_B = "cell3-ur10e-2f140.urdf", "cell3-ur10e-epick.urdf"


def cell(
    *extra: Record, first: int | None = 0, last: int | None = 3599
) -> tuple[list[Record], Record]:
    """An arm cell with two configuration threads (each anchored on its URDF), a recorded shift
    run, and ``extra`` records; returns the records and the run."""
    shift = run(
        "shift-1.mcap",
        RUN_1,
        on_run(first) if first is not None else None,
        on_run(last) if last is not None else None,
        ARM,
    )
    return [
        *threads(NodeType.MACHINE, ARM),
        *threads(NodeType.SITE, CELL),
        configuration_thread(CFG_A, URDF_A),
        configuration_thread(CFG_B, URDF_B),
        run_thread(RUN_1, "threads/shift-1"),
        RUN_CLOCK,
        FORM_CLOCK,
        hardware(URDF_A),
        hardware(URDF_B),
        shift,
        *extra,
    ], shift


# --- configuration_active_during ----------------------------------------------------------------


def test_a_binding_over_the_whole_run_holds_from_first_to_the_tick_after_last() -> None:
    records, shift = cell()
    bound = binding("shift-1 urdf", shift, hardware(URDF_A))
    result = consolidate([*records, bound])
    (active,) = of(result, "configuration_active_during")
    assert (active.subject, active.object) == (RUN_NODE, CONFIG_A)
    assert (active.valid_from, active.valid_to) == (placed(0), placed(3600))  # last is inclusive
    assert active.assertion_kind == "observed"
    assert set(active.provenance.records) == {rid(bound), rid(shift), rid(hardware(URDF_A))}
    assert of(result, "configuration_unknown") == []


def test_a_mid_run_change_gives_two_windows_of_one_run() -> None:
    records, shift = cell()
    before = binding(
        "before", shift, hardware(URDF_A), start=on_run(0), end=on_run(1800), clock=RUN_CLOCK_ID
    )
    after = binding(
        "after", shift, hardware(URDF_B), start=on_run(1800), end=on_run(3600), clock=RUN_CLOCK_ID
    )
    result = consolidate([*records, before, after])
    windows = sorted(
        (c.object.node_id, c.valid_from.ticks, c.valid_to.ticks)  # type: ignore[union-attr]
        for c in of(result, "configuration_active_during")
    )
    assert windows == [
        (CONFIG_A.node_id, T0, T0 + 1800),
        (CONFIG_B.node_id, T0 + 1800, T0 + 3600),
    ]
    assert "configuration.binding_overlap" not in codes(result)


def test_overlapping_bindings_of_one_kind_are_candidates() -> None:
    records, shift = cell()
    a = binding("a", shift, hardware(URDF_A), start=on_run(0), end=on_run(2000), clock=RUN_CLOCK_ID)
    b = binding(
        "b", shift, hardware(URDF_B), start=on_run(1000), end=on_run(3600), clock=RUN_CLOCK_ID
    )
    result = consolidate([*records, a, b])
    assert of(result, "configuration_active_during") == []
    assert {c.object for c in of(result, "configuration_candidate")} == {CONFIG_A, CONFIG_B}
    (overlap,) = [f for f in result.findings if f.code == "configuration.binding_overlap"]
    assert set(overlap.records) == {rid(a), rid(b)}


def test_a_run_with_no_binding_is_unknown_never_the_nearest_configuration() -> None:
    # The cell was commissioned on CFG-A the hour before; the run still has no configuration.
    records, shift = cell(
        commissioning("CC-3-001", [ARM], CFG_A, on_form(-3600)),
    )
    result = consolidate(records)
    assert of(result, "configuration_active_during") == []
    (unknown,) = of(result, "configuration_unknown")
    assert (unknown.subject, unknown.object) == (RUN_NODE, LedgerRecordRef(rid(shift)))
    assert (unknown.valid_from, unknown.valid_to) == (placed(0), placed(3600))
    assert unknown.assertion_kind == "observed"


def test_an_anchored_run_is_named_by_the_thread_citing_its_evidence() -> None:
    anonymous = run("bag-0007.bag", None, on_run(0), on_run(59), ARM)
    records, _ = cell()
    records += [run_thread(None, "bag-0007.bag"), anonymous]
    result = consolidate([*records, binding("bag", anonymous, hardware(URDF_B))])
    (active,) = of(result, "configuration_active_during")
    assert active.subject == NodeRef(NodeType.RUN, "anchor:bag-0007.bag")


def test_a_run_whose_last_instant_is_unknown_is_open() -> None:
    records, shift = cell(last=None)
    result = consolidate([*records, binding("b", shift, hardware(URDF_A))])
    (active,) = of(result, "configuration_active_during")
    assert (active.valid_from, active.valid_to) == (placed(0), OPEN)


def test_a_run_with_no_stated_instant_starts_at_its_thread() -> None:
    records, _ = cell(first=None, last=None)
    result = consolidate(records)
    (unknown,) = of(result, "configuration_unknown")
    assert (unknown.valid_from, unknown.valid_to) == (Timestamp(100, CLOCK), OPEN)  # thread start


def test_bindings_that_cannot_be_followed_are_findings_and_unknown_windows() -> None:
    records, shift = cell()
    missing_snapshot = binding("lost", shift, hardware("deleted.urdf"))
    unthreaded = binding("orphan", shift, hardware("orphan.urdf"))
    no_run = binding("no run", run("never-ingested.mcap", RUN_1), hardware(URDF_A))
    result = consolidate([*records, hardware("orphan.urdf"), missing_snapshot, unthreaded, no_run])
    assert codes(result) == [
        "configuration.dangling_binding",
        "configuration.dangling_binding",
        "configuration.unthreaded_id",
    ]
    unknown = {c.object for c in of(result, "configuration_unknown")}
    assert unknown == {LedgerRecordRef(rid(missing_snapshot)), LedgerRecordRef(rid(unthreaded))}


def test_a_binding_outside_its_run_or_a_run_ending_before_it_starts_is_a_finding() -> None:
    records, shift = cell()
    late = binding("late", shift, hardware(URDF_A), start=on_run(5000), clock=RUN_CLOCK_ID)
    result = consolidate([*records, late])
    assert codes(result) == ["configuration.untimeable_window"]
    assert of(result, "configuration_active_during") == []
    backwards, _ = cell(first=3599, last=0)
    result = consolidate(backwards)
    assert codes(result) == ["configuration.untimeable_window"]
    (unknown,) = of(result, "configuration_unknown")
    assert (unknown.valid_from, unknown.valid_to) == (placed(3599), OPEN)


def test_a_binding_naming_a_snapshot_under_another_kind_says_so() -> None:
    records, shift = cell()
    wrong = {**binding("wrong kind", shift, hardware(URDF_A)), "snapshot_kind": "calibration"}
    (finding,) = consolidate([*records, wrong]).findings
    assert finding.code == "configuration.dangling_binding"
    assert finding.details["held_as"] == "hardware_configuration"


def test_a_snapshot_two_configuration_threads_cite_gives_both_readings() -> None:
    records, shift = cell(
        configuration_thread(LogicalId("plant.configuration", "CELL3-CFG-A'"), URDF_A)
    )
    result = consolidate([*records, binding("b", shift, hardware(URDF_A))])
    assert len(of(result, "configuration_candidate")) == 2
    assert codes(result) == ["configuration.ambiguous_anchor"]


def test_an_inverted_binding_window_is_a_finding() -> None:
    records, shift = cell()
    # The compiler refuses an empty window; this one states its end on another clock.
    other = binding(
        "elsewhere", shift, hardware(URDF_A), start=None, end=Timestamp(5, CLOCK), clock=CLOCK
    )
    result = consolidate([*records, other])
    assert codes(result) == ["configuration.untimeable_window"]
    assert of(result, "configuration_active_during") == []


# --- Authorisation ------------------------------------------------------------------------------


def authorised(
    valid_from: int | None, valid_until: int | str | None, configuration: object = CFG_A
) -> Record:
    """An envelope; ``valid_until`` ``"open"`` states it has none, ``None`` is ``Unknown``."""
    return envelope(
        f"AUTH {valid_from}-{valid_until}-{configuration}",
        CELL,
        configuration,  # type: ignore[arg-type]
        on_form(valid_from) if valid_from is not None else None,
        on_form(valid_until) if isinstance(valid_until, int) else valid_until,  # type: ignore[arg-type]
    )


def covered(*envelopes: Record, last: int | None = 3599) -> Consolidation:
    """The shift bound to CFG-A by a binding that states it holds for the whole run (both bounds
    stated open), beside ``envelopes``."""
    records, shift = cell(*envelopes, last=last)
    whole = binding("b", shift, hardware(URDF_A), start="open", end="open", clock=RUN_CLOCK_ID)
    return consolidate([*records, whole])


def not_covered(result: Consolidation) -> list[tuple[int, int | None]]:
    return sorted(
        (c.valid_from.ticks - T0, None if c.valid_to == OPEN else c.valid_to.ticks - T0)  # type: ignore[union-attr]
        for c in of(result, "not_covered_by_authorisation")
    )


def test_an_envelope_is_a_site_claim_on_its_window() -> None:
    result = covered(authorised(-86400, 86400))
    (claim,) = of(result, "authorised_configuration")
    assert (claim.subject, claim.object) == (NodeRef(NodeType.SITE, "plant.cell:CELL-3"), CONFIG_A)
    assert (claim.valid_from, claim.valid_to) == (placed(-86400), placed(86400))
    assert claim.assertion_kind == "stated"


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ((-86400, 86400), []),  # covers the run
        ((0, 3600), []),  # exactly the run: [first, last + 1)
        ((0, "open"), []),  # states it has no end
        ((1800, 86400), [(0, 1800)]),  # starts mid-run
        ((-86400, 1800), [(1800, 3600)]),  # ends mid-run
        ((-86400, 0), [(0, 3600)]),  # ends as the run starts
        ((3600, 86400), [(0, 3600)]),  # starts as the run ends
    ],
)
def test_coverage_is_the_part_of_the_run_no_envelope_window_covers(
    window: tuple[int, int | str], expected: list[tuple[int, int]]
) -> None:
    result = covered(authorised(*window))
    assert not_covered(result) == expected
    for claim in of(result, "not_covered_by_authorisation"):
        assert (claim.subject, claim.object, claim.assertion_kind) == (
            RUN_NODE,
            CONFIG_A,
            "observed",
        )
    assert "configuration.authorisation_undecided" not in codes(result)


def _bound_by(
    validity_kwargs: dict[str, object], *envelopes: Record, **cell_kwargs: object
) -> Consolidation:
    records, shift = cell(*envelopes, **cell_kwargs)  # type: ignore[arg-type]
    return consolidate([*records, binding("b", shift, hardware(URDF_A), **validity_kwargs)])  # type: ignore[arg-type]


UNSTATED_WINDOWS = {
    "Unknown (the compiler's own stated bindings)": {"validity": Unknown()},
    "NotCovered": {},
    "start Known, end Unknown": {"start": on_run(0), "end": None, "clock": RUN_CLOCK_ID},
    "start Unknown, end Known": {"start": None, "end": on_run(3600), "clock": RUN_CLOCK_ID},
}


@pytest.mark.parametrize("validity", UNSTATED_WINDOWS.values(), ids=UNSTATED_WINDOWS.keys())
def test_coverage_is_never_decided_over_a_window_the_binding_does_not_state(
    validity: dict[str, object],
) -> None:
    # An envelope ending mid-run: over a stated window, the second half would be not covered.
    result = _bound_by(validity, authorised(-86400, 1800))
    (active,) = of(result, "configuration_active_during")  # the run ran with it: still said
    assert active.object == CONFIG_A
    assert of(result, "not_covered_by_authorisation") == []
    (undecided,) = result.findings
    assert undecided.code == "configuration.authorisation_undecided"
    assert undecided.details["envelopes_naming_it"] == 1
    # And with no envelope at all: still undecided, never a whole-run not-covered claim.
    alone = _bound_by(validity)
    assert of(alone, "not_covered_by_authorisation") == []
    (finding,) = alone.findings
    assert finding.details["envelopes_naming_it"] == 0


def test_an_ambiguous_binding_window_is_one_candidate_per_reading_and_decides_nothing() -> None:
    either = Ambiguous(
        (
            Candidate(ValidityWindow(RUN_CLOCK_ID, Known(on_run(0)), Known(on_run(100)))),
            Candidate(ValidityWindow(RUN_CLOCK_ID, Known(on_run(0)), Known(on_run(200)))),
        )
    )
    result = _bound_by({"validity": either}, authorised(-86400, 1800))
    assert of(result, "configuration_active_during") == []
    assert of(result, "not_covered_by_authorisation") == []
    readings = sorted(
        (c.valid_from.ticks - T0, c.valid_to.ticks - T0)  # type: ignore[union-attr]
        for c in of(result, "configuration_candidate")
    )
    assert readings == [(0, 100), (0, 200)]
    assert codes(result) == ["configuration.ambiguous_window"]


def test_a_stated_open_start_counts_only_when_the_run_states_its_first_instant() -> None:
    stated_open = {"start": "open", "end": on_run(3600), "clock": RUN_CLOCK_ID}
    assert not_covered(_bound_by(stated_open, authorised(-86400, 1800))) == [(1800, 3600)]
    # With no first instant the run's start is its thread's, a convention: nothing is decided.
    records, shift = cell(authorised(-86400, 1800), first=None)
    records = [
        run_thread(
            RUN_1, "threads/shift-1", start=on_run(-60)
        )  # its thread starts on the run clock
        if r.get("node_type") == "run"
        else r
        for r in records
    ]
    bound = binding("b", shift, hardware(URDF_A), **stated_open)  # type: ignore[arg-type]
    result = consolidate([*records, bound])
    (active,) = of(result, "configuration_active_during")
    assert active.valid_from == placed(-60)
    assert of(result, "not_covered_by_authorisation") == []
    assert codes(result) == ["configuration.authorisation_undecided"]


@pytest.mark.parametrize(
    "until",
    [None, Ambiguous((Candidate(on_form(1800)), Candidate(on_form(86400))))],
    ids=["Unknown", "Ambiguous"],
)
def test_an_envelope_whose_end_is_not_stated_never_authorises_until_further_notice(
    until: object,
) -> None:
    envelope_ = envelope("AUTH no end", CELL, CFG_A, on_form(-86400), until)  # type: ignore[arg-type]
    result = covered(envelope_)
    assert of(result, "authorised_configuration") == []  # no site claim with an open end
    assert of(result, "not_covered_by_authorisation") == []  # nor a decision after valid_from
    assert codes(result) == [
        "configuration.authorisation_undecided",
        "configuration.envelope_unplaced",
    ]
    # Before its valid_from it covers nothing, which is decided.
    later = covered(envelope("AUTH later", CELL, CFG_A, on_form(1800), until))  # type: ignore[arg-type]
    assert "configuration.authorisation_undecided" in codes(later)
    after_run = covered(envelope("AUTH after", CELL, CFG_A, on_form(7200), until))  # type: ignore[arg-type]
    assert not_covered(after_run) == [(0, 3600)]


def test_two_envelopes_together_cover_the_run() -> None:
    assert not_covered(covered(authorised(-10, 1000), authorised(1000, 5000))) == []
    assert not_covered(covered(authorised(-10, 1000), authorised(2000, 5000))) == [(1000, 2000)]


def test_a_configuration_no_envelope_names_is_not_covered_over_the_whole_run() -> None:
    result = covered(authorised(-86400, 86400, CFG_B))
    assert not_covered(result) == [(0, 3600)]
    (claim,) = of(result, "not_covered_by_authorisation")
    assert claim.assertion_kind == "observed" and claim.object == CONFIG_A


def test_coverage_across_clocks_is_undecided_never_assumed() -> None:
    boot = envelope("AUTH boot", CELL, CFG_A, Timestamp(0, CLOCK), Timestamp(10**9, CLOCK))
    result = covered(boot)
    assert of(result, "not_covered_by_authorisation") == []
    assert codes(result) == ["configuration.authorisation_undecided"]


def test_an_ambiguous_envelope_leaves_coverage_undecided() -> None:
    either = Ambiguous((Candidate(CFG_A), Candidate(CFG_B)))
    result = covered(authorised(-86400, 86400, either))
    assert of(result, "not_covered_by_authorisation") == []
    assert of(result, "authorised_configuration") == []
    assert codes(result) == [
        "configuration.authorisation_undecided",
        "configuration.envelope_unplaced",
    ]


def test_an_open_run_partly_covered_is_undecided() -> None:
    # It may end before the envelope does, or run past it: neither is stated.
    for window in ((-86400, 1800), (1800, 86400)):
        result = covered(authorised(*window), last=None)
        assert of(result, "not_covered_by_authorisation") == []
        assert codes(result) == ["configuration.authorisation_undecided"]
    # A run that starts after the envelope ends is not covered at any instant, whenever it ends.
    later = covered(authorised(-86400, -10), last=None)
    assert not_covered(later) == [(0, None)]


def test_an_envelope_naming_no_configuration_leaves_what_it_may_cover_undecided() -> None:
    overlapping = covered(authorised(-10, 1800, Unknown()))
    assert of(overlapping, "not_covered_by_authorisation") == []
    assert "configuration.authorisation_undecided" in codes(overlapping)
    # One whose window misses the run on the same clock decides nothing either way.
    elsewhere = covered(authorised(86400, 2 * 86400, Unknown()))
    assert not_covered(elsewhere) == [(0, 3600)]
    assert "configuration.authorisation_undecided" not in codes(elsewhere)


def test_envelopes_that_cannot_be_placed_are_findings() -> None:
    no_start = authorised(None, 86400)
    inverted = envelope("AUTH inverted", CELL, CFG_A, on_form(10), on_form(10))
    no_site = envelope("AUTH no site", None, CFG_A, on_form(-10), on_form(86400))
    no_configuration = authorised(-10, 86400, Unknown())
    result = covered(no_start, inverted, no_site, no_configuration)
    assert codes(result) == [
        "configuration.envelope_unplaced",
        "configuration.envelope_unplaced",
        "configuration.envelope_unplaced",
        "configuration.untimeable_window",
    ]
    assert of(result, "authorised_configuration") == []
    # The envelope with no site still names CFG-A over a window that covers the run, which
    # settles coverage although the other two cannot be placed.
    assert of(result, "not_covered_by_authorisation") == []


# --- Hostile input ------------------------------------------------------------------------------


def test_malformed_records_are_findings_and_the_rest_of_the_build_stands() -> None:
    records, shift = cell(commissioning("CC-3-001", [ARM], CFG_A, on_form(-3600)))
    good = binding("b", shift, hardware(URDF_A), start="open", end="open", clock=RUN_CLOCK_ID)
    broken = {**commissioning("broken", [ARM], CFG_B, on_form(0)), "commissioned": "yesterday"}
    padded = commissioning("padded", [LogicalId("robot.serial", " 20415")], CFG_B, on_form(1))
    inferred = {**binding("guess", shift, hardware(URDF_B))}
    inferred["provenance"] = {**inferred["provenance"], "assertion_kind": "inferred"}  # type: ignore[dict-item]
    junk = {"kind": "snapshot_binding", "id": 7}
    result = consolidate([*records, good, broken, padded, inferred, junk])
    assert codes(result) == [
        "configuration.inferred_record",
        "configuration.malformed_record",
        "configuration.malformed_record",
        "configuration.malformed_record",
    ]
    assert len(of(result, "configuration_active_during")) == 1
    assert len(of(result, "has_configuration")) == 1


def test_one_record_id_with_two_contents_is_used_nowhere() -> None:
    records, _ = cell()
    original = commissioning("CC-3-001", [ARM], CFG_A, on_form(0))
    forged = {**commissioning("CC-3-001", [ARM], CFG_B, on_form(0)), "id": original["id"]}
    result = consolidate([*records, original], [forged])
    assert of(result, "has_configuration") == []
    assert "configuration.record_conflict" in codes(result)
    # The same record in two packages is one record.
    twice = consolidate([*records, original], [original])
    assert len(of(twice, "has_configuration")) == 1


def test_records_a_chain_cannot_place_are_findings() -> None:
    records, _ = cell()
    stranger = LogicalId("robot.serial", "99999")
    unnamed = change("unknown config id", [ARM], LogicalId("plant.configuration", "x"), on_form(5))
    result = consolidate(
        [
            *records,
            commissioning("untimed", [ARM], CFG_A, None),
            commissioning("no machines", Unknown(), CFG_A, on_form(0)),
            commissioning("stranger", [stranger], CFG_A, on_form(0)),
            unnamed,
            change(
                "ambiguous machine",
                Known((Ambiguous((Candidate(ARM), Candidate(stranger))),)),
                CFG_B,
                on_form(9),
            ),
        ]
    )
    assert codes(result) == [
        "configuration.chain_gap",
        "configuration.unplaced_record",  # no Known machine list
        "configuration.unplaced_record",  # an Ambiguous machine entry
        "configuration.unthreaded_id",
        "configuration.unthreaded_id",
        "configuration.untimed_record",
    ]
    # The unthreaded configuration id leaves the arm's configuration unknown from its instant.
    (unknown,) = [
        c for c in of(result, "configuration_unknown") if c.subject.node_type is NodeType.MACHINE
    ]
    assert unknown.object == LedgerRecordRef(rid(unnamed)) and unknown.valid_from == placed(5)


def test_a_machine_whose_records_use_two_clocks_has_a_chain_per_clock() -> None:
    records, _ = cell()
    result = consolidate(
        [
            *records,
            commissioning("civil", [ARM], CFG_A, on_form(0)),
            change("boot clock", [ARM], CFG_B, Timestamp(500, CLOCK)),
        ]
    )
    assert codes(result) == ["configuration.clock_split"]
    assert {c.valid_to for c in of(result, "has_configuration")} == {OPEN}
    assert of(result, "succeeds") == []


def test_configuration_keys_are_reported_and_ignored() -> None:
    records, _ = cell()
    result = consolidate(records, config={"window": 5})
    assert codes(result) == ["configuration.unknown_config"]


# --- Determinism --------------------------------------------------------------------------------


def _scenario() -> list[Record]:
    records, shift = cell(
        commissioning("CC-3-001", [ARM], CFG_A, on_form(-86400)),
        change("CHG-T1", [ARM], CFG_B, on_form(1800)),
        authorised(-86400, 1000),
    )
    return [
        *records,
        binding(
            "before", shift, hardware(URDF_A), start=on_run(0), end=on_run(1800), clock=RUN_CLOCK_ID
        ),
        binding("after", shift, hardware(URDF_B), start=on_run(1800), end=None, clock=RUN_CLOCK_ID),
        *worked_example("manipulator_cell"),
    ]


def test_output_is_byte_identical_across_runs_and_input_orders() -> None:
    records = _scenario()
    once = canonical_json.dumps(consolidate(records).to_json())
    assert canonical_json.dumps(consolidate(records).to_json()) == once
    split = len(records) // 2
    shuffled = consolidate(list(reversed(records[split:])), list(reversed(records[:split])))
    assert canonical_json.dumps(shuffled.to_json()) == once


def test_rebuild_with_identity_is_deterministic_and_every_claim_conforms() -> None:
    plan: list[tuple[Consolidator, dict[str, JsonValue]]] = [
        (IdentityConsolidator(), {}),
        (ConfigurationLineageConsolidator(), {}),
    ]
    lake = ledger({"package-0": _scenario()})
    first = rebuild(lake, plan, recorded_at=TX)
    second = rebuild(lake, plan, recorded_at=TX)
    assert [canonical_json.dumps(c.to_json()) for c in first] == [
        canonical_json.dumps(c.to_json()) for c in second
    ]
    configuration = first[1]
    assert configuration.claims and not [
        f for f in configuration.findings if f.code.startswith("consolidate.")
    ]
    predicates = {c.predicate for c in configuration.claims}
    assert "succeeds" not in predicates  # a chain's change is the machine's spans (ADR 0019)
    assert {
        "has_configuration",
        "configuration_active_during",
        "not_covered_by_authorisation",
        "authorised_configuration",
    } <= predicates
