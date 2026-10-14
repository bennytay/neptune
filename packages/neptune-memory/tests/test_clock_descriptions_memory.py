"""``memory.clocks`` (ADR 0029): each clock described as its records declare it, never converted."""

from __future__ import annotations

import json
from fractions import Fraction
from typing import TYPE_CHECKING, Final, Protocol

import pytest
from jsonschema import Draft202012Validator

from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.ids import parse_record_id
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, TransformRecord
from neptune.model.reference import CivilTimeZone, TimestampDomain
from neptune.model.time import INT64_MIN, ClockRole, Epoch, Timescale
from neptune_memory.consolidate.base import rebuild
from neptune_memory.consolidate.clocks import CLOCKS_CONSOLIDATOR_ID, ClockDescriptionConsolidator
from neptune_memory.ledger import StubLedger
from neptune_memory.schema.claim import TypedLiteral, ValueType
from neptune_memory.schema.clock_description import (
    ClockDescription,
    clock_description_from_json,
)
from neptune_memory.schema.codec import object_from_json
from neptune_memory.schema.export import graph_schema
from neptune_memory.schema.interval import OPEN, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.ids import RecordId
    from neptune.model.jsonvalue import JsonObject, JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune_memory.consolidate.base import Consolidation, Consolidator

Record = dict[str, object]


class Encodable(Protocol):
    def to_json(self) -> JsonObject: ...


MAPPING: Final = transform_record(
    adapter_id="deploy_event_log_map", adapter_version="0.1.0", config={"zone": "declared"}
)
OTHER: Final = transform_record(adapter_id="cmms_reader", adapter_version="2", config={})
STATED, OBSERVED = AssertionKind.STATED, AssertionKind.OBSERVED


def cite(name: str) -> EvidenceRef:
    return EvidenceRef(content_id(name.encode()), (ByteRange(0, 16),))


def domain(
    name: str,
    *,
    kind: AssertionKind = STATED,
    resolution: Knowledge[Fraction] | None = None,
    epoch: Knowledge[Epoch] | None = None,
    timescale: Knowledge[Timescale] | None = None,
) -> TimestampDomain:
    """A clock as a Deploy mapping declares a wall clock: 1 s ticks from the Unix epoch of its
    own wall clock, timescale unstated (root ADR 0023 §2)."""
    evidence = cite(name)
    return TimestampDomain(
        id=evidence_record_id("timestamp_domain", evidence, MAPPING),
        provenance=Provenance(evidence, MAPPING.id, kind),
        field=name,
        scope=("syslog events",),
        role=Known(ClockRole.DOCUMENT),
        resolution=Known(Fraction(1)) if resolution is None else resolution,
        epoch=Known(Epoch.UNIX) if epoch is None else epoch,
        timescale=Unknown() if timescale is None else timescale,
        declared_monotonic=NotCovered(),
    )


def zone(
    clock: TimestampDomain,
    state: Knowledge[str],
    tag: str = "",
    by: TransformRecord = MAPPING,
) -> CivilTimeZone:
    evidence = cite(f"{clock.field} zone {tag}")
    return CivilTimeZone(
        id=evidence_record_id("civil_time_zone", evidence, by),
        provenance=Provenance(evidence, by.id, STATED),
        domain=clock.id,
        zone=state,
    )


def ledger(*packages: Sequence[Encodable | Record]) -> StubLedger:
    def encoded(record: Encodable | Record) -> Record:
        return record if isinstance(record, dict) else dict(record.to_json())

    return StubLedger(
        {f"pkg-{i}": (1, [encoded(r) for r in records]) for i, records in enumerate(packages)}
    )


def run(stub: StubLedger, config: Mapping[str, JsonValue] | None = None) -> Consolidation:
    plan: list[tuple[Consolidator, Mapping[str, JsonValue]]] = [
        (ClockDescriptionConsolidator(), config or {})
    ]
    (result,) = rebuild(stub, plan, recorded_at=ledger_tx(1))
    return result


