"""Declared civil time zones and lists that can be blank (ADR 0061).

The fixture is one CMMS work-order export row as a lifecycle mapper reads it: a local civil time
whose zone the export's header declares, and a replaced-parts cell left blank.
"""

import calendar
import io
from dataclasses import replace
from datetime import datetime
from typing import Any, Final

import pytest
from jsonschema import Draft202012Validator

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id, digest_stream
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import LogicalId
from neptune.model.kinds import KIND_SINCE, RECORD_KINDS, kinds_at, record_version
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
from neptune.model.lifecycle import LIFECYCLE_SINCE, MaintenanceEvent, PartReplacement
from neptune.model.lists import LIST_STATES_SINCE
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, Row, RowCell
from neptune.model.record import SCHEMA_VERSION, Family, SchemaVersionError
from neptune.model.reference import (
    CIVIL_ZONE_SINCE,
    CivilTimeZone,
    TimestampDomain,
    check_iana_zone,
    civil_time_zone_from_json,
)
from neptune.model.schema import canonical_schema
from neptune.model.source import LocalPath
from neptune.model.time import SECOND, ClockRole, Epoch, Timestamp
from neptune.store.package import MANIFEST, PackageError, package_files, read_files, table_path
from neptune.store.receipt import build_receipt

EXPORT: Final = (
    b"# timezone: Europe/Berlin\n"
    b"work_order,robot,performed,parts_replaced\n"
    b"WO-1182,ARM-3,2026-03-04 14:10,\n"
)
SOURCE: Final = content_id(EXPORT)
MAPPER: Final = transform_record(
    adapter_id="deploy.cmms_generic", adapter_version="0.1.0", config={"zone": "Europe/Berlin"}
)
STATED: Final = AssertionKind.STATED
VALIDATOR: Final = Draft202012Validator(canonical_schema())
COLUMNS: Final = ("work_order", "robot", "performed", "parts_replaced")


def cite(*steps: Any) -> Provenance:
    return Provenance(EvidenceRef(SOURCE, (ByteRange(0, len(EXPORT)), *steps)), MAPPER.id, STATED)


def cell(column: str) -> Provenance:
    return cite(RowCell(0, COLUMNS.index(column), column))


# The performed column: civil seconds on Berlin's clock, never converted (ADR 0023 §2).
CLOCK_AT: Final = cell("performed")
CLOCK: Final = TimestampDomain(
    id=evidence_record_id("timestamp_domain", CLOCK_AT.evidence, MAPPER),
    provenance=CLOCK_AT,
    field="performed",
    scope=(),
    role=Known(ClockRole.DOCUMENT),
    resolution=Known(SECOND),
    epoch=Known(Epoch.UNIX),
    timescale=Unknown(),
    declared_monotonic=Unknown(),
)
HEADER: Final = cite(Row(0))  # the export's "# timezone:" line declares the zone


def zone(state: Any, provenance: Provenance = HEADER) -> CivilTimeZone:
    return CivilTimeZone(
        id=evidence_record_id("civil_time_zone", provenance.evidence, MAPPER),
        provenance=provenance,
        domain=CLOCK.id,
        zone=state,
    )


def civil_ticks(text: str) -> int:
    """Seconds from 1970-01-01T00:00:00 of the same civil clock: the wall time, not an instant."""
    return calendar.timegm(datetime.fromisoformat(text).timetuple())


def work_order(**change: Any) -> MaintenanceEvent:
    row = cite(Row(2))
    values: dict[str, Any] = {
        "identifiers": Known((Known(LogicalId("cmms.work_order", "WO-1182"), cell("work_order")),)),
        "site": NotCovered(),
        "machines": Known((Known(LogicalId("robot.serial", "ARM-3"), cell("robot")),)),
        "configuration": NotCovered(),
        "related": NotCovered(),
        "performed": Known(Timestamp(civil_ticks("2026-03-04 14:10"), CLOCK.id), cell("performed")),
        "diagnosis": NotCovered(),
        "actions": NotCovered(),
        # The cell is blank: the export says nothing about parts. Never () (non-negotiable 3).
        "parts": Unknown(cell("parts_replaced")),
    }
    return MaintenanceEvent(
        id=evidence_record_id("maintenance_event", row.evidence, MAPPER),
        provenance=row,
        **{**values, **change},
    )


