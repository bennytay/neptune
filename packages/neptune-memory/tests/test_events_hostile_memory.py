"""The event consolidator on hostile, malformed, boundary and reordered input (ADR 0013).

Every problem is a structured finding and the rest of the build is unaffected; an ``Ambiguous`` or
``Unknown`` value never becomes a definite claim; the same Ledger gives byte-identical output
whatever the package or record order.
"""

from __future__ import annotations

import random
from fractions import Fraction
from typing import TYPE_CHECKING, Final

import pytest

from memory_event_records import SECOND, incident, intervention, table
from memory_identity_records import Record, ambiguous, ledger
from memory_run_records import domain, mapping
from neptune.identity import canonical_json
from neptune.model.alignment import ValidityWindow
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import Ambiguous, Known, to_json
from neptune.model.time import INT64_MAX, Timestamp
from neptune_memory.consolidate.base import Consolidation, run_consolidator
from neptune_memory.consolidate.event_records import (
    DEFAULT_CONFIG,
    parse_config,
    resolve_config,
)
from neptune_memory.consolidate.events import MAX_LISTED, EventConsolidator, event_node
from neptune_memory.consolidate.runs import involvement
from neptune_memory.schema.interval import OPEN, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue

TX = ledger_tx(3)
CLOCK, CLOCK_ID = domain("controller", civil=False)
OTHER, OTHER_ID = domain("robot boot", civil=False)


def consolidate(
    packages: Mapping[str, Sequence[Record]], config: Mapping[str, JsonValue] | None = None
) -> Consolidation:
    return run_consolidator(
        EventConsolidator(),
        ledger(packages),
        (),
        resolve_config(config),
        recorded_at=TX,
        registry=CORE_PREDICATES,
    )


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def at(ticks: int) -> Timestamp:
    return Timestamp(ticks, CLOCK_ID)


EVENTS: Final = {
    "vendors": {"plc": {"text": {"E-STOP": "emergency_stop", "OK": "not_an_event"}}},
    "tables": [
        {
            "name": "plc alarms",
            "vendor": "plc",
            "kind": "code",
            "at": {"ticks": "t"},
            "clock": {"record": CLOCK_ID},
            "machine": {"column": "cell", "namespace": "plc.cell"},
            "zone": {"column": "zone", "namespace": "plc.zone"},
            "severity": "prio",
        }
    ],
}


def plc(*rows: Sequence[object], name: str = "plc alarms") -> tuple[list[Record], list[RecordId]]:
    records, _, ids = table(name, ("t", "code", "cell", "zone", "prio"), rows)  # type: ignore[arg-type]
    return records, ids


# --- Malformed records ----------------------------------------------------------------------------


def test_a_malformed_record_is_a_finding_and_the_rest_is_read() -> None:
    good, good_id = incident("good", occurred=at(5))
    broken = {**good, "occurred": "yesterday"}
    wrong_kind = {**good, "id": "rec:sha256:" + "0" * 64, "severity": 7}
    result = consolidate({"p": [CLOCK, good, broken, wrong_kind, {"kind": "intervention"}]})
    assert codes(result).count("events.malformed_record") == 3
    assert {c.subject for c in result.claims} == {event_node(good_id)}


def test_an_inferred_record_is_never_a_ground() -> None:
    record, _ = incident("guess", occurred=at(5))
    record["provenance"] = {**record["provenance"], "assertion_kind": "inferred"}  # type: ignore[dict-item]
    result = consolidate({"p": [CLOCK, record]})
    assert result.claims == ()
    assert codes(result) == ["events.inferred_record"]


def test_one_record_id_with_two_contents_is_used_nowhere() -> None:
    record, _ = incident("x", occurred=at(5), severity="S1")
    forged = {**record, "severity": {"knowledge": "known", "value": "S3"}}
    result = consolidate({"a": [CLOCK, record], "b": [forged]})
    assert result.claims == ()
    assert "events.record_conflict" in codes(result)


def test_the_same_record_in_two_packages_is_one_event() -> None:
    record, rid = incident("x", occurred=at(5))
    once = consolidate({"a": [CLOCK, record]})
    twice = consolidate({"a": [CLOCK, record], "b": [CLOCK, record]})
    assert once.claims == twice.claims
    assert not of_code(twice, "events.record_conflict")
    assert {c.subject for c in twice.claims} == {event_node(rid)}


def of_code(result: Consolidation, code: str) -> list[object]:
    return [f for f in result.findings if f.code == code]