def described(result: Consolidation) -> dict[RecordId, ClockDescription]:
    out: dict[RecordId, ClockDescription] = {}
    for claim in result.claims:
        assert claim.predicate == "clock_description"
        assert isinstance(claim.object, TypedLiteral)
        assert isinstance(claim.object.value, ClockDescription)
        out[parse_record_id(claim.subject.node_id)] = claim.object.value
    return out


# --- What a description holds --------------------------------------------------------------------


def test_a_declared_zone_is_carried_with_the_adapter_that_declared_it() -> None:
    syslog = domain("Timestamp")
    result = run(ledger([syslog, zone(syslog, Known("America/New_York")), MAPPING]))
    assert not result.findings
    (claim,) = result.claims
    assert claim.subject == NodeRef(NodeType.CLOCK, syslog.id)
    assert claim.assertion_kind is STATED
    assert claim.valid.start.ticks == INT64_MIN and claim.valid.start.domain_id == syslog.id
    assert claim.valid.end is OPEN
    description = described(result)[syslog.id]
    assert description.civil_time_zone == Known("America/New_York")
    assert description.zone_declared_by == Known("deploy_event_log_map")
    assert description.is_wall_clock
    assert description.resolution == Known(Fraction(1)) and description.epoch == Known(Epoch.UNIX)
    assert description.timescale == Unknown()  # never filled in from the zone
    assert (description.field, description.scope) == ("Timestamp", ("syslog events",))
    # It cites the domain and the zone, and names both records and the mapping's transform.
    assert set(claim.provenance.evidence) == {syslog.provenance.evidence, cite("Timestamp zone ")}
    assert set(claim.provenance.records) >= {syslog.id, MAPPING.id}


def test_a_zone_the_source_leaves_unstated_stays_unknown_and_is_still_a_wall_clock() -> None:
    log = domain("Completed")
    description = described(run(ledger([log, zone(log, Unknown()), MAPPING])))[log.id]
    assert description.civil_time_zone == Unknown()
    assert description.is_wall_clock


def test_a_clock_no_zone_record_names_is_not_applicable_never_a_zone() -> None:
    boot = domain("log_time", kind=OBSERVED, epoch=Unknown(), resolution=Known(Fraction(1, 10**9)))
    result = run(ledger([boot]))
    (claim,) = result.claims
    description = described(result)[boot.id]
    assert description.civil_time_zone == NotApplicable()
    assert description.zone_declared_by == NotApplicable()
    assert not description.is_wall_clock
    assert description.epoch == Unknown()
    assert claim.assertion_kind is OBSERVED


def test_a_transform_record_not_in_the_ledger_leaves_the_declarer_unknown() -> None:
    log = domain("Stopped")
    description = described(run(ledger([log, zone(log, Known("Europe/Berlin"))])))[log.id]
    assert description.civil_time_zone == Known("Europe/Berlin")
    assert description.zone_declared_by == Unknown()


def test_two_zone_records_leave_the_zone_in_doubt_even_when_they_agree() -> None:
    log = domain("Stopped")
    agree = run(ledger([log, zone(log, Known("UTC"), "a"), zone(log, Known("UTC"), "b"), MAPPING]))
    assert [f.code for f in agree.findings] == ["clocks.zone_repeated"]
    assert described(agree)[log.id].civil_time_zone == Unknown()
    differ = run(
        ledger(
            [log, zone(log, Known("UTC"), "a"), zone(log, Known("Asia/Tokyo"), "b", OTHER)],
            [MAPPING, OTHER],
        )
    )
    description = described(differ)[log.id]
    assert description.civil_time_zone == Ambiguous((Candidate("Asia/Tokyo"), Candidate("UTC")))
    assert description.zone_declared_by == Unknown()  # two adapters declare it


def test_a_state_citing_its_own_evidence_is_lifted_to_the_claim() -> None:
    own = Provenance(cite("header"), MAPPING.id, OBSERVED)
    clock = domain("t", resolution=Known(Fraction(1, 1000), own))
    result = run(ledger([clock]))
    (claim,) = result.claims
    assert described(result)[clock.id].resolution == Known(Fraction(1, 1000))
    assert cite("header") in claim.provenance.evidence
    assert claim.assertion_kind is OBSERVED  # one observed citation: not every one is stated


