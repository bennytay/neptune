"""The run consolidator on hostile, boundary and reordered input (ADR 0009).

Every malformed or contradictory record is a finding and never a claim, and the rest of the build
is unaffected. Interval ends at the edge of a clock, inverted or split across clocks, mappings
that do not cover a run, and residual bounds are each pinned. The output is a function of the
Ledger's content only: package order, record order and repetition change nothing.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING

import pytest

from memory_identity_records import CLOCK, Record, at, ledger
from memory_run_records import (
    assembly,
    declaration,
    domain,
    mapping,
    revision,
    run,
    site,
)
from memory_time_records import estimate
from neptune.identity import canonical_json
from neptune.model.alignment import MemberRole
from neptune.model.ids import LogicalId
from neptune.model.knowledge import Ambiguous, Unknown
from neptune.model.run import run_from_json
from neptune.model.time import INT64_MAX, Epoch, Timescale, Timestamp
from neptune_memory.consolidate.base import Consolidation, rebuild, run_consolidator
from neptune_memory.consolidate.identity import IdentityConsolidator
from neptune_memory.consolidate.runs import RunConsolidator, involvement, run_node
from neptune_memory.schema.claim import LedgerRecordRef
from neptune_memory.schema.interval import OPEN, CivilClock, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.consolidate.base import Consolidator
    from neptune_memory.schema.claim import Claim

TX = ledger_tx(4)
AMR = LogicalId("asset-tag", "AMR-04")
REC, DESC = MemberRole.RECORDING, MemberRole.DESCRIPTION
SECONDS = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(1, 10**9))
OTHER = domain("other", civil=False)[1]


def consolidate(packages: Mapping[str, Sequence[Record]], config: object = None) -> Consolidation:
    return run_consolidator(
        RunConsolidator(),
        ledger(packages),
        (),
        dict(config or {}),  # type: ignore[call-overload]
        recorded_at=TX,
        registry=CORE_PREDICATES,
    )


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def of(result: Consolidation, predicate: str) -> list[Claim]:
    return [c for c in result.claims if c.predicate == predicate]


def run_node_of(record: Record) -> NodeRef:
    return run_node(run_from_json(record))  # type: ignore[arg-type]


def _good() -> Record:
    return run("good.mcap", first=at(0), last=at(9), machine=AMR)[0]


GOOD = run("good.mcap", first=at(0), last=at(9), machine=AMR)[1]


# --- malformed input ----------------------------------------------------------------------------


def _without(record: Record, key: str) -> Record:
    return {k: v for k, v in record.items() if k != key}


@pytest.mark.parametrize(
    ("kind", "bad"),
    [
        ("run", _without(run("x.mcap")[0], "first")),
        ("run", {**run("x.mcap")[0], "first": 5}),
        ("run", {**run("x.mcap")[0], "extra": True}),
        ("run", run("x.mcap", machine=LogicalId("asset-tag", " AMR-04"))[0]),
        ("run", run("x.mcap", logical_id=LogicalId("manifest", "   "))[0]),
        ("run_assembly", {**assembly("x", run("x.mcap")[1], [("x.mcap", REC)])[0], "members": []}),
        (
            "run_assembly",
            {**assembly("x", run("x.mcap")[1], [("x.mcap", REC)])[0], "rule": "Not A Token"},
        ),
        ("source_revision", {**revision("x.mcap")[0], "content_id": "md5:abc"}),
        ("clock_mapping", {**mapping("m", CLOCK, OTHER, anchor=(0, 0)), "rate": "0"}),
        ("site", {**site("register", LogicalId("site", "WH-1")), "identifiers": "WH-1"}),
        ("run_declaration", {**declaration("d", GOOD), "extra": 1}),
        ("run_declaration", _without(declaration("d", GOOD), "site")),
        ("run_declaration", _without(declaration("d", GOOD), "provenance")),
        ("run_declaration", {**declaration("d", GOOD), "schema_version": 8}),
        ("run_declaration", {**declaration("d", GOOD), "machine": {"knowledge": "maybe"}}),
        ("run_declaration", {**declaration("d", GOOD), "run": "manifest:cell3-pick-0412"}),
        ("run_declaration", {**declaration("d", GOOD), "id": 7}),
        ("run_declaration", declaration("d", GOOD, machine=LogicalId("asset-tag", "AMR-04 "))),
    ],
)
def test_a_malformed_record_is_a_finding_and_the_rest_still_consolidates(
    kind: str, bad: Record
) -> None:
    result = consolidate({"log": [_good(), bad]})
    (finding,) = result.findings
    assert finding.code == "runs.malformed_record"
    assert finding.details["kind"] == kind
    assert {c.predicate for c in result.claims} == {"evidenced_by", "recorded_by"}


def test_inferred_records_are_findings_never_grounds() -> None:
    record, rid = run("x.mcap", first=at(0), last=at(9))
    held = assembly("x", rid, [("x.mcap", REC)])[0]
    inferred = {**held, "provenance": {**held["provenance"], "assertion_kind": "inferred"}}  # type: ignore[dict-item]
    result = consolidate({"log": [record, inferred]})
    assert codes(result) == ["runs.inferred_record"]
    assert of(result, "has_member") == []


def test_an_estimated_clock_mapping_line_is_inferred_not_malformed() -> None:
    """A ``derived/clock_mapping`` line states ``assertion_kind`` at its top level: it is an
    INFO ``inferred_record``, never a malformed record, and never a ground."""
    record, _ = run("x.mcap", first=at(0), last=at(9))
    line = estimate("fit", "x", "y", anchor=(0, 0))
    result = consolidate({"log": [record, line]})
    assert codes(result) == ["runs.inferred_record"]
    assert all(str(c.assertion_kind) != "inferred" for c in result.claims)


def test_one_record_id_with_two_contents_is_a_conflict_and_neither_is_used() -> None:
    first, _ = run("x.mcap", first=at(0), last=at(9), machine=AMR)
    second = {**first, "machine": {"knowledge": "unknown"}}
    result = consolidate({"a": [first], "b": [second]})
    assert codes(result) == ["runs.record_conflict"]
    assert result.claims == ()


def test_a_record_repeated_in_two_packages_is_one_record() -> None:
    record = _good()
    once, twice = consolidate({"a": [record]}), consolidate({"a": [record], "b": [record]})
    assert twice.claims == once.claims and twice.findings == ()


def test_dangling_assembly_and_declaration_are_findings() -> None:
    _, absent = run("absent.mcap")
    result = consolidate(
        {
            "log": [
                _good(),
                assembly("x", absent, [("absent.mcap", REC)])[0],
                declaration("d", absent, site=LogicalId("site", "WH-1")),
                # Its run is the id of a record that is not a run (a clock): never guessed into
                # a run by its run name or anything else.
                declaration("e", OTHER, logical_id=LogicalId("manifest", "good"), machine=AMR),
            ]
        }
    )
    assert codes(result) == [
        "runs.dangling_assembly",
        "runs.dangling_declaration",
        "runs.dangling_declaration",
    ]
    assert of(result, "has_member") == [] and of(result, "at_site") == []


def test_configuration_is_refused_as_a_finding() -> None:
    result = consolidate({"log": [_good()]}, {"window": 5})
    assert codes(result) == ["runs.unknown_config"]
    assert result.findings[0].details["keys"] == ["window"]


def test_an_ambiguous_run_id_is_keyed_by_its_record() -> None:
    record, rid = run(
        "x.mcap", first=at(0), last=at(9), logical_id=[LogicalId("m", "a"), LogicalId("m", "b")]
    )
    result = consolidate({"log": [record]})
    assert codes(result) == ["runs.ambiguous_run_id"]
    assert {c.subject.node_id for c in result.claims} == {f"record:{rid}"}


def test_an_assembly_whose_member_bytes_are_not_in_its_package_has_no_parts() -> None:
    meta, meta_id = run("bag/metadata.yaml", first=at(0), last=at(9))
    part, _ = run("bag/bag_0.mcap", first=at(0), last=at(9), machine=AMR)
    held = assembly("bag list", meta_id, [("bag/metadata.yaml", DESC), ("bag/bag_0.mcap", REC)])[0]
    # The part's revision is in another package: membership is stated, the part is not found.
    result = consolidate({"a": [meta, held], "b": [part, revision("bag/bag_0.mcap")[0]]})
    assert len(of(result, "has_member")) == 2
    assert of(result, "recorded_by") != [] and codes(result) == []
    assert {c.subject for c in of(result, "recorded_by")} == {run_node_of(part)}


# --- boundaries ---------------------------------------------------------------------------------


def _interval(record: Record) -> set[tuple[object, object]]:
    return {(c.valid_from, c.valid_to) for c in of(consolidate({"log": [record]}), "evidenced_by")}


def test_a_one_instant_run_holds_for_one_tick() -> None:
    assert _interval(run("x.mcap", first=at(5), last=at(5))[0]) == {(at(5), at(6))}


def test_a_run_ending_on_the_last_tick_of_its_clock_is_open() -> None:
    assert _interval(run("x.mcap", first=at(0), last=at(INT64_MAX))[0]) == {(at(0), OPEN)}


def test_a_run_with_no_last_instant_is_open() -> None:
    assert _interval(run("x.mcap", first=at(0))[0]) == {(at(0), OPEN)}


def test_an_inverted_run_places_nothing() -> None:
    result = consolidate({"log": [run("x.mcap", first=at(9), last=at(3))[0]]})
    assert result.claims == () and codes(result) == ["runs.inverted_interval"]


def test_a_last_instant_on_another_clock_leaves_the_end_open() -> None:
    _, other = domain("gps", civil=False)
    result = consolidate({"log": [run("x.mcap", first=at(0), last=at(9, other))[0]]})
    assert {c.valid_to for c in result.claims} == {OPEN}
    assert codes(result) == ["runs.end_on_other_clock"]


def test_a_run_with_no_first_instant_and_no_parts_is_untimed() -> None:
    result = consolidate({"log": [run("x.mcap", machine=AMR)[0]]})
    assert result.claims == () and codes(result) == ["runs.untimed_run"]


def test_a_civil_clock_places_the_run_directly_and_projects_nothing() -> None:
    civil, clock = domain("utc", civil=True)
    record = run("x.mcap", first=at(10, clock), last=at(19, clock))[0]
    result = consolidate({"log": [civil, record]})
    assert {(c.valid_from, c.valid_to) for c in result.claims} == {(SECONDS.at(10), SECONDS.at(20))}


def _projected(*records: Record) -> set[tuple[object, object]]:
    result = consolidate({"log": list(records)})
    return {
        (c.valid_from, c.valid_to)
        for c in of(result, "evidenced_by")
        if c.valid_from.domain_id == SECONDS.domain_id
    }


def test_a_projection_rounds_outwards_and_widens_by_the_residual_bound() -> None:
    civil, clock = domain("utc", civil=True)
    boot_record, boot = domain("boot", civil=False)
    record = run("x.mcap", first=Timestamp(1, boot), last=Timestamp(4, boot))[0]
    halves = mapping("sync", boot, clock, anchor=(0, 100), rate=Fraction(1, 2))
    # [1, 5) at half rate from 100: [100.5, 102.5) -> [100, 103); bound 2 -> [98, 105).
    assert _projected(civil, boot_record, record, halves) == {(SECONDS.at(100), SECONDS.at(103))}
    bounded = mapping("sync bounded", boot, clock, anchor=(0, 100), rate=Fraction(1, 2), bound=2)
    assert _projected(civil, boot_record, record, bounded) == {(SECONDS.at(98), SECONDS.at(105))}


@pytest.mark.parametrize(
    ("window", "projected"),
    [((0, 5), True), ((0, 4), False), ((2, None), False), ((None, None), True), ((1, 5), True)],
)
def test_a_mapping_projects_only_a_run_inside_its_stated_window(
    window: tuple[int | None, int | None], projected: bool
) -> None:
    civil, clock = domain("utc", civil=True)
    boot_record, boot = domain("boot", civil=False)
    record = run("x.mcap", first=Timestamp(1, boot), last=Timestamp(4, boot))[0]
    sync = mapping("sync", boot, clock, anchor=(0, 100), window=window)
    assert bool(_projected(civil, boot_record, record, sync)) is projected


def test_a_mapping_to_a_clock_that_is_not_civil_projects_nothing() -> None:
    gps_record, gps = domain("gps week", civil=False)
    boot_record, boot = domain("boot", civil=False)
    record = run("x.mcap", first=Timestamp(1, boot), last=Timestamp(4, boot))[0]
    result = consolidate(
        {"log": [gps_record, boot_record, record, mapping("m", boot, gps, anchor=(0, 0))]}
    )
    assert {c.valid_from.domain_id for c in result.claims} == {boot}


def test_a_projection_outside_the_civil_clock_is_a_finding() -> None:
    civil, clock = domain("utc", civil=True)
    boot_record, boot = domain("boot", civil=False)
    record = run("x.mcap", first=Timestamp(0, boot), last=Timestamp(4, boot))[0]
    far = mapping("far", boot, clock, anchor=(0, INT64_MAX - 1))
    result = consolidate({"log": [civil, boot_record, record, far]})
    assert codes(result) == ["runs.projection_out_of_range"]
    assert {c.valid_from.domain_id for c in result.claims} == {boot}


def test_concurrent_and_touching_parts_on_one_clock() -> None:
    civil, clock = domain("utc", civil=True)
    meta, meta_id = run("bag/metadata.yaml")
    a = run("bag/a.mcap", first=Timestamp(0, clock), last=Timestamp(9, clock), machine=AMR)[0]
    b = run("bag/b.mcap", first=Timestamp(5, clock), last=Timestamp(14, clock), machine=AMR)[0]
    c = run("bag/c.mcap", first=Timestamp(15, clock), last=Timestamp(20, clock), machine=AMR)[0]
    files = ["bag/a.mcap", "bag/b.mcap", "bag/c.mcap"]
    held = assembly("bag list", meta_id, [(f, REC) for f in files])[0]
    result = consolidate({"up": [civil, meta, a, b, c, held, *(revision(f)[0] for f in files)]})
    # a and b overlap: concurrent, no link. c starts after both end: it continues one of them,
    # and which is ambiguous, so a candidate each (one way: the order is known).
    assert of(result, "continues") == []
    pairs = {(x.subject, x.object) for x in of(result, "continues_candidate")}
    assert pairs == {(run_node_of(c), run_node_of(a)), (run_node_of(c), run_node_of(b))}


def test_parts_of_different_machines_never_continue_each_other() -> None:
    civil, clock = domain("utc", civil=True)
    meta, meta_id = run("bag/metadata.yaml")
    a = run("bag/a.mcap", first=Timestamp(0, clock), last=Timestamp(9, clock), machine=AMR)[0]
    b = run(
        "bag/b.mcap",
        first=Timestamp(10, clock),
        last=Timestamp(19, clock),
        machine=LogicalId("asset-tag", "AMR-05"),
    )[0]
    held = assembly("bag list", meta_id, [("bag/a.mcap", REC), ("bag/b.mcap", REC)])[0]
    result = consolidate(
        {"up": [civil, meta, a, b, held, revision("bag/a.mcap")[0], revision("bag/b.mcap")[0]]}
    )
    assert of(result, "continues") == [] and of(result, "continues_candidate") == []


# --- determinism --------------------------------------------------------------------------------


def _fleet() -> dict[str, list[Record]]:
    civil, clock = domain("utc", civil=True)
    boot_record, boot = domain("boot", civil=False)
    meta, meta_id = run("bag/metadata.yaml")
    parts = [
        run(
            f"bag/{n}.mcap",
            first=Timestamp(10 * i, clock),
            last=Timestamp(10 * i + 9, clock),
            machine=AMR,
        )[0]
        for i, n in enumerate("abc")
    ]
    held = assembly("bag list", meta_id, [(f"bag/{n}.mcap", REC) for n in "abc"])[0]
    boot_run, boot_id = run(
        "arm.bag",
        first=Timestamp(0, boot),
        last=Timestamp(9, boot),
        machine=[AMR, LogicalId("asset-tag", "ARM-1")],
    )
    return {
        "a": [civil, meta, *parts, held, *(revision(f"bag/{n}.mcap")[0] for n in "abc")],
        "b": [boot_record, boot_run, mapping("sync", boot, clock, anchor=(0, 0))],
        "c": [
            site("register", LogicalId("site", "WH-1")),
            declaration("d", boot_id, site=LogicalId("site", "WH-2"), task=LogicalId("task", "t")),
        ],
    }


def _json(result: Consolidation) -> bytes:
    return canonical_json.dumps(result.to_json())


def test_the_same_ledger_gives_byte_identical_output() -> None:
    first, second = consolidate(_fleet()), consolidate(_fleet())
    assert _json(first) == _json(second)
    assert first.claims and first.findings  # the fixture exercises claims and findings


def test_package_and_record_order_change_nothing() -> None:
    fleet = _fleet()
    shuffled = {name: list(reversed(records)) for name, records in reversed(fleet.items())}
    renamed = {f"z-{name}": records for name, records in fleet.items()}
    assert _json(consolidate(shuffled)) == _json(consolidate(fleet))
    assert consolidate(renamed).claims == consolidate(fleet).claims


def test_rebuild_with_identity_is_deterministic_and_stays_in_the_vocabulary() -> None:
    plan: list[tuple[Consolidator, Mapping[str, JsonValue]]] = [
        (IdentityConsolidator(), {}),
        (RunConsolidator(), {}),
    ]
    results = [rebuild(ledger(_fleet()), plan, recorded_at=TX) for _ in range(2)]
    assert [_json(r) for r in results[0]] == [_json(r) for r in results[1]]
    runs = results[0][1]
    assert not [f for f in runs.findings if f.code.startswith("consolidate.")]
    assert all(c.provenance.consolidator_id == "memory.runs" for c in runs.claims)


# --- review regressions -------------------------------------------------------------------------


def _bag(*parts: Record, extra: Sequence[Record] = ()) -> Consolidation:
    meta, meta_id = run("bag/metadata.yaml")
    names = [f"bag/{chr(97 + i)}.mcap" for i in range(len(parts))]
    held = assembly("bag list", meta_id, [(n, REC) for n in names])[0]
    return consolidate({"up": [meta, *parts, held, *(revision(n)[0] for n in names), *extra]})


def test_touching_parts_on_one_clock_are_ordered_there_not_on_a_widened_projection() -> None:
    civil, clock = domain("utc", civil=True)
    boot_record, boot = domain("boot", civil=False)
    a = run("bag/a.mcap", first=Timestamp(0, boot), last=Timestamp(9, boot), machine=AMR)[0]
    b = run("bag/b.mcap", first=Timestamp(10, boot), last=Timestamp(19, boot), machine=AMR)[0]
    sync = mapping("sync", boot, clock, anchor=(0, 0), bound=5)
    result = _bag(a, b, extra=[civil, boot_record, sync])
    links = of(result, "continues")
    assert {(c.subject, c.object) for c in links} == {(run_node_of(b), run_node_of(a))}
    assert {c.valid_from.domain_id for c in links} == {boot, SECONDS.domain_id}  # each placement
    assert of(result, "continues_candidate") == []


def test_projections_that_overlap_only_by_their_bound_are_unordered() -> None:
    civil, clock = domain("utc", civil=True)
    boot_a, ba = domain("boot a", civil=False)
    boot_b, bb = domain("boot b", civil=False)
    a = run("bag/a.mcap", first=Timestamp(0, ba), last=Timestamp(9, ba), machine=AMR)[0]
    b = run("bag/b.mcap", first=Timestamp(0, bb), last=Timestamp(9, bb), machine=AMR)[0]
    maps = [
        mapping("sync a", ba, clock, anchor=(0, 0), bound=5),
        mapping("sync b", bb, clock, anchor=(0, 10), bound=5),
    ]
    result = _bag(a, b, extra=[civil, boot_a, boot_b, *maps])
    assert of(result, "continues") == []
    assert len(of(result, "continues_candidate")) == 2 * 2  # both ways, on each placement


def test_a_part_of_another_machine_between_two_parts_does_not_hide_the_link() -> None:
    civil, clock = domain("utc", civil=True)
    other = LogicalId("asset-tag", "AMR-05")
    a = run("bag/a.mcap", first=Timestamp(0, clock), last=Timestamp(9, clock), machine=AMR)[0]
    b = run("bag/b.mcap", first=Timestamp(0, clock), last=Timestamp(9, clock), machine=other)[0]
    c = run("bag/c.mcap", first=Timestamp(10, clock), last=Timestamp(19, clock), machine=AMR)[0]
    result = _bag(a, b, c, extra=[civil])
    (link,) = of(result, "continues")
    assert (link.subject, link.object) == (run_node_of(c), run_node_of(a))


def test_a_declared_id_in_the_record_namespace_cannot_forge_another_runs_node() -> None:
    honest, honest_id = run("a.mcap", first=at(0), last=at(9), machine=AMR)
    forged, forged_id = run(
        "b.mcap", first=at(0), last=at(9), logical_id=LogicalId("record", honest_id)
    )
    result = consolidate({"log": [honest, forged]})
    assert codes(result) == ["runs.reserved_namespace"]
    subjects = {c.subject.node_id for c in result.claims}
    assert subjects == {f"record:{honest_id}", f"record:{forged_id}"}


def test_a_deeply_nested_declaration_is_one_finding_not_a_crash() -> None:
    nested: object = "x"
    for _ in range(5000):
        nested = {"value": nested}
    bad = {**declaration("d", GOOD), "machine": {"knowledge": "known", "value": nested}}
    result = consolidate({"log": [_good(), bad]})
    assert codes(result) == ["runs.malformed_record"]
    assert of(result, "recorded_by")


def test_an_assembly_window_bounds_its_membership() -> None:
    record, rid = run("x.mcap", first=at(0), last=at(99))
    held = assembly("x list", rid, [("x.mcap", REC)])[0]
    window = {
        "knowledge": "known",
        "value": {
            "clock": CLOCK,
            "start": {"knowledge": "known", "value": at(20).to_json()},
            "end": {"knowledge": "unknown"},
        },
    }
    result = consolidate({"log": [record, {**held, "validity": window}]})
    assert {(c.valid_from, c.valid_to) for c in of(result, "has_member")} == {(at(20), at(100))}
    # The run itself still holds over its whole interval: only the membership is bounded.
    runs = [c for c in of(result, "evidenced_by") if c.object == LedgerRecordRef(rid)]
    assert {(c.valid_from, c.valid_to) for c in runs} == {(at(0), at(100))}


# --- review round 2: an Unknown or Ambiguous value never becomes a definite claim ---------------


def readings_of(value: object) -> set[tuple[NodeRef, ...]]:
    assert isinstance(value, Ambiguous)
    return {c.value for c in value.candidates}


def _known(stamp: Timestamp) -> Record:
    return {"knowledge": "known", "value": stamp.to_json()}


def _ambiguous_windows(clock: str, *spans: tuple[int, int]) -> Record:
    return {
        "knowledge": "ambiguous",
        "candidates": [
            {
                "value": {
                    "clock": clock,
                    "start": _known(Timestamp(lo, clock)),  # type: ignore[arg-type]
                    "end": _known(Timestamp(hi, clock)),  # type: ignore[arg-type]
                }
            }
            for lo, hi in spans
        ],
    }


def test_a_part_with_no_machine_leaves_the_assembled_runs_machine_a_candidate() -> None:
    meta, meta_id = run("folder/manifest", logical_id=LogicalId("manifest", "shift-3"))
    a = run("folder/a.mcap", first=at(0), last=at(500), machine=AMR)[0]
    anonymous = run("folder/b.mcap", first=at(10), last=at(400))[0]
    files = ["folder/a.mcap", "folder/b.mcap"]
    held = assembly("folder paths", meta_id, [(f, REC) for f in files], rule="manifest.run")[0]
    result = consolidate({"p": [meta, a, anonymous, held, *(revision(f)[0] for f in files)]})
    shift = run_node_of(meta)
    assert [c for c in of(result, "recorded_by") if c.subject == shift] == []
    assert {c.object for c in of(result, "recorded_by_candidate")} == {
        NodeRef(NodeType.MACHINE, "asset-tag:AMR-04")
    }
    assert involvement(result.claims, shift, "recorded_by") == Unknown()
    assert codes(result) == ["runs.part_machine_unstated"]


def test_a_part_with_no_machine_between_two_parts_only_possibly_continues() -> None:
    a1 = run("bag/a.mcap", first=at(0), last=at(9), machine=AMR)[0]
    unnamed = run("bag/b.mcap", first=at(10), last=at(19))[0]
    a2 = run("bag/c.mcap", first=at(20), last=at(29), machine=AMR)[0]
    result = _bag(a1, unnamed, a2)
    assert of(result, "continues") == []
    pairs = {(c.subject, c.object) for c in of(result, "continues_candidate")}
    assert pairs == {
        (run_node_of(unnamed), run_node_of(a1)),
        (run_node_of(a2), run_node_of(unnamed)),
    }


def test_an_ambiguous_mapping_window_projects_nothing_it_does_not_cover_in_every_reading() -> None:
    civil, clock = domain("utc", civil=True)
    boot_record, boot = domain("boot", civil=False)
    record = run("x.mcap", first=Timestamp(200, boot), last=Timestamp(299, boot))[0]
    sync = mapping("sync", boot, clock, anchor=(0, 0))
    torn = {**sync, "validity": _ambiguous_windows(boot, (0, 100), (500, 600))}
    result = consolidate({"log": [civil, boot_record, record, torn]})
    assert {c.valid_from.domain_id for c in result.claims} == {boot}
    assert codes(result) == ["runs.ambiguous_window"]
    # Every reading covers the run: the projection holds whichever reading is meant.
    wide = {**sync, "validity": _ambiguous_windows(boot, (0, 400), (100, 600))}
    result = consolidate({"log": [civil, boot_record, record, wide]})
    assert {c.valid_from.domain_id for c in result.claims} == {boot, SECONDS.domain_id}


def test_an_ambiguous_assembly_window_bounds_membership_to_what_every_reading_shares() -> None:
    record, rid = run("x.mcap", first=at(0), last=at(99))
    held = assembly("x list", rid, [("x.mcap", REC)])[0]
    overlapping = {**held, "validity": _ambiguous_windows(CLOCK, (10, 60), (40, 90))}
    result = consolidate({"log": [record, overlapping]})
    assert {(c.valid_from, c.valid_to) for c in of(result, "has_member")} == {(at(40), at(60))}
    assert codes(result) == ["runs.ambiguous_window"]
    apart = {**held, "validity": _ambiguous_windows(CLOCK, (0, 10), (50, 60))}
    assert of(consolidate({"log": [record, apart]}), "has_member") == []


def test_an_untimed_part_leaves_the_assembled_runs_span_unstated() -> None:
    meta, meta_id = run("s5/manifest", logical_id=LogicalId("manifest", "s5"))
    p1 = run("s5/p1.mcap", first=at(0), last=at(9))[0]
    p2 = run("s5/p2.mcap", first=at(10), last=at(19))[0]
    p3, p3_id = run("s5/p3.bin")
    files = ["s5/p1.mcap", "s5/p2.mcap", "s5/p3.bin"]
    held = assembly("s5 paths", meta_id, [(f, REC) for f in files], rule="manifest.run")[0]
    result = consolidate({"p": [meta, p1, p2, p3, held, *(revision(f)[0] for f in files)]})
    s5 = run_node_of(meta)
    assert [c for c in result.claims if c.subject == s5] == []
    (span,) = [f for f in result.findings if meta_id in f.records]
    assert span.code == "runs.untimed_run" and p3_id in span.records


def test_an_open_ended_part_stays_a_possible_predecessor() -> None:
    a = run("bag/a.mcap", first=at(0))[0]  # the header states only the start
    b = run("bag/b.mcap", first=at(20), last=at(29))[0]
    c = run("bag/c.mcap", first=at(40), last=at(49))[0]
    result = _bag(a, b, c)
    assert of(result, "continues") == []
    pairs = {(x.subject, x.object) for x in of(result, "continues_candidate")}
    assert pairs == {
        (run_node_of(b), run_node_of(a)),
        (run_node_of(c), run_node_of(a)),
        (run_node_of(c), run_node_of(b)),
    }
    # involvement reads candidates alone, so "continues nothing" stays a reading too.
    assert readings_of(involvement(result.claims, run_node_of(c), "continues")) == {
        (),
        (run_node_of(a),),
        (run_node_of(b),),
    }