# --- Times ----------------------------------------------------------------------------------------


def test_an_untimed_or_ambiguously_timed_event_has_no_claim() -> None:
    blank, _ = incident("blank time", occurred=None, severity="S2")
    record, _ = incident("two times", occurred=at(5))
    record["occurred"] = to_json(ambiguous("two times", at(5), at(9)), Timestamp.to_json)
    result = consolidate({"p": [CLOCK, blank, record]})
    assert result.claims == ()
    assert codes(result) == ["events.ambiguous_time", "events.untimed_event"]


def test_an_intervention_ending_before_it_starts_is_not_placed() -> None:
    record, _ = intervention("backwards", start=at(50), end=at(10))
    result = consolidate({"p": [CLOCK, record]})
    assert result.claims == ()
    assert codes(result) == ["events.inverted_interval"]


def test_an_end_on_another_clock_leaves_the_event_open() -> None:
    record, _ = intervention("two clocks", start=at(50), end=Timestamp(60, OTHER_ID))
    result = consolidate({"p": [CLOCK, OTHER, record]})
    assert {c.valid_to for c in result.claims} == {OPEN}
    assert "events.end_on_other_clock" in codes(result)


def test_an_instant_at_the_last_tick_is_open_and_a_projection_past_it_is_a_finding() -> None:
    last, last_id = incident("last tick", occurred=at(INT64_MAX))
    mapped, _ = incident("mapped", occurred=Timestamp(INT64_MAX - 10, OTHER_ID))
    push = mapping("push", OTHER_ID, CLOCK_ID, anchor=(0, 1_000))
    result = consolidate({"p": [CLOCK, OTHER, last, mapped, push]})
    assert {c.valid_to for c in result.claims if c.subject == event_node(last_id)} == {OPEN}
    assert "events.projection_out_of_range" in codes(result)


def test_a_mapping_whose_window_does_not_cover_the_event_is_not_used() -> None:
    one, _ = incident("one", occurred=Timestamp(10 * SECOND, OTHER_ID))
    two, _ = incident("two", occurred=at(10 * SECOND))
    stale = mapping("old sync", OTHER_ID, CLOCK_ID, anchor=(0, 0), window=(0, SECOND))
    result = consolidate({"p": [CLOCK, OTHER, one, two, stale]})
    assert not [c for c in result.claims if c.predicate == "co_occurs_within"]
    assert "events.clocks_unrelated" in codes(result)


def test_an_ambiguous_mapping_window_counts_only_where_its_readings_agree() -> None:
    one, _ = incident("one", occurred=Timestamp(10 * SECOND, OTHER_ID))
    two, _ = incident("two", occurred=at(10 * SECOND))
    sync = mapping("sync", OTHER_ID, CLOCK_ID, anchor=(0, 0), bound=0)
    readings = ambiguous(
        "sync",
        ValidityWindow(
            OTHER_ID, Known(Timestamp(0, OTHER_ID)), Known(Timestamp(60 * SECOND, OTHER_ID))
        ),
        ValidityWindow(
            OTHER_ID, Known(Timestamp(0, OTHER_ID)), Known(Timestamp(5 * SECOND, OTHER_ID))
        ),
    )
    sync["validity"] = to_json(readings, ValidityWindow.to_json)
    result = consolidate({"p": [CLOCK, OTHER, one, two, sync]})
    assert not [c for c in result.claims if c.predicate == "co_occurs_within"]  # not all agree
    assert "events.ambiguous_window" in codes(result)


def test_many_unrelated_clocks_are_listed_up_to_a_bound() -> None:
    records: list[Record] = []
    for k in range(12):
        clock, clock_id = domain(f"boot {k}", civil=False)
        report, _ = incident(f"report {k}", occurred=Timestamp(5, clock_id))
        records += [clock, report]
    result = consolidate({"p": records})
    unrelated = [f for f in result.findings if f.code == "events.clocks_unrelated"]
    assert len(unrelated) == MAX_LISTED + 1  # 66 pairs: the first listed, then "more"
    assert len([f for f in unrelated if "clocks" not in f.details]) == 1


# --- Event tables ---------------------------------------------------------------------------------


