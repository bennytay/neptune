"""Status and safety-state records: what a typed log stream's own messages report (ADR 0071).

Both are evidence records (ADR 0017) of the ``run`` family, ``observed``, since schema version 9.
Each is one statement a message makes because its *declared* type is a status or safety type,
never because of its topic's name (a guess from a name is derived, ADR 0071 §2):

- A ``StatusReport`` is one status a message reports: one ``diagnostic_msgs/DiagnosticStatus``
  (alone, or one item of a ``DiagnosticArray``), one PX4 ULog logged message, one ArduPilot
  DataFlash ``MSG`` or ``ERR`` record. ``convention`` names which.
- A ``SafetyState`` is one sample of a field that its declared type defines as a stop or safety
  state: an industrial controller's ``e_stopped``, a UR arm's safety mode, PX4's kill switch.

Everything is as the message states it: a level is the integer on the wire, its names are the
constants the stream's own definition declares for that value (or the format specification's),
text is verbatim and a blank is ``Unknown``. Times are the sample's on each of its stream's
clocks, in the stream's order, as the series row holds them: nothing is converted to UTC, and no
clock is chosen as the "real" one. ``provenance`` cites the message's bytes, or a status's or a
field's exact bytes inside it; ``stream`` is the ``Stream`` the same transform declared.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final

from neptune.model._fields import (
    check_text_values,
    check_type,
    enum_decoder,
    exact_object,
    is_int,
    json_array,
    json_str,
    text_decoder,
    values_of,
)
from neptune.model.ids import RecordId, check_text, check_verbatim, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import Knowledge, NotApplicable, from_json, to_json
from neptune.model.lists import Listed, check_listed, listed_from_json, listed_to_json
from neptune.model.provenance import (
    Provenance,
    check_evidence_record,
    evidence_record_json,
    evidence_record_object,
    provenance_from_json,
)
from neptune.model.record import Family
from neptune.model.time import Timestamp, timestamp_from_json

# The schema version that added the status kinds; their records are written at it (ADR 0037 §1).
STATUS_SINCE: Final = 9


class StatusConvention(StrEnum):
    """Which declared status a ``StatusReport`` reads, and so which of its fields can be stated."""

    # diagnostic_msgs/DiagnosticStatus, alone or as an item of a diagnostic_msgs/DiagnosticArray
    ROS_DIAGNOSTIC_STATUS = "ros_diagnostic_status"
    PX4_LOGGED_MESSAGE = "px4_logged_message"  # a ULog logged message, `L` or tagged `C`
    ARDUPILOT_MESSAGE = "ardupilot_message"  # a DataFlash MSG record: text
    ARDUPILOT_ERROR = "ardupilot_error"  # a DataFlash ERR record: a subsystem and an error code


class SafetyCondition(StrEnum):
    """What a declared field reports, as its type's definition defines it (ADR 0071 §2)."""

    EMERGENCY_STOP = "emergency_stop"  # an emergency stop or kill switch, engaged or not
    FAULT = "fault"  # a controller's own error flag
    SAFETY_MODE = "safety_mode"  # a safety controller's mode: normal, a stop, a violation, ...


# A status's value as stated: a ROS key/value's text, or an integer field's number.
StatusScalar = str | int


@dataclass(frozen=True)
class StatusValue:
    """One key and value a status states, verbatim and in source order: a ROS ``KeyValue``, or
    an integer field of a format whose status has no key/values of its own (an ArduPilot ``ERR``'s
    ``Subsys`` and ``ECode``, a tagged ULog message's ``tag``), keyed by the field's name."""

    key: str
    value: StatusScalar

    def __post_init__(self) -> None:
        check_verbatim("status key", self.key)
        if isinstance(self.value, str):
            check_verbatim(f"status value {self.key!r}", self.value)
        elif not is_int(self.value):
            raise TypeError(f"a status value is text or an integer, got {self.value!r}")

    def to_json(self) -> JsonObject:
        return {"key": self.key, "value": self.value}


def status_value_from_json(data: JsonValue) -> StatusValue:
    obj = exact_object(data, "status value", {"key", "value"})
    value = obj["value"]
    if not isinstance(value, str) and not is_int(value):
        raise ValueError(f"a status value is text or an integer, got {value!r}")
    return StatusValue(json_str(obj["key"], "status key"), value)