def package_records(*extra: Any) -> list[Any]:
    ledger = SourceLedger()
    ledger.observe(LocalPath("cmms/work_orders.csv"), digest_stream(io.BytesIO(EXPORT)))
    return [*ledger.artifacts(), *ledger.revisions(), MAPPER, CLOCK, *extra]


def line(record: Any) -> Any:
    return canonical_json.loads(canonical_json.dumps(record.to_json()))


# --- Civil time zones --------------------------------------------------------------------------


def test_the_kind_is_a_reference_companion_added_at_its_own_version() -> None:
    assert RECORD_KINDS["civil_time_zone"][0] is CivilTimeZone
    assert CivilTimeZone.family is Family.REFERENCE
    assert KIND_SINCE["civil_time_zone"] == CIVIL_ZONE_SINCE <= SCHEMA_VERSION
    assert set(kinds_at(CIVIL_ZONE_SINCE)) - set(kinds_at(CIVIL_ZONE_SINCE - 1)) == {
        "civil_time_zone"
    }


def test_a_cmms_row_with_a_declared_zone_round_trips_it_through_a_package() -> None:
    berlin, event = zone(Known("Europe/Berlin")), work_order()
    files = package_files(package_records(berlin, event))
    package = read_files(files)
    assert package.manifest.version == CIVIL_ZONE_SINCE
    assert files[table_path("civil_time_zone")] == canonical_json.dumps(berlin.to_json()) + b"\n"
    read = {record.id: record for record in package.records if hasattr(record, "provenance")}
    assert read[berlin.id] == berlin
    assert read[berlin.id].zone == Known("Europe/Berlin")  # verbatim, as declared
    assert read[event.id] == event
    # Nothing converted: the ticks are still 14:10 on the civil clock, the domain unchanged.
    assert read[event.id].performed == Known(
        Timestamp(civil_ticks("2026-03-04 14:10"), CLOCK.id), cell("performed")
    )
    assert read[CLOCK.id] == CLOCK and CLOCK.timescale == Unknown()
    assert package.files() == files


@pytest.mark.parametrize(
    "state",
    [
        Known("Europe/Berlin"),
        Unknown(),  # the export could say and does not
        NotCovered(cite()),  # its format has no place for a zone
        Ambiguous((Candidate("Europe/Berlin", HEADER), Candidate("Europe/Vienna", cite(Row(1))))),
    ],
    ids=["known", "unknown", "not_covered", "ambiguous"],
)
def test_zone_states_round_trip_strictly_and_validate(state: Any) -> None:
    record = zone(state)
    data = line(record)
    assert data["schema_version"] == CIVIL_ZONE_SINCE
    assert civil_time_zone_from_json(data) == record
    assert canonical_json.dumps(civil_time_zone_from_json(data).to_json()) == (
        canonical_json.dumps(record.to_json())
    )
    assert list(VALIDATOR.iter_errors(data)) == []
    with pytest.raises(SchemaVersionError, match=f"from schema version {CIVIL_ZONE_SINCE}"):
        civil_time_zone_from_json({**data, "schema_version": CIVIL_ZONE_SINCE - 1})
    with pytest.raises(ValueError):
        civil_time_zone_from_json({**data, "utc_offset": "+01:00"})  # nothing derived rides along


@pytest.mark.parametrize(
    "name",
    ["Europe/Berlin", "UTC", "Etc/GMT-5", "America/Argentina/Buenos_Aires", "EST5EDT", "GMT+0"],
)
def test_iana_names_are_checked_by_spelling_only(name: str) -> None:
    check_iana_zone("zone", name)
    assert zone(Known(name)).zone == Known(name)


def test_a_well_spelled_name_no_tz_database_knows_is_kept_as_declared() -> None:
    # Validity depends on a tz database release; the record's bytes may not (ADR 0061 §1).
    assert zone(Known("Mars/Olympus_Mons")).zone == Known("Mars/Olympus_Mons")