def test_table_rows_become_events_and_hostile_cells_become_findings() -> None:
    records, ids = plc(
        (100, "E-STOP", "CELL-3", "Z1", 1),  # an event
        (200, "OK", "CELL-3", "Z1", 0),  # declared not an event
        (300, "WHO?", "CELL-3", "Z1", 1),  # unmapped: kind Unknown
        (400, None, "CELL-3", "Z1", 1),  # blank kind
        ("12:00:01", "E-STOP", "CELL-3", "Z1", 1),  # a time written as text: never parsed
        (500, "E-STOP", " CELL-3", None, 1),  # a padded id and a blank zone
        (600, "E-STOP", True, "Z1", 1.5),  # an id that is no text, a severity that is no level
    )
    result = consolidate({"p": [CLOCK, *records]}, EVENTS)  # type: ignore[arg-type]
    estop, ok, unmapped, blank, text_time, padded, odd = (event_node(r) for r in ids)
    subjects = {c.subject for c in result.claims}
    assert subjects == {estop, unmapped, blank, padded, odd}
    assert ok not in subjects and text_time not in subjects
    kinds = {c.subject: c.object.value for c in result.claims if c.predicate == "event_kind"}  # type: ignore[union-attr]
    assert kinds == {estop: "emergency_stop", padded: "emergency_stop", odd: "emergency_stop"}
    assert involvement(result.claims, estop, "involves") is not None
    assert not [
        c for c in result.claims if c.subject in (padded, odd) and c.predicate == "involves"
    ]
    assert not [c for c in result.claims if c.subject == odd and c.predicate == "stated_severity"]
    found = codes(result)
    for code in (
        "events.kind_unmapped",
        "events.kind_unstated",
        "events.untimed_event",
        "events.id_unusable",
    ):
        assert code in found, code


def test_a_table_lacking_a_declared_column_is_not_read() -> None:
    records, _, _ = table("plc alarms", ("t", "code", "cell"), [(100, "E-STOP", "CELL-3")])
    result = consolidate({"p": [CLOCK, *records]}, EVENTS)  # type: ignore[arg-type]
    assert result.claims == ()
    assert codes(result) == ["events.table_unusable"]


def test_a_table_the_config_does_not_declare_is_not_events() -> None:
    records, _ = plc((100, "E-STOP", "CELL-3", "Z1", 1), name="asset register")
    assert consolidate({"p": [CLOCK, *records]}, EVENTS).claims == ()  # type: ignore[arg-type]


def test_an_ambiguous_place_is_candidates_never_a_definite_one() -> None:
    record, rid = incident(
        "two zones",
        occurred=at(5),
        site=ambiguous("two zones", LogicalId("site", "A"), LogicalId("site", "B")),
        zone=ambiguous("two zones", LogicalId("zone", "A/1"), LogicalId("zone", "B/1")),
    )
    result = consolidate({"p": [CLOCK, record]})
    event = event_node(rid)
    predicates = {c.predicate for c in result.claims}
    assert "at_site" not in predicates and "in_zone" not in predicates
    assert {c.object for c in result.claims if c.predicate == "in_zone_candidate"} == {
        NodeRef(NodeType.ZONE, "zone:A/1"),
        NodeRef(NodeType.ZONE, "zone:B/1"),
    }
    assert isinstance(involvement(result.claims, event, "at_site"), Ambiguous)


# --- Config ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "fragment"),
    [
        ({"surprise": 1}, "unexpected keys"),
        ({"co_occurrence": {"window_seconds": "-1"}}, "plain decimal"),
        ({"co_occurrence": {"window_seconds": "soon"}}, "plain decimal"),
        ({"co_occurrence": {"window_seconds": "Infinity"}}, "plain decimal"),
        ({"co_occurrence": {"window_seconds": "1e30"}}, "plain decimal"),
        ({"co_occurrence": {"window_seconds": 86_401}}, "at most"),
        ({"co_occurrence": {"window_seconds": 0}}, "positive"),
        ({"co_occurrence": {"max_partners": 0}}, "max_partners"),
        ({"vendors": {"x": {"text": {"A": "explosion"}}}}, "not a registered event kind"),
        ({"vendors": {"incident_record": {"text": {"S1": "not_an_event"}}}}, "always an event"),
        ({"vendors": {"incident_record": {"integer": {"2": "incident"}}}}, "unexpected keys"),
        ({"vendors": {"x": {"integer": {"02": "fault"}}}}, "written canonically"),
        ({"vendors": {"x": {"A": "fault"}}}, "unexpected keys"),
        ({"vendors": [1]}, "vendors must be an object"),
        ({"tables": {"name": "x"}}, "tables must be a list"),
        ({"tables": [{"name": "x"}]}, "missing"),
        ({"tables": [{**EVENTS["tables"][0], "vendor": "nobody"}]}, "declares no mapping"),  # type: ignore[index]
        ({"tables": [{**EVENTS["tables"][0], "clock": {"record": "clock"}}]}, "record id"),  # type: ignore[index]
        ({"tables": [{**EVENTS["tables"][0], "at": {"ticks": " t"}}]}, "whitespace"),  # type: ignore[index]
        (
            {"tables": [{**EVENTS["tables"][0], "zone": {"column": "z", "namespace": "record"}}]},  # type: ignore[index]
            "reserved",
        ),
    ],
)
def test_an_unusable_config_part_is_refused_alone(config: dict[str, object], fragment: str) -> None:
    merged = {**EVENTS, **config}
    if "vendors" not in config and "tables" in config:
        merged["vendors"] = EVENTS["vendors"]
    parsed = parse_config(resolve_config(merged))  # type: ignore[arg-type]
    assert any(fragment in problem for problem in parsed.problems), parsed.problems