def _check_times(times: tuple[Knowledge[Timestamp], ...]) -> None:
    if not isinstance(times, tuple) or not times:
        raise ValueError("a sample carries at least one clock: times holds one state per clock")
    domains: list[str] = []
    for time in times:
        check_type("times", time, Timestamp)
        domains += [stamp.domain_id for stamp in values_of(time)]
    if len(set(domains)) != len(domains):
        raise ValueError(f"times are on distinct clocks, got {domains}")


def _times_json(times: tuple[Knowledge[Timestamp], ...]) -> list[JsonValue]:
    return [to_json(time, Timestamp.to_json) for time in times]


def _times_from_json(data: JsonValue) -> tuple[Knowledge[Timestamp], ...]:
    return tuple(
        from_json(item, timestamp_from_json, provenance_from_json)
        for item in json_array(data, "times")
    )


def _check_names(name: str, names: tuple[str, ...]) -> None:
    if not isinstance(names, tuple):
        raise TypeError(f"{name} must be a tuple of names, got {type(names).__name__}")
    for item in names:
        if not isinstance(item, str):
            raise TypeError(f"{name} must be names, got {item!r}")
        check_text(name, item)
    if list(names) != sorted(set(names)):
        raise ValueError(f"{name} are unique and sorted: {names}")


def _names_from_json(data: JsonValue) -> tuple[str, ...]:
    return tuple(json_str(item, "name") for item in json_array(data, "names"))


def _names_json(names: tuple[str, ...]) -> JsonValue:
    return list(names)


def _check_values(name: str, values: tuple[StatusValue, ...]) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple, got {type(values).__name__}")
    for item in values:
        if not isinstance(item, StatusValue):
            raise TypeError(f"not a StatusValue: {item!r}")


def _values_from_json(data: JsonValue) -> tuple[StatusValue, ...]:
    return tuple(status_value_from_json(item) for item in json_array(data, "values"))


def _values_json(values: tuple[StatusValue, ...]) -> JsonValue:
    return [item.to_json() for item in values]


def _level_from_json(data: JsonValue) -> int:
    if not is_int(data):
        raise ValueError(f"a level is an integer, got {data!r}")
    return data


@dataclass(frozen=True)
class StatusReport:
    """One status a message reports, as stated (module docstring; ADR 0071 §1).

    - ``level``: the level as the integer the message holds (a ``DiagnosticStatus`` ``level``
      byte, a ULog ``log_level``); ``NotCovered`` where the format has no level (ArduPilot).
    - ``level_names``: the names the stream's own definition gives that value (its constants:
      ``ERROR``), or the format specification's (ULog's ``ERR``); ``Known(())`` where it names
      other values and not this one, ``Unknown`` where it declares no names, ``NotCovered`` with
      no level.
    - ``name``, ``message``, ``hardware_id``: the status's text, verbatim; ``Unknown`` where blank
      or not UTF-8, ``NotCovered`` where the format has no such field.
    - ``values``: the key/values it states, in order (``Known(())`` for none); ``Unknown`` where a
      key or value is not UTF-8, ``NotCovered`` where the format has none.
    """

    kind: ClassVar[str] = "status_report"
    family: ClassVar[Family] = Family.RUN
    since: ClassVar[int] = STATUS_SINCE
    id: RecordId
    provenance: Provenance
    stream: RecordId
    convention: StatusConvention
    times: tuple[Knowledge[Timestamp], ...]
    level: Knowledge[int]
    level_names: Listed[str]
    name: Knowledge[str]
    message: Knowledge[str]
    hardware_id: Knowledge[str]
    values: Listed[StatusValue]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.stream)
        if not isinstance(self.convention, StatusConvention):
            raise TypeError(f"convention must be a StatusConvention, got {self.convention!r}")
        _check_times(self.times)
        check_type("level", self.level, int)
        if any(isinstance(level, bool) for level in values_of(self.level)):
            raise ValueError("a level is an integer, not a boolean")
        check_listed("level_names", self.level_names, _check_names)
        for name in ("name", "message", "hardware_id"):
            check_text_values(name, getattr(self, name))
        check_listed("values", self.values, _check_values)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "convention": self.convention.value,
                "hardware_id": to_json(self.hardware_id),
                "level": to_json(self.level),
                "level_names": listed_to_json(self.level_names, _names_json),
                "message": to_json(self.message),
                "name": to_json(self.name),
                "stream": self.stream,
                "times": _times_json(self.times),
                "values": listed_to_json(self.values, _values_json),
            },
            self.since,
        )


def _text(obj: Mapping[str, JsonValue], name: str) -> Knowledge[str]:
    return from_json(obj[name], text_decoder(name), provenance_from_json)