# --- Hostile and malformed input -----------------------------------------------------------------


def test_a_malformed_domain_is_a_finding_and_the_rest_is_described() -> None:
    good = domain("good")
    broken: Record = dict(domain("broken").to_json())
    broken["epoch"] = {"knowledge": "known", "value": "the dawn of time"}
    result = run(ledger([broken, good]))
    assert [f.code for f in result.findings] == ["clocks.malformed_record"]
    assert list(described(result)) == [good.id]


def test_one_id_with_two_contents_is_used_nowhere() -> None:
    clock = domain("t")
    other: Record = dict(clock.to_json())
    other["field"] = "u"
    result = run(ledger([clock], [other]))
    assert [f.code for f in result.findings] == ["clocks.record_conflict"]
    assert not result.claims


def test_a_property_stated_absent_grounds_no_description() -> None:
    clock = domain("t", epoch=KnownAbsent(Provenance(cite("t"), MAPPING.id, STATED)))
    result = run(ledger([clock]))
    assert [f.code for f in result.findings] == ["clocks.unreadable_state"]
    assert not result.claims


def test_config_keys_are_reported_and_ignored() -> None:
    result = run(ledger([domain("t")]), {"zone": "UTC"})
    assert [f.code for f in result.findings] == ["clocks.unknown_config"]
    assert len(result.claims) == 1


# --- Determinism and the literal -----------------------------------------------------------------


def test_package_order_and_repeats_never_change_a_claim() -> None:
    a, b = domain("a"), domain("b")
    one = run(ledger([a, zone(a, Known("UTC")), MAPPING], [b]))
    two = run(ledger([b], [MAPPING, zone(a, Known("UTC")), a], [a]))
    assert [c.id for c in one.claims] == [c.id for c in two.claims]
    assert len(one.claims) == 2


def test_the_literal_round_trips_and_validates_against_the_published_schema() -> None:
    syslog = domain("Timestamp")
    (claim,) = run(ledger([syslog, zone(syslog, Known("America/New_York")), MAPPING])).claims
    encoded = claim.object.to_json()
    assert object_from_json(json.loads(json.dumps(encoded))) == claim.object
    schema = graph_schema()
    validator = Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/TypedLiteral"})
    assert not list(validator.iter_errors(encoded))
    assert encoded["datatype"] == str(ValueType.CLOCK_DESCRIPTION)


@pytest.mark.parametrize(
    "change",
    [
        {"civil_time_zone": None},
        {"civil_time_zone": {"knowledge": "known", "value": "+05:00"}},
        {"civil_time_zone": {"knowledge": "not_applicable"}},  # but a declarer is named
        {"zone_declared_by": {"knowledge": "not_applicable"}},  # but a zone is declared
        {"resolution": {"knowledge": "known", "value": {"denominator": 1, "numerator": 0}}},
        {"epoch": {"knowledge": "known", "value": "noon"}},
        {"extra": 1},
        {
            "timescale": {
                "knowledge": "known",
                "provenance": Provenance(cite("x"), MAPPING.id, STATED).to_json(),
                "value": "posix",
            }
        },
    ],
)
def test_the_literal_reader_refuses_what_to_json_never_writes(change: dict[str, object]) -> None:
    syslog = domain("Timestamp")
    (claim,) = run(ledger([syslog, zone(syslog, Known("America/New_York")), MAPPING])).claims
    data = {**claim.object.value.to_json(), **change}  # type: ignore[union-attr]
    with pytest.raises((ValueError, TypeError)):
        clock_description_from_json(data)


def test_the_consolidator_id_is_registered_by_default() -> None:
    from neptune_memory.consolidate.snapshot import default_registrations

    assert CLOCKS_CONSOLIDATOR_ID in {r.consolidator_id for r in default_registrations()}