def test_an_invalid_config_is_a_finding_and_valid_parts_still_run() -> None:
    records, ids = plc((100, "E-STOP", "CELL-3", "Z1", 1))
    config = {**EVENTS, "co_occurrence": {"window_seconds": "0"}, "extra": True}
    result = consolidate({"p": [CLOCK, *records]}, config)  # type: ignore[arg-type]
    assert codes(result).count("events.invalid_config") == 2
    assert {c.subject for c in result.claims} == {event_node(ids[0])}


def test_defaults_resolve_to_one_config_hash() -> None:
    assert resolve_config(None) == resolve_config({}) == resolve_config(dict(DEFAULT_CONFIG))
    assert resolve_config({"co_occurrence": {"max_partners": 64}}) == resolve_config({})
    for spelling in (5, "5", "5.0", "5.000000000"):
        assert resolve_config({"co_occurrence": {"window_seconds": spelling}}) == resolve_config()
    half = resolve_config({"co_occurrence": {"window_seconds": "0.50"}})
    assert half["co_occurrence"]["window_seconds"] == "0.5"  # type: ignore[index,call-overload]


def test_a_window_shorter_than_a_tick_is_named_as_such() -> None:
    seconds, seconds_id = domain("rtc", civil=False, resolution=Fraction(1))
    one, _ = incident("one", occurred=Timestamp(5, seconds_id))
    two, _ = incident("two", occurred=Timestamp(5, seconds_id))
    result = consolidate({"p": [seconds, one, two]}, {"co_occurrence": {"window_seconds": "0.5"}})
    assert "events.window_below_resolution" in codes(result)
    assert "events.window_unscaled" not in codes(result)


def test_a_padded_id_in_a_lifecycle_record_is_not_a_node() -> None:
    record, rid = incident(
        "padded",
        occurred=at(5),
        machines=[LogicalId("serial", " SPOT-1")],
        site=LogicalId("site", "S"),
    )
    result = consolidate({"p": [CLOCK, record]})
    predicates = {c.predicate for c in result.claims if c.subject == event_node(rid)}
    assert "involves" not in predicates and "at_site" in predicates
    assert "events.id_unusable" in codes(result)


# --- Co-occurrence boundaries ---------------------------------------------------------------------


