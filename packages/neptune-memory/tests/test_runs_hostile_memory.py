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
    by_record,
    declaration,
    domain,
    mapping,
    revision,
    run,
    site,
)
from neptune.identity import canonical_json
from neptune.model.alignment import MemberRole
from neptune.model.ids import LogicalId
from neptune.model.run import run_from_json
from neptune.model.time import INT64_MAX, Epoch, Timescale, Timestamp
from neptune_memory.consolidate.base import Consolidation, rebuild, run_consolidator
from neptune_memory.consolidate.identity import IdentityConsolidator
from neptune_memory.consolidate.runs import RunConsolidator, run_node
from neptune_memory.schema.interval import OPEN, CivilClock, ledger_tx
from neptune_memory.schema.predicates import CORE_PREDICATES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.consolidate.base import Consolidator
    from neptune_memory.schema.claim import Claim
    from neptune_memory.schema.nodes import NodeRef

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
        ("run_declaration", {**declaration("d", AMR), "extra": 1}),
        ("run_declaration", _without(declaration("d", AMR), "site")),
        ("run_declaration", {**declaration("d", AMR), "evidence": []}),
        ("run_declaration", {**declaration("d", AMR), "machine": {"knowledge": "maybe"}}),
        ("run_declaration", {**declaration("d", AMR), "run": {"namespace": "Bad", "value": "x"}}),
        ("run_declaration", {**declaration("d", AMR), "id": 7}),
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
                declaration("d", by_record(absent), site=LogicalId("site", "WH-1")),
                declaration("e", LogicalId("manifest", "never-recorded")),
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
    # a and b overlap: concurrent, no link. c starts the tick after b ends: c continues b.
    (link,) = of(result, "continues")
    assert (link.subject, link.object) == (run_node_of(c), run_node_of(b))
    assert of(result, "continues_candidate") == []


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
            declaration(
                "d", by_record(boot_id), site=LogicalId("site", "WH-2"), task=LogicalId("task", "t")
            ),
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