def status_report_from_json(data: JsonValue) -> StatusReport:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    keys = {
        "convention",
        "hardware_id",
        "level",
        "level_names",
        "message",
        "name",
        "stream",
        "times",
        "values",
    }
    obj, record_id, provenance = evidence_record_object(
        data, StatusReport.kind, keys, StatusReport.since
    )
    return StatusReport(
        id=record_id,
        provenance=provenance,
        stream=parse_record_id(json_str(obj["stream"], "stream")),
        convention=enum_decoder(StatusConvention)(obj["convention"]),
        times=_times_from_json(obj["times"]),
        level=from_json(obj["level"], _level_from_json, provenance_from_json),
        level_names=listed_from_json(
            obj["level_names"], _names_from_json, provenance_from_json, "level_names"
        ),
        name=_text(obj, "name"),
        message=_text(obj, "message"),
        hardware_id=_text(obj, "hardware_id"),
        values=listed_from_json(obj["values"], _values_from_json, provenance_from_json, "values"),
    )


# A safety state's value as stated: a boolean field's, or an integer field's.
SafetyValue = bool | int


def _safety_value_from_json(data: JsonValue) -> SafetyValue:
    if isinstance(data, bool) or is_int(data):
        return data
    raise ValueError(f"a safety state's value is a boolean or an integer, got {data!r}")


@dataclass(frozen=True)
class SafetyState:
    """One sample of a field its declared type defines as a stop or safety state (ADR 0071 §2).

    - ``declared_type``: the message type that defines the field, as the stream's definition
      names it (``industrial_msgs/RobotStatus``, a ULog format's ``actuator_armed``).
    - ``field``: the field's path in that type, verbatim (``e_stopped.val``, ``manual_lockdown``).
    - ``condition``: what the type defines the field to report.
    - ``value``: the value on the wire, a boolean or an integer as the field is declared.
    - ``value_names``: the names the definition's constants give that value, sorted (``TRUE``,
      ``ON``, ... where several share it); ``Known(())`` where it names other values, ``Unknown``
      where it declares none, ``NotApplicable`` for a boolean.
    """

    kind: ClassVar[str] = "safety_state"
    family: ClassVar[Family] = Family.RUN
    since: ClassVar[int] = STATUS_SINCE
    id: RecordId
    provenance: Provenance
    stream: RecordId
    declared_type: str
    field: str
    condition: SafetyCondition
    times: tuple[Knowledge[Timestamp], ...]
    value: Knowledge[SafetyValue]
    value_names: Listed[str]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.stream)
        for name in ("declared_type", "field"):
            text = getattr(self, name)
            if not isinstance(text, str):
                raise TypeError(f"{name} must be a str, got {type(text).__name__}")
            check_text(name, text)
        if not isinstance(self.condition, SafetyCondition):
            raise TypeError(f"condition must be a SafetyCondition, got {self.condition!r}")
        _check_times(self.times)
        check_type("value", self.value, bool | int)
        check_listed("value_names", self.value_names, _check_names)
        if any(isinstance(value, bool) for value in values_of(self.value)) and not isinstance(
            self.value_names, NotApplicable
        ):
            raise ValueError("a boolean has no named values: value_names is NotApplicable")

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "condition": self.condition.value,
                "declared_type": self.declared_type,
                "field": self.field,
                "stream": self.stream,
                "times": _times_json(self.times),
                "value": to_json(self.value),
                "value_names": listed_to_json(self.value_names, _names_json),
            },
            self.since,
        )


def safety_state_from_json(data: JsonValue) -> SafetyState:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    keys = {"condition", "declared_type", "field", "stream", "times", "value", "value_names"}
    obj, record_id, provenance = evidence_record_object(
        data, SafetyState.kind, keys, SafetyState.since
    )
    decode: Callable[[JsonValue], SafetyValue] = _safety_value_from_json
    return SafetyState(
        id=record_id,
        provenance=provenance,
        stream=parse_record_id(json_str(obj["stream"], "stream")),
        declared_type=json_str(obj["declared_type"], "declared_type"),
        field=json_str(obj["field"], "field"),
        condition=enum_decoder(SafetyCondition)(obj["condition"]),
        times=_times_from_json(obj["times"]),
        value=from_json(obj["value"], decode, provenance_from_json),
        value_names=listed_from_json(
            obj["value_names"], _names_from_json, provenance_from_json, "value_names"
        ),
    )