def test_co_occurrence_is_capped_nearest_first() -> None:
    """One panel alarm and four PLC rows (one export, so never paired with each other)."""
    rows, ids = plc(*((i * SECOND // 10, "E-STOP", "CELL-3", "Z1", 1) for i in range(1, 5)))
    alarm, alarm_id = incident("alarm panel", occurred=Timestamp(0, OTHER_ID))
    sync = mapping("sync", OTHER_ID, CLOCK_ID, anchor=(0, 0), bound=0)
    config = {**EVENTS, "co_occurrence": {"max_partners": 2}}
    result = consolidate({"p": [CLOCK, OTHER, sync, alarm, *rows]}, config)  # type: ignore[arg-type]
    partners = {
        c.object for c in result.claims if c.predicate == "co_occurs_within" and
        c.subject == event_node(alarm_id)
    }  # fmt: skip
    assert partners == {event_node(ids[0]), event_node(ids[1])}
    capped = [f for f in result.findings if f.code == "events.co_occurrence_capped"]
    assert [f.details["event"] for f in capped] == [
        event_node(alarm_id).node_id
    ]  # only the full one


def test_dense_logs_from_two_sources_stay_bounded() -> None:
    """Two exports with 1,500 alarms each at one instant: every pair would be 2.25 million; the
    sweep takes at most ``max_partners`` per event, and the findings stay one per clock."""
    rows = [(100, "E-STOP", "CELL-3", "Z1", 1)] * 1500
    first, _ = plc(*rows)
    second, _, _ = table("plc alarms", ("t", "code", "cell", "zone", "prio"), rows, file="mirror")
    config = {**EVENTS, "co_occurrence": {"max_partners": 2}}
    result = consolidate({"a": [CLOCK, *first], "b": second}, config)  # type: ignore[arg-type]
    pairs = [c for c in result.claims if c.predicate == "co_occurs_within"]
    assert 0 < len(pairs) <= 2 * 1500 * 2


def test_a_huge_residual_bound_is_undecided_once_per_clock_not_per_pair() -> None:
    rows, _ = plc(*((i * SECOND, "E-STOP", "CELL-3", "Z1", 1) for i in range(30)))
    panel = {**EVENTS["tables"][0], "name": "panel", "clock": {"record": OTHER_ID}}  # type: ignore[index]
    alarms, _, _ = table(
        "panel",
        ("t", "code", "cell", "zone", "prio"),
        [(i * SECOND, "E-STOP", "C", "Z", 1) for i in range(30)],
    )
    vague = mapping("vague", OTHER_ID, CLOCK_ID, anchor=(0, 0), bound=10**15)
    config = {**EVENTS, "tables": [*EVENTS["tables"], panel]}
    result = consolidate({"p": [CLOCK, OTHER, vague, *rows, *alarms]}, config)  # type: ignore[arg-type]
    assert not [c for c in result.claims if c.predicate == "co_occurs_within"]
    assert codes(result).count("events.co_occurrence_undecided") == 1


# --- Determinism ----------------------------------------------------------------------------------


def _scenario() -> dict[str, list[Record]]:
    records, _ = plc((100, "E-STOP", "CELL-3", "Z1", 1), (200, "E-STOP", "CELL-3", "Z1", 2))
    report, _ = incident(
        "report", occurred=Timestamp(150, OTHER_ID), machines=[LogicalId("cmms", "C3")]
    )
    sync = mapping("sync", OTHER_ID, CLOCK_ID, anchor=(0, 0), bound=0)
    assist, _ = intervention("assist", start=at(120), end=at(400), mode="on-site")
    return {"a": [CLOCK, *records], "b": [OTHER, report, sync], "c": [assist]}


def test_output_is_byte_identical_whatever_the_order() -> None:
    packages = _scenario()
    first = canonical_json.dumps(consolidate(packages, EVENTS).to_json())  # type: ignore[arg-type]
    rng = random.Random(134)
    for _ in range(5):
        names = list(packages)
        rng.shuffle(names)
        shuffled = {}
        for name in names:
            records = list(packages[name])
            rng.shuffle(records)
            shuffled[name] = records
        again = consolidate(shuffled, EVENTS)  # type: ignore[arg-type]
        assert canonical_json.dumps(again.to_json()) == first
    assert any(c.predicate == "co_occurs_within" for c in consolidate(packages, EVENTS).claims)  # type: ignore[arg-type]


# --- Ends, bounds, partial coverage and typed kinds (review of #125) -----------------------------


def test_an_unstated_intervention_end_leaves_it_open_never_an_instant() -> None:
    record, rid = intervention("takeover", start=at(100 * SECOND), end=None, mode="teleop")
    result = consolidate({"p": [CLOCK, record]})
    assert {c.valid_to for c in result.claims if c.subject == event_node(rid)} == {OPEN}
    (finding,) = [f for f in result.findings if f.code == "events.end_unstated"]
    assert finding.details["event"] == event_node(rid).node_id


def test_an_ambiguous_intervention_end_keeps_its_readings_and_stays_open() -> None:
    record, rid = intervention("takeover", start=at(100 * SECOND), mode="teleop")
    record["end"] = to_json(
        ambiguous("takeover", at(400 * SECOND), at(900 * SECOND)), Timestamp.to_json
    )
    result = consolidate({"p": [CLOCK, record]})
    assert {c.valid_to for c in result.claims if c.subject == event_node(rid)} == {OPEN}
    (finding,) = [f for f in result.findings if f.code == "events.end_ambiguous"]
    assert finding.details["readings"] == [at(400 * SECOND).to_json(), at(900 * SECOND).to_json()]
    assert "events.value_ambiguous" not in codes(result)  # no claim of "no reading" that is false


def test_a_blank_end_cell_leaves_the_row_open() -> None:
    spec = {**EVENTS["tables"][0], "end": {"ticks": "t_end"}}  # type: ignore[index]
    records, _, ids = table(
        "plc alarms",
        ("t", "t_end", "code", "cell", "zone", "prio"),
        [
            (100 * SECOND, None, "E-STOP", "C3", "Z1", 1),
            (100 * SECOND, 160 * SECOND, "E-STOP", "C3", "Z1", 1),
        ],
    )
    result = consolidate({"p": [CLOCK, *records]}, {**EVENTS, "tables": [spec]})  # type: ignore[dict-item]
    blank, stated = (event_node(r) for r in ids)
    assert {c.valid_to for c in result.claims if c.subject == blank} == {OPEN}
    assert {c.valid_to for c in result.claims if c.subject == stated} == {at(160 * SECOND)}
    assert codes(result).count("events.end_unstated") == 1


def test_a_mapping_that_states_no_bound_decides_no_co_occurrence() -> None:
    one, one_id = incident("one", occurred=Timestamp(10 * SECOND, OTHER_ID))
    two, _ = incident("two", occurred=at(10 * SECOND))
    sync = mapping("sync without bound", OTHER_ID, CLOCK_ID, anchor=(0, 0))
    result = consolidate({"p": [CLOCK, OTHER, one, two, sync]})
    assert not [c for c in result.claims if c.predicate == "co_occurs_within"]
    found = codes(result)
    assert "events.co_occurrence_unbounded" in found and "events.bound_unstated" in found
    # The event is still placed on the mapped clock, citing the mapping.
    clocks = {c.valid_from.domain_id for c in result.claims if c.subject == event_node(one_id)}
    assert clocks == {OTHER_ID, CLOCK_ID}


def test_events_a_mapping_window_does_not_cover_are_named_not_skipped() -> None:
    rows, ids = plc((50 * SECOND, "E-STOP", "C3", "Z1", 1), (200 * SECOND, "E-STOP", "C3", "Z1", 1))
    early, _ = incident("early", occurred=Timestamp(50 * SECOND, OTHER_ID))
    late, _ = incident("late", occurred=Timestamp(200 * SECOND, OTHER_ID))
    sync = mapping("sync", OTHER_ID, CLOCK_ID, anchor=(0, 0), bound=0, window=(0, 100 * SECOND))
    result = consolidate({"p": [CLOCK, OTHER, sync, early, late, *rows]}, EVENTS)  # type: ignore[arg-type]
    pairs = {(c.subject, c.object) for c in result.claims if c.predicate == "co_occurs_within"}
    assert any(event_node(ids[0]) in pair for pair in pairs)
    assert not any(event_node(ids[1]) in pair for pair in pairs)
    (partial,) = [f for f in result.findings if f.code == "events.clocks_unrelated"]
    assert partial.details["partial"] is True
    assert partial.details["events"] in ([1, 2], [2, 1])  # the late incident; both PLC rows


def test_an_integer_kind_and_a_text_kind_are_never_one_key() -> None:
    rows, ids = plc((100, 2, "C3", "Z1", 1), (200, "2", "C3", "Z1", 1))
    config = {
        **EVENTS,
        "vendors": {"plc": {"integer": {"2": "fault"}, "text": {"2": "warning"}}},
    }
    result = consolidate({"p": [CLOCK, *rows]}, config)  # type: ignore[arg-type]
    kinds = {c.subject: c.object for c in result.claims if c.predicate == "event_kind"}
    assert kinds[event_node(ids[0])].value == "fault"  # type: ignore[union-attr]
    assert kinds[event_node(ids[1])].value == "warning"  # type: ignore[union-attr]
    only_integer = {**EVENTS, "vendors": {"plc": {"integer": {"2": "fault"}}}}
    result = consolidate({"p": [CLOCK, *rows]}, only_integer)  # type: ignore[arg-type]
    kinds = {c.subject: c.object for c in result.claims if c.predicate == "event_kind"}
    assert event_node(ids[1]) not in kinds  # the text "2" is not coerced to the level 2
    (unmapped,) = [f for f in result.findings if f.code == "events.kind_unmapped"]
    assert unmapped.details["declared_type"] == "text"