@pytest.mark.parametrize(
    "name",
    [
        "",
        "W. Europe Standard Time",
        "+01:00",
        "Europe//Berlin",
        "/Europe/Berlin",
        "Europe/Berlin/",
        "../etc/passwd",
        "Europe/./Berlin",
        "Europe/Berlin\n",
        "x" * 256,
        "-Europe/Berlin",
        "Europe/-Berlin",
    ],
)
def test_names_not_spelled_as_iana_zones_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="IANA"):
        zone(Known(name))


@pytest.mark.parametrize(
    "state",
    [KnownAbsent(HEADER), NotApplicable(), Known(3), "Europe/Berlin"],
    ids=["known_absent", "not_applicable", "not_text", "bare"],
)
def test_a_civil_clock_always_has_a_zone_so_absence_is_refused(state: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        zone(state)


def test_the_zone_names_a_record_id() -> None:
    with pytest.raises(ValueError):
        replace(zone(Known("UTC")), domain="Europe/Berlin")  # type: ignore[arg-type]


# --- Lists that can be blank -------------------------------------------------------------------


def test_a_blank_list_cell_is_unknown_never_an_empty_list() -> None:
    event = work_order()
    data = line(event)
    assert data["parts"] == {"knowledge": "unknown", "provenance": cell("parts_replaced").to_json()}
    assert data["schema_version"] == LIST_STATES_SINCE == record_version(event)
    assert MaintenanceEvent.from_json(data) == event
    assert MaintenanceEvent.from_json(data).parts != Known(())
    assert list(VALIDATOR.iter_errors(data)) == []


def test_declared_empty_is_known_and_keeps_the_version_4_bytes() -> None:
    def bare(**lists: Any) -> MaintenanceEvent:
        return work_order(related=Known(()), actions=Known(()), parts=Known(()), **lists)

    event = bare()
    data = line(event)
    assert data["parts"] == [] and data["related"] == [] and data["actions"] == []
    assert data["schema_version"] == LIFECYCLE_SINCE == record_version(event)
    assert MaintenanceEvent.from_json(data) == event
    assert list(VALIDATOR.iter_errors(data)) == []


def test_a_known_list_citing_its_own_cell_is_a_state_object() -> None:
    parts = Known(
        (
            PartReplacement(
                Known("joint 4 drive"),
                Known((Known(LogicalId("serial", "JD-0098")),)),
                Unknown(),  # the installed serial cell is blank
            ),
        ),
        cell("parts_replaced"),
    )
    event = work_order(parts=parts)
    data = line(event)
    assert data["parts"]["knowledge"] == "known"
    assert data["parts"]["value"][0]["installed"] == {"knowledge": "unknown"}
    assert data["schema_version"] == LIST_STATES_SINCE
    assert MaintenanceEvent.from_json(data) == event
    # A nested part's state alone raises the record's version too.
    nested = work_order(parts=Known(parts.value), actions=Known(()), related=Known(()))
    assert line(nested)["parts"][0]["installed"] == {"knowledge": "unknown"}
    assert record_version(nested) == LIST_STATES_SINCE
    assert list(VALIDATOR.iter_errors(line(nested))) == []


def test_list_state_lines_are_refused_below_their_version_and_in_another_spelling() -> None:
    data = line(work_order())
    with pytest.raises(SchemaVersionError, match=f"uses schema version {LIST_STATES_SINCE}"):
        MaintenanceEvent.from_json({**data, "schema_version": LIFECYCLE_SINCE})
    with pytest.raises(ValueError, match="written as an array"):
        MaintenanceEvent.from_json({**data, "parts": {"knowledge": "known", "value": []}})
    with pytest.raises(ValueError):
        MaintenanceEvent.from_json({**data, "parts": "none"})
    with pytest.raises(ValueError, match="KnownAbsent"):
        work_order(parts=KnownAbsent(cell("parts_replaced")))
    # An item in doubt is an Ambiguous item of a Known list; the whole list is never Ambiguous.
    with pytest.raises(ValueError, match="Ambiguous"):
        work_order(
            machines=Ambiguous[tuple[Any, ...]](
                (
                    Candidate((Known(LogicalId("robot.serial", "ARM-3")),), cell("robot")),
                    Candidate((Known(LogicalId("robot.serial", "ARM-4")),), cell("robot")),
                )
            )
        )
    in_doubt = Ambiguous(
        (
            Candidate(LogicalId("robot.serial", "ARM-3"), cell("robot")),
            Candidate(LogicalId("robot.serial", "ARM-8"), cell("robot")),
        )
    )
    assert line(work_order(machines=Known((in_doubt,))))["machines"][0]["knowledge"] == "ambiguous"


@pytest.mark.parametrize(
    "parts",
    [
        {"knowledge": "known_absent", "provenance": cell("parts_replaced").to_json()},
        {"knowledge": "known", "value": []},  # the bare array written another way
        {"knowledge": "ambiguous", "candidates": [{"value": []}, {"value": [{}]}]},
        "none",
    ],
    ids=["known_absent", "inherited_known_object", "ambiguous", "text"],
)
def test_the_schema_refuses_list_shapes_the_reader_refuses(parts: Any) -> None:
    data = {**line(work_order()), "parts": parts}
    assert list(VALIDATOR.iter_errors(data)) != []
    with pytest.raises(ValueError):
        MaintenanceEvent.from_json(data)


def test_same_row_same_bytes() -> None:
    first = package_files(package_records(zone(Known("Europe/Berlin")), work_order()))
    again = package_files(package_records(work_order(), zone(Known("Europe/Berlin"))))
    assert first == again


def test_a_package_is_written_at_its_records_version_not_only_its_kinds() -> None:
    blank, bare = work_order(), work_order(related=Known(()), actions=Known(()), parts=Known(()))
    assert read_files(package_files(package_records(bare))).manifest.version == LIFECYCLE_SINCE
    files = package_files(package_records(blank))
    assert read_files(files).manifest.version == LIST_STATES_SINCE
    # The same lines under a version 4 manifest are refused: one set of records, one package.
    lower = package_files(package_records(bare))
    lower[table_path("maintenance_event")] = files[table_path("maintenance_event")]
    with pytest.raises(PackageError):
        read_files(lower)
    with pytest.raises(ValueError, match="later"):
        build_receipt(package_records(blank), version=LIFECYCLE_SINCE)
    assert MANIFEST in files


def test_the_schema_describes_both_list_shapes_and_the_new_kind() -> None:
    schema = canonical_schema()
    defs = schema["$defs"]
    assert isinstance(defs, dict)
    assert "CivilTimeZone" in defs
    listed = defs["Listed_PartReplacement"]
    assert isinstance(listed, dict)
    shapes = listed["anyOf"]
    assert isinstance(shapes, list)
    assert shapes[0] == {"items": {"$ref": "#/$defs/PartReplacement"}, "type": "array"}
    event = defs["MaintenanceEvent"]
    assert isinstance(event, dict)
    properties = event["properties"]
    assert isinstance(properties, dict)
    assert properties["schema_version"] == {"enum": [LIFECYCLE_SINCE, LIST_STATES_SINCE]}
    assert properties["parts"] == {"$ref": "#/$defs/Listed_PartReplacement"}


@pytest.mark.parametrize(
    "zone_json",
    [
        {"knowledge": "known_absent", "provenance": HEADER.to_json()},
        {"knowledge": "not_applicable"},
    ],
    ids=["known_absent", "not_applicable"],
)
def test_the_schema_refuses_zone_states_the_reader_refuses(zone_json: Any) -> None:
    data = {**line(zone(Known("UTC"))), "zone": zone_json}
    assert list(VALIDATOR.iter_errors(data)) != []
    with pytest.raises(ValueError):
        civil_time_zone_from_json(data)


def test_a_lifecycle_line_declares_exactly_the_version_its_content_needs() -> None:
    bare = line(work_order(related=Known(()), actions=Known(()), parts=Known(())))
    with pytest.raises(SchemaVersionError):
        MaintenanceEvent.from_json({**bare, "schema_version": LIST_STATES_SINCE})
