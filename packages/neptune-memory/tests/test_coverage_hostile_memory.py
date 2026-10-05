"""Coverage (ADR 0015) on malformed, contradictory and boundary input, and its determinism.

Every bad record is a finding and never a claim, and never fails the build; every unstated or
undecidable value stays unclaimed rather than becoming a definite fact.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Final

import pytest

from memory_coverage_records import (
    SECOND,
    binding,
    component,
    configuration,
    finding,
    series,
    stream,
)
from memory_identity_records import Record, at, ledger
from memory_run_records import assembly, domain, revision, run
from neptune.identity import canonical_json
from neptune.model.alignment import MemberRole
from neptune.model.ids import LogicalId
from neptune.model.time import INT64_MAX
from neptune_memory.consolidate.base import (
    Consolidation,
    Consolidator,
    rebuild,
    run_consolidator,
)
from neptune_memory.consolidate.coverage import CoverageConsolidator
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.predicates import CORE_PREDICATES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.ids import RecordId
    from neptune.model.jsonvalue import JsonValue

TX = ledger_tx(3)
T0: Final = 5 * SECOND


def consolidate(packages: Mapping[str, Sequence[Record]], config: object = None) -> Consolidation:
    return run_consolidator(
        CoverageConsolidator(),
        ledger(packages),
        (),
        dict(config or {}),  # type: ignore[call-overload]
        recorded_at=TX,
        registry=CORE_PREDICATES,
    )


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def predicates(result: Consolidation) -> list[str]:
    return sorted(c.predicate for c in result.claims)


def base(
    *, count: int | None = 101, resolution_stated: bool = True
) -> tuple[list[Record], dict[str, RecordId]]:
    """A one-stream run on a nanosecond boot clock: declared 101 samples over [T0, T0 + 1 s]."""
    if resolution_stated:
        clock_record, clock = domain("boot", civil=False)
    else:
        clock_record, clock = domain("boot", civil=False)
        clock_record = {**clock_record, "resolution": {"knowledge": "unknown"}}
    run_record, run_id = run("arm.mcap", first=at(T0, clock), last=at(T0 + SECOND, clock))
    joints, joints_id = stream(
        "/joints", run_id, (clock,), count=count, first=at(T0, clock),
        last=at(T0 + SECOND, clock), recording="arm.mcap",
    )  # fmt: skip
    return [clock_record, run_record, joints], {"clock": clock, "run": run_id, "stream": joints_id}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: {k: v for k, v in r.items() if k != "rows_unknown"},
        lambda r: {**r, "extra": 1},
        lambda r: {**r, "first": True},
        lambda r: {**r, "first": "5"},
        lambda r: {**r, "last": r["first"] - 1},
        lambda r: {**r, "rows_known": 0},
        lambda r: {**r, "rows_known": 1},  # one row but first != last
        lambda r: {**r, "rows_unknown": -1},
        lambda r: {**r, "last": 2**63},
        lambda r: {**r, "stream": "not-a-record-id"},
        lambda r: {**r, "clock": None},
    ],
)
def test_malformed_series_rows_are_findings_and_place_nothing(mutate: object) -> None:
    records, ids = base()
    row = series(ids["stream"], ids["clock"], T0, T0 + SECOND, 101)
    result = consolidate({"p": [*records, mutate(row)]})  # type: ignore[operator]
    assert "coverage.malformed_record" in codes(result)
    assert predicates(result) == ["rate_declared"]  # only what the stream itself declares


@pytest.mark.parametrize(
    ("kind", "record"),
    [
        ("stream", {"kind": "stream", "id": "rec:sha256:" + "0" * 64}),
        ("ingest_finding", {"kind": "ingest_finding", "code": "x"}),
        ("hardware_component", {"kind": "hardware_component"}),
        ("snapshot_binding", {"kind": "snapshot_binding", "run": 5}),
        ("image", {"kind": "image", "capture": "\ud800"}),
        ("video", {"kind": "video"}),
        ("timestamp_domain", {"kind": "timestamp_domain", "resolution": "fast"}),
    ],
)
def test_malformed_compiler_records_are_findings(kind: str, record: Record) -> None:
    records, _ = base()
    result = consolidate({"p": [*records, record]})
    (malformed,) = [f for f in result.findings if f.code == "coverage.malformed_record"]
    assert malformed.details["kind"] == kind
    assert "rate_declared" in predicates(result)  # the rest of the build is unaffected


def test_a_blank_declared_sensor_identifier_is_malformed() -> None:
    records, ids = base()
    config, config_id = configuration("arm.urdf", LogicalId("asset-tag", "ARM-1"))
    blank = component("arm.urdf", config_id, "cam", LogicalId("serial", " "))[0]
    result = consolidate({"p": [*records, config, blank, binding("b", ids["run"], config_id)]})
    assert "coverage.malformed_record" in codes(result)
    assert not [c for c in result.claims if c.predicate.startswith("sensor_")]


def test_an_inferred_binding_is_never_a_ground() -> None:
    records, ids = base()
    config, config_id = configuration("arm.urdf", LogicalId("asset-tag", "ARM-1"))
    cam = component("arm.urdf", config_id, "cam", LogicalId("serial", "C1"))[0]
    bound = binding("b", ids["run"], config_id)
    inferred = {**bound, "provenance": {**bound["provenance"], "assertion_kind": "inferred"}}  # type: ignore[dict-item]
    result = consolidate({"p": [*records, config, cam, inferred]})
    assert "coverage.inferred_record" in codes(result)
    assert not [c for c in result.claims if c.predicate.startswith("sensor_")]


def test_two_packages_disagreeing_on_a_series_row_use_neither() -> None:
    records, ids = base()
    a = series(ids["stream"], ids["clock"], T0, T0 + SECOND, 101)
    b = series(ids["stream"], ids["clock"], T0, T0 + SECOND, 51)
    result = consolidate({"p": [*records, a], "q": [b]})
    assert "coverage.record_conflict" in codes(result)
    assert "recorded" not in predicates(result) and "rate_observed" not in predicates(result)
    # The same row in two packages is one row.
    again = consolidate({"p": [*records, a], "q": [a]})
    assert "coverage.record_conflict" not in codes(again)
    assert predicates(again).count("recorded") == 1


def test_dangling_references_are_findings() -> None:
    records, ids = base()
    clock = ids["clock"]
    orphan, orphan_id = stream("/orphan", "rec:sha256:" + "1" * 64, (clock,), recording="x")  # type: ignore[arg-type]
    missing_config = binding("b", ids["run"], "rec:sha256:" + "2" * 64)  # type: ignore[arg-type]
    result = consolidate(
        {"p": [*records, orphan, series(orphan_id, clock, 0, 10, 2), missing_config]}
    )
    assert {"coverage.dangling_stream", "coverage.dangling_binding"} <= set(codes(result))
    assert all(c.subject.node_id != f"record:{orphan_id}" for c in result.claims)


def test_unknown_config_is_reported_and_ignored() -> None:
    records, _ = base()
    result = consolidate({"p": records}, {"tolerance": 0.1})
    assert "coverage.unknown_config" in codes(result)
    assert predicates(result) == ["rate_declared"]


# --- Boundaries ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "rows", "first", "last", "reason"),
    [
        (1, 1, T0, T0, "fewer_than_two_samples"),
        (2, 2, T0, T0, "zero_span"),
    ],
)
def test_too_few_instants_determine_no_rate(
    count: int, rows: int, first: int, last: int, reason: str
) -> None:
    clock_record, clock = domain("boot", civil=False)
    run_record, run_id = run("x.mcap", first=at(T0, clock), last=at(T0, clock))
    one, one_id = stream(
        "/once", run_id, (clock,), count=count, first=at(first, clock), last=at(last, clock),
        recording="x.mcap",
    )  # fmt: skip
    result = consolidate(
        {"p": [clock_record, run_record, one, series(one_id, clock, first, last, rows)]}
    )
    assert "rate_declared" not in predicates(result) and "rate_observed" not in predicates(result)
    reasons = {
        f.details["reason"] for f in result.findings if f.code == "coverage.rate_undetermined"
    }
    assert reasons == {reason}
    assert predicates(result) == ["recorded"]  # one instant still holds over its one tick


def test_an_unstated_clock_resolution_gives_no_hertz() -> None:
    records, ids = base(resolution_stated=False)
    result = consolidate(
        {"p": [*records, series(ids["stream"], ids["clock"], T0, T0 + SECOND, 51)]}
    )
    assert predicates(result) == ["recorded"]
    reasons = {
        f.details["reason"] for f in result.findings if f.code == "coverage.rate_undetermined"
    }
    assert reasons == {"clock_resolution_unstated"}


def test_a_series_at_the_clocks_last_tick_is_not_placed() -> None:
    records, ids = base()
    row = series(ids["stream"], ids["clock"], INT64_MAX - 10, INT64_MAX, 11)
    result = consolidate({"p": [*records, row]})
    assert "coverage.end_unrepresentable" in codes(result)
    assert "recorded" not in predicates(result)


def test_samples_outside_the_declared_extent_claim_no_gap_on_that_side() -> None:
    records, ids = base()
    # Starts before the declared first (contradiction) and ends early (a trailing gap).
    row = series(ids["stream"], ids["clock"], T0 - 10, T0 + SECOND // 2, 51)
    result = consolidate({"p": [*records, row]})
    assert "coverage.extent_disagrees" in codes(result)
    (gap,) = [c for c in result.claims if c.predicate == "gap"]
    assert gap.valid_from.ticks == T0 + SECOND // 2 + 1


def test_a_declared_extent_on_two_clocks_predicts_nothing() -> None:
    clock_record, clock = domain("boot", civil=False)
    other_record, other = domain("header", civil=False)
    run_record, run_id = run("x.mcap", first=at(T0, clock), last=at(T0 + SECOND, clock))
    two, two_id = stream(
        "/two", run_id, (clock, other), count=11, first=at(T0, clock), last=at(T0, other),
        recording="x.mcap",
    )  # fmt: skip
    result = consolidate(
        {
            "p": [
                clock_record,
                other_record,
                run_record,
                two,
                series(two_id, clock, T0 + 5, T0 + 9, 5),
            ]
        }
    )
    assert "coverage.declared_extent_unusable" in codes(result)
    assert "gap" not in predicates(result) and "rate_declared" not in predicates(result)


def test_a_run_without_a_first_instant_or_inverted_places_no_claim() -> None:
    clock_record, clock = domain("boot", civil=False)
    for first, last in ((None, at(T0, clock)), (at(T0, clock), at(T0 - 1, clock))):
        run_record, run_id = run("y.bag", first=first, last=last)
        bad = finding("rosbag1.truncated", "y.bag")
        config, config_id = configuration("y.urdf", LogicalId("asset-tag", "Y"))
        cam = component("y.urdf", config_id, "cam", LogicalId("serial", "C"))[0]
        result = consolidate(
            {"p": [clock_record, run_record, bad, config, cam, binding("b", run_id, config_id)]}
        )
        assert result.claims == ()
        assert codes(result).count("coverage.unplaced_run") == 1


def test_a_run_last_on_another_clock_is_open_and_not_closed() -> None:
    clock_record, clock = domain("boot", civil=False)
    other_record, other = domain("gps", civil=False)
    run_record, run_id = run("z.bag", first=at(T0, clock), last=at(T0, other))
    config, config_id = configuration("z.urdf", LogicalId("asset-tag", "Z"))
    cam = component("z.urdf", config_id, "cam", LogicalId("serial", "C"))[0]
    result = consolidate(
        {
            "p": [
                clock_record,
                other_record,
                run_record,
                config,
                cam,
                binding("b", run_id, config_id),
            ]
        }
    )
    (claim,) = result.claims
    assert claim.predicate == "sensor_presence_unknown"
    (undecided,) = [f for f in result.findings if f.code == "coverage.presence_undecided"]
    assert undecided.details["reasons"] == ["files_not_attributed", "recording_not_closed"]


def test_a_run_with_nothing_unattributed_and_closed_makes_a_configured_sensor_absent() -> None:
    """The smallest known absence: a closed run a manifest declares, whose assembly holds no
    recording, no stream and no file of any sensor."""
    clock_record, clock = domain("boot", civil=False)
    run_record, run_id = run("w.yaml", first=at(T0, clock), last=at(T0 + SECOND, clock))
    files = assembly("w.yaml", run_id, [("w.yaml", MemberRole.DESCRIPTION)])[0]
    config, config_id = configuration("w.urdf", LogicalId("asset-tag", "W"))
    cam, cam_id = component("w.urdf", config_id, "cam", LogicalId("serial", "C"))
    nameless = component("w.urdf", config_id, "imu")[0]
    result = consolidate(
        {
            "p": [
                clock_record,
                run_record,
                files,
                revision("w.yaml")[0],
                config,
                cam,
                nameless,
                binding("b", run_id, config_id),
            ]
        }
    )
    assert predicates(result) == ["sensor_not_recorded", "sensor_not_recorded"]
    assert {c.object.node_id for c in result.claims} == {  # type: ignore[union-attr]
        "serial:C",
        f"record:{nameless['id']}",
    }
    del cam_id


# --- Determinism ---------------------------------------------------------------------------------


def _scene() -> dict[str, list[Record]]:
    records, ids = base()
    config, config_id = configuration("arm.urdf", LogicalId("asset-tag", "ARM-1"))
    cam = component("arm.urdf", config_id, "cam", LogicalId("serial", "C1"))[0]
    return {
        "p": [
            *records,
            series(ids["stream"], ids["clock"], T0 + 10, T0 + SECOND - 10, 99),
            finding("mcap.chunk_crc_mismatch", "arm.mcap"),
            config,
            cam,
            binding("b", ids["run"], config_id),
        ],
        "q": [finding("mcap.truncated", "arm.mcap", records=(ids["stream"],))],
    }


def _bytes(result: Consolidation) -> bytes:
    return canonical_json.dumps(result.to_json())


def test_same_ledger_same_bytes_in_any_record_or_package_order() -> None:
    scene = _scene()
    expected = _bytes(consolidate(scene))
    shuffler = random.Random(129)
    for _ in range(5):
        shuffled = {}
        for name in shuffler.sample(sorted(scene), len(scene)):
            records = list(scene[name])
            shuffler.shuffle(records)
            shuffled[name] = records
        assert _bytes(consolidate(shuffled)) == expected


def test_rebuild_is_byte_identical() -> None:
    scene = _scene()
    plan: list[tuple[Consolidator, Mapping[str, JsonValue]]] = [(CoverageConsolidator(), {})]
    first = rebuild(ledger(scene), plan, recorded_at=TX)
    second = rebuild(ledger(scene), plan, recorded_at=TX)
    assert [_bytes(r) for r in first] == [_bytes(r) for r in second]
    assert {c.predicate for c in first[0].claims} >= {
        "recorded",
        "gap",
        "rate_declared",
        "rate_observed",
        "integrity_finding",
        "sensor_presence_unknown",
    }


# --- Review findings, pinned ---------------------------------------------------------------------


def test_a_series_wholly_past_the_declared_extent_gaps_only_the_extent() -> None:
    records, ids = base()
    row = series(ids["stream"], ids["clock"], T0 + 5 * SECOND, T0 + 6 * SECOND, 101)
    result = consolidate({"p": [*records, row]})
    (gap,) = [c for c in result.claims if c.predicate == "gap"]
    assert (gap.valid_from.ticks, gap.valid_to.ticks) == (T0, T0 + SECOND + 1)  # type: ignore[union-attr]
    assert "coverage.extent_disagrees" in codes(result)


def test_a_recording_that_is_no_image_or_video_withholds_known_absence() -> None:
    clock_record, clock = domain("boot", civil=False)
    run_record, run_id = run("scan.yaml", first=at(T0, clock), last=at(T0 + SECOND, clock))
    files = assembly(
        "scan.yaml",
        run_id,
        [("scan.yaml", MemberRole.DESCRIPTION), ("cloud.pcd", MemberRole.RECORDING)],
    )[0]
    config, config_id = configuration("s.urdf", LogicalId("asset-tag", "S"))
    lidar = component("s.urdf", config_id, "lidar", LogicalId("serial", "L"))[0]
    result = consolidate(
        {
            "p": [
                clock_record,
                run_record,
                files,
                revision("scan.yaml")[0],
                revision("cloud.pcd")[0],
                config,
                lidar,
                binding("b", run_id, config_id),
            ]
        }
    )
    assert predicates(result) == ["sensor_presence_unknown"]
    (undecided,) = [f for f in result.findings if f.code == "coverage.presence_undecided"]
    assert undecided.details["reasons"] == ["files_not_attributed"]


def test_an_unreadable_run_record_anywhere_withholds_known_absence() -> None:
    clock_record, clock = domain("boot", civil=False)
    run_record, run_id = run("w.yaml", first=at(T0, clock), last=at(T0 + SECOND, clock))
    files = assembly("w.yaml", run_id, [("w.yaml", MemberRole.DESCRIPTION)])[0]
    config, config_id = configuration("w.urdf", LogicalId("asset-tag", "W"))
    cam = component("w.urdf", config_id, "cam", LogicalId("serial", "C"))[0]
    scene = [clock_record, run_record, files, revision("w.yaml")[0], config, cam]
    scene.append(binding("b", run_id, config_id))
    broken_records: list[Record] = [
        {"kind": "run_assembly", "run": run_id},
        {"kind": "stream", "run": run_id},
    ]
    for broken in broken_records:
        result = consolidate({"p": scene, "q": [broken]})
        assert predicates(result) == ["sensor_presence_unknown"], broken
        (undecided,) = [f for f in result.findings if f.code == "coverage.presence_undecided"]
        assert undecided.details["reasons"] == ["ledger_records_unreadable"]


def test_a_run_ending_on_another_clock_of_one_civil_timeline_is_closed() -> None:
    a_record, a = domain("ntp a", civil=True)
    b_record, b = domain("ntp b", civil=True)
    run_record, run_id = run("v.yaml", first=at(T0, a), last=at(T0 + SECOND, b))
    files = assembly("v.yaml", run_id, [("v.yaml", MemberRole.DESCRIPTION)])[0]
    config, config_id = configuration("v.urdf", LogicalId("asset-tag", "V"))
    cam = component("v.urdf", config_id, "cam", LogicalId("serial", "C"))[0]
    result = consolidate(
        {
            "p": [
                a_record,
                b_record,
                run_record,
                files,
                revision("v.yaml")[0],
                config,
                cam,
                binding("b", run_id, config_id),
            ]
        }
    )
    (claim,) = result.claims
    assert claim.predicate == "sensor_not_recorded"
    assert claim.valid_to.ticks == T0 + SECOND + 1  # type: ignore[union-attr]


def test_series_rows_naming_no_stream_or_clock_are_findings() -> None:
    records, ids = base()
    other_record, other = domain("other", civil=False)
    rows = [
        series("rec:sha256:" + "3" * 64, ids["clock"], 0, 9, 10),  # type: ignore[arg-type]
        series(ids["stream"], other, 0, 9, 10),
    ]
    result = consolidate({"p": [*records, other_record, *rows]})
    assert codes(result).count("coverage.dangling_series") == 2
    assert "recorded" not in predicates(result)


def test_a_declared_last_at_the_clocks_last_tick_places_no_gap() -> None:
    clock_record, clock = domain("boot", civil=False)
    run_record, run_id = run("u.mcap", first=at(T0, clock), last=at(T0 + SECOND, clock))
    edge, edge_id = stream(
        "/edge", run_id, (clock,), count=3, first=at(T0, clock), last=at(INT64_MAX, clock),
        recording="u.mcap",
    )  # fmt: skip
    row = series(edge_id, clock, T0 + 1, T0 + 2, 2)
    result = consolidate({"p": [clock_record, run_record, edge, row]})
    assert "gap" not in predicates(result)
    assert "coverage.end_unrepresentable" in codes(result)
