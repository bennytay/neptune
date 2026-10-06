"""The status and safety-state kinds of schema version 10 (ADR 0071): shape, rules, JSON, schema."""

from dataclasses import replace
from typing import Any, Final

import pytest
from jsonschema import Draft202012Validator

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.ids import RecordId
from neptune.model.kinds import kinds_at, package_version
from neptune.model.knowledge import (
    AssertionKind,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance
from neptune.model.record import SCHEMA_VERSION, Family, SchemaVersionError
from neptune.model.schema import canonical_schema
from neptune.model.status import (
    STATUS_SINCE,
    SafetyCondition,
    SafetyState,
    StatusConvention,
    StatusReport,
    StatusValue,
    safety_state_from_json,
    status_report_from_json,
)
from neptune.model.time import Timestamp

DATA: Final = b"\x00\x01\x00\x00" + bytes(200)
SOURCE: Final = content_id(DATA)
MCAP: Final = transform_record(adapter_id="mcap", adapter_version="0.3.0", config={})
STREAM: Final = RecordId("rec:sha256:" + "1" * 64)
LOG_TIME: Final = RecordId("rec:sha256:" + "2" * 64)
HEADER: Final = RecordId("rec:sha256:" + "3" * 64)
VALIDATOR: Final = Draft202012Validator(canonical_schema())


def cite(
    offset: int, length: int, *inner: Any, kind: AssertionKind = AssertionKind.OBSERVED
) -> Provenance:
    return Provenance(EvidenceRef(SOURCE, (ByteRange(offset, length), *inner)), MCAP.id, kind)


MESSAGE: Final = cite(0, 120)
DEFINITION: Final = cite(150, 40, kind=AssertionKind.STATED)


def report(**changes: Any) -> StatusReport:
    provenance = cite(0, 120, ByteRange(40, 60))
    fields: dict[str, Any] = {
        "convention": StatusConvention.ROS_DIAGNOSTIC_STATUS,
        "times": (
            Known(Timestamp(1_790_000_000_000_000_000, LOG_TIME), MESSAGE),
            Unknown(MESSAGE),
        ),
        "level": Known(2),
        "level_names": Known(("ERROR",), DEFINITION),
        "name": Known("arm/joint_3"),
        "message": Known("following error 4.2 deg"),
        "hardware_id": Unknown(),
        "values": Known((StatusValue("error_deg", "4.2"), StatusValue("", ""))),
    }
    fields.update(changes)
    return StatusReport(
        id=evidence_record_id(StatusReport.kind, provenance.evidence, MCAP),
        provenance=provenance,
        stream=STREAM,
        **fields,
    )


def safety(**changes: Any) -> SafetyState:
    provenance = cite(0, 120, ByteRange(44, 1))
    fields: dict[str, Any] = {
        "declared_type": "industrial_msgs/RobotStatus",
        "field": "e_stopped.val",
        "condition": SafetyCondition.EMERGENCY_STOP,
        "times": (Known(Timestamp(5, LOG_TIME), MESSAGE), Known(Timestamp(4, HEADER), MESSAGE)),
        "value": Known(1),
        "value_names": Known(("CLOSED", "ENABLED", "HIGH", "ON", "TRUE"), DEFINITION),
    }
    fields.update(changes)
    return SafetyState(
        id=evidence_record_id(SafetyState.kind, provenance.evidence, MCAP),
        provenance=provenance,
        stream=STREAM,
        **fields,
    )


def logged() -> StatusReport:
    """A ULog logged message: what the format has no place for is not covered."""
    spec = cite(0, 7)
    return report(
        convention=StatusConvention.PX4_LOGGED_MESSAGE,
        times=(Known(Timestamp(5_800_010, LOG_TIME)),),
        level=Known(50),
        level_names=Known(("CRIT",), spec),
        name=NotCovered(spec),
        hardware_id=NotCovered(spec),
        values=NotCovered(spec),
    )


def ardupilot_error() -> StatusReport:
    fmt = cite(100, 89)
    return report(
        convention=StatusConvention.ARDUPILOT_ERROR,
        times=(NotCovered(),),
        level=NotCovered(fmt),
        level_names=NotCovered(fmt),
        name=NotCovered(fmt),
        message=NotCovered(fmt),
        hardware_id=NotCovered(fmt),
        values=Known((StatusValue("Subsys", 5), StatusValue("ECode", 1))),
    )


SAMPLES: Final = (
    (report(), status_report_from_json),
    (report(level_names=Known((), DEFINITION), values=Known(())), status_report_from_json),
    (report(level_names=Unknown(DEFINITION), values=Unknown()), status_report_from_json),
    (logged(), status_report_from_json),
    (ardupilot_error(), status_report_from_json),
    (safety(), safety_state_from_json),
    (
        safety(
            declared_type="husky_msgs/HuskyStatus",
            field="e_stop",
            value=Known(True),
            value_names=NotApplicable(),
        ),
        safety_state_from_json,
    ),
    (safety(value=Known(-1), value_names=Known(("UNKNOWN",), DEFINITION)), safety_state_from_json),
)


@pytest.mark.parametrize(("record", "read"), SAMPLES)
def test_each_kind_round_trips_and_validates_against_the_schema(record: Any, read: Any) -> None:
    data = canonical_json.loads(canonical_json.dumps(record.to_json()))
    assert read(data) == record
    assert isinstance(data, dict)
    assert (data["kind"], data["schema_version"]) == (record.kind, STATUS_SINCE)
    assert type(record).family is Family.RUN and type(record).since == STATUS_SINCE
    assert not list(VALIDATOR.iter_errors(data))


@pytest.mark.parametrize(("record", "read"), SAMPLES)
def test_a_newer_or_older_record_is_refused_by_its_version(record: Any, read: Any) -> None:
    with pytest.raises(SchemaVersionError):
        read({**record.to_json(), "schema_version": SCHEMA_VERSION + 1, "added_later": 1})
    with pytest.raises(SchemaVersionError, match=f"from schema version {STATUS_SINCE}"):
        read({**record.to_json(), "schema_version": STATUS_SINCE - 1})


def test_a_package_with_status_records_is_a_version_10_package() -> None:
    assert STATUS_SINCE == 10
    added = set(kinds_at(STATUS_SINCE)) - set(kinds_at(STATUS_SINCE - 1))
    assert added == {"safety_state", "status_report"}
    assert package_version(["run", "stream"]) == 1
    assert package_version(["stream", "status_report"]) == STATUS_SINCE


def test_a_known_list_that_inherits_is_the_bare_array() -> None:
    data = report().to_json()
    assert data["values"] == [{"key": "error_deg", "value": "4.2"}, {"key": "", "value": ""}]
    names = data["level_names"]
    assert isinstance(names, dict) and names["knowledge"] == "known"  # it cites the definition


def test_a_boolean_and_an_integer_are_different_values() -> None:
    one, true = safety(value=Known(1)), safety(value=Known(True), value_names=NotApplicable())
    assert canonical_json.dumps(one.to_json()["value"]) != canonical_json.dumps(
        true.to_json()["value"]
    )
    assert safety_state_from_json(true.to_json()).value == Known(True)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: report(level=Known(True)), "not a boolean"),
        (lambda: report(times=()), "at least one clock"),
        (
            lambda: report(times=(Known(Timestamp(1, LOG_TIME)), Known(Timestamp(2, LOG_TIME)))),
            "distinct clocks",
        ),
        (lambda: report(name=Known("")), "non-empty"),
        (lambda: report(values=KnownAbsent(DEFINITION)), "Known\\(\\(\\)\\)"),
        (lambda: report(level_names=Known(("WARN", "ERROR"))), "sorted"),
        (lambda: report(convention="ros_diagnostic_status"), "StatusConvention"),
        (lambda: safety(value=Known(True)), "value_names is NotApplicable"),
        (lambda: safety(value=Known("1")), "bool"),
        (lambda: safety(field=""), "non-empty"),
        (lambda: safety(condition="emergency_stop"), "SafetyCondition"),
    ],
)
def test_the_rules_are_enforced(build: Any, message: str) -> None:
    with pytest.raises((ValueError, TypeError), match=message):
        build()


def test_a_status_value_is_text_or_an_integer() -> None:
    with pytest.raises(TypeError):
        StatusValue("k", 1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StatusValue("k", True)
    data = dict(report().to_json())
    data["values"] = [{"key": "k", "value": 1.5}]
    with pytest.raises(ValueError, match="text or an integer"):
        status_report_from_json(data)


def test_the_reader_is_strict() -> None:
    data = report().to_json()
    with pytest.raises(ValueError, match="unexpected"):
        status_report_from_json({**data, "extra": 1})
    with pytest.raises(ValueError):
        status_report_from_json({**data, "level": {"knowledge": "known", "value": "2"}})
    with pytest.raises(ValueError):
        safety_state_from_json(
            {**safety().to_json(), "value": {"knowledge": "known", "value": 1.0}}
        )


def test_evidence_ids_cover_the_exact_bytes() -> None:
    """Two statuses of one message are two records: their byte ranges differ."""
    first = report()
    other = replace(first, provenance=cite(0, 120, ByteRange(100, 20)))
    assert evidence_record_id(StatusReport.kind, other.provenance.evidence, MCAP) != first.id
