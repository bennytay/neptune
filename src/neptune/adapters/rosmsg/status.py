"""Status and safety-state records from ROS streams whose declared type says what they are
(ADR 0071). MCAP, rosbag1 and rosbag2 share this, as they share decoding (ADR 0068 §1).

Three steps, kept apart:

- **Recognising** (``recognise``), once per stream, from its parsed definition: a
  ``diagnostic_msgs/DiagnosticArray`` or ``DiagnosticStatus``, or a type of ``SAFETY_TYPES`` whose
  field defines a stop or safety state. The declared type name selects; the definition must then
  have the shape that type defines (field names, wire types, arrays), or the stream is
  ``Unrecognised`` and gives no records. A topic's name is never read: ``/estop`` of
  ``std_msgs/Bool`` says nothing a declared type does, and a guess from it would be derived.
- **Reading** (``read_statuses``, ``read_safety``): one payload read whole by the definition
  (``codec.decode_value``), bounded as the column decoder is; the values as the wire holds them,
  each with its bytes' span in the payload.
- **Writing records** (``StatusWriter``): what was read, as ``StatusReport`` and ``SafetyState``
  records citing the message's bytes, or a status's or a field's exact bytes inside it, with the
  sample's time on every clock of its stream. Statuses at the level their definition names
  ``OK``, and safety states at the value their definition names normal, stay rows only unless the
  config asks for them (``nominal_status_records``), and one call writes at most
  ``max_status_records``: past it a ``status_not_recorded`` finding counts what was left out.

``SAFETY_TYPES`` is part of the adapters' transform: a change to it is a new adapter version.
"""

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.contract import AdapterConfig, ConfigOption
from neptune.adapters.rosmsg.codec import (
    DecodeLimits,
    Malformed,
    Struct,
    decode_value,
    header_field,
)
from neptune.adapters.rosmsg.definitions import (
    ArrayKind,
    ConstantDef,
    Definition,
    FieldDef,
    MessageDef,
)
from neptune.adapters.rosmsg.streams import Report
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import ContentId, RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import (
    AssertionKind,
    Knowledge,
    Known,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import ByteRange, EvidenceRef, Locator, Provenance, TransformRecord
from neptune.model.status import (
    SafetyCondition,
    SafetyState,
    StatusConvention,
    StatusReport,
    StatusValue,
)
from neptune.model.time import Timestamp

DIAGNOSTIC_ARRAY: Final = "diagnostic_msgs/DiagnosticArray"
DIAGNOSTIC_STATUS: Final = "diagnostic_msgs/DiagnosticStatus"
KEY_VALUE: Final = "diagnostic_msgs/KeyValue"
OK: Final = "OK"  # the constant DiagnosticStatus declares for a status with nothing to report
DEFAULT_MAX_STATUS_RECORDS: Final = 8192
# What the records one call writes may weigh at most, as the sandbox's reply encodes them (ASCII
# JSON): a quarter of its reply limit (64 MiB), so status text never takes a chunk's rows with it.
# A safety bound tied to the reply limit, not a knob: it is not part of the config.
MAX_STATUS_RECORD_BYTES: Final = 16 << 20

STATUS_OPTIONS: Final = (
    ConfigOption(
        "max_status_records",
        DEFAULT_MAX_STATUS_RECORDS,
        "status and safety-state records one call writes at most; past it a"
        " status_not_recorded finding counts the rest, which stay rows",
    ),
    ConfigOption(
        "nominal_status_records",
        False,
        "also write a record for a status at the level its definition names OK and a safety"
        " state at the value its definition names normal; off, those stay rows only",
    ),
)

# Finding codes (without the adapter's prefix) and the convention every ROS stream adapter
# documents for what this module writes.
STATUS_FINDINGS: Final = (
    (
        "status_definition_unrecognised",
        "a stream's declared type is a diagnostic status or a safety-state type, but its"
        " definition does not have that type's fields; its messages are rows only"
        " (unsupported, info)",
    ),
    (
        "status_not_recorded",
        "status or safety-state items of one stream that one call does not write as records,"
        " by reason: past max_status_records or the records' byte budget (record_limit,"
        " byte_limit; each later message counted whole and not read, past_limit), a payload"
        " past a decoding limit or one that does not read whole by its definition (limit or"
        " corrupt, warning)",
    ),
)
STATUS_CONVENTION: Final = (
    "status",
    "a message of a declared diagnostic_msgs/DiagnosticArray or DiagnosticStatus gives a"
    " status_report per status, and one of a type in the safety catalogue"
    " (industrial_msgs/RobotStatus, ur_dashboard_msgs/SafetyMode,"
    " autoware_auto_system_msgs/EmergencyState, husky_msgs/HuskyStatus) a safety_state per"
    " listed field, when the definition has the type's fields; never by topic name. A status at"
    " its definition's OK level, or a state at its declared normal value, is a row only unless"
    " nominal_status_records; a record cites its row's message, and an item inside it (a"
    " status of an array, a field) one more byte_range inside the bytes the row cites; times"
    " are the row's, on every clock of the stream",
)


@dataclass(frozen=True)
class SafetyField:
    """A field a declared type defines as a stop or safety state: its path from the root, what it
    reports, the wire types it may be declared as, and its normal value: the name of the constant
    the definition must declare for it, or a boolean's value."""

    path: tuple[str, ...]
    condition: SafetyCondition
    wires: frozenset[str]
    normal: str | bool


def _field(path: str, condition: SafetyCondition, wire: str, normal: str | bool) -> SafetyField:
    return SafetyField(tuple(path.split(".")), condition, frozenset({wire}), normal)


# The declared types whose definitions define a stop or safety state, across embodiments: an
# industrial arm controller (ROS-Industrial), a UR arm's safety controller, an autonomous
# vehicle's emergency handler (Autoware), a mobile base's e-stop (Husky). A type is matched by its
# declared name, then its definition must hold the field as listed.
SAFETY_TYPES: Final[Mapping[str, tuple[SafetyField, ...]]] = {
    "industrial_msgs/RobotStatus": (
        _field("e_stopped.val", SafetyCondition.EMERGENCY_STOP, "int8", "FALSE"),
        _field("in_error.val", SafetyCondition.FAULT, "int8", "FALSE"),
    ),
    "ur_dashboard_msgs/SafetyMode": (
        _field("mode", SafetyCondition.SAFETY_MODE, "uint8", "NORMAL"),
    ),
    "autoware_auto_system_msgs/EmergencyState": (
        _field("state", SafetyCondition.SAFETY_MODE, "uint8", "NORMAL"),
    ),
    "husky_msgs/HuskyStatus": (_field("e_stop", SafetyCondition.EMERGENCY_STOP, "bool", False),),
}

Names = Mapping[int, tuple[str, ...]]


def _names(constants: tuple[ConstantDef, ...]) -> Names | None:
    """A type's integer constants by value, each value's names sorted; ``None`` if it declares
    none (a boolean constant names no integer)."""
    found: dict[int, set[str]] = {}
    for constant in constants:
        if isinstance(constant.value, int) and not isinstance(constant.value, bool):
            found.setdefault(constant.value, set()).add(constant.name)
    if not found:
        return None
    return {value: tuple(sorted(names)) for value, names in found.items()}


def _constant(constants: tuple[ConstantDef, ...], name: str) -> int | None:
    """The integer value of the constant ``name``; ``None`` if it is not declared, or declared
    twice with two values."""
    values = {
        c.value
        for c in constants
        if c.name == name and isinstance(c.value, int) and not isinstance(c.value, bool)
    }
    return values.pop() if len(values) == 1 else None


# --- Recognising ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusType:
    """A stream of diagnostic statuses: an array of them per message, or one."""

    definition: Definition
    array: bool
    names: Names | None  # the status type's level constants; None: it declares none
    ok: int | None  # the value of its OK constant, where it declares one


@dataclass(frozen=True)
class Resolved:
    """A safety field found in a definition: its normal value, where the definition gives it,
    and the names its owning type's constants give its values (``None`` where it declares none
    or the field is a boolean)."""

    spec: SafetyField
    boolean: bool
    normal: int | bool | None
    names: Names | None


@dataclass(frozen=True)
class SafetyType:
    definition: Definition
    type: str
    fields: tuple[Resolved, ...]


@dataclass(frozen=True)
class Unrecognised:
    """A stream whose declared type is a status or safety type, but whose definition does not
    have that type's shape: no records, a finding."""

    type: str
    detail: str


Recognised = StatusType | SafetyType


def _shape(message: MessageDef | None, expected: list[tuple[str, frozenset[str] | str]]) -> bool:
    """``message``'s fields are exactly ``expected``: names in order, each a scalar of one of the
    wire types, or (a type name) one array of that message type."""
    if message is None or len(message.fields) != len(expected):
        return False
    for found, (name, kind) in zip(message.fields, expected, strict=True):
        if found.name != name:
            return False
        if isinstance(kind, frozenset):
            if found.array is not None or found.wire not in kind:
                return False
        elif found.wire is not None or found.type != kind or found.array not in _ARRAYS:
            return False
    return True


_TEXT: Final = frozenset({"string"})
_ARRAYS: Final = (ArrayKind.UNBOUNDED, ArrayKind.BOUNDED)
_STATUS_FIELDS: Final[list[tuple[str, frozenset[str] | str]]] = [
    ("level", frozenset({"int8", "uint8"})),
    ("name", _TEXT),
    ("message", _TEXT),
    ("hardware_id", _TEXT),
    ("values", KEY_VALUE),
]


def recognise(definition: Definition) -> Recognised | Unrecognised | None:
    """What a stream's declared type makes its messages: statuses, safety states, a type of
    those whose definition is not their shape, or (``None``) nothing to record."""
    root, types = definition.root, definition.types
    if root in (DIAGNOSTIC_ARRAY, DIAGNOSTIC_STATUS):
        status = types.get(DIAGNOSTIC_STATUS)
        ok = _shape(status, _STATUS_FIELDS) and _shape(
            types.get(KEY_VALUE), [("key", _TEXT), ("value", _TEXT)]
        )
        if root == DIAGNOSTIC_ARRAY:
            array = types[root]
            ok = (
                ok
                and len(array.fields) == 2
                and header_field(definition) is not None
                and _shape(MessageDef(root, array.fields[1:]), [("status", DIAGNOSTIC_STATUS)])
            )
        if not ok or status is None:
            return Unrecognised(
                root,
                f"{root}'s definition does not have the fields diagnostic_msgs defines (a header"
                " and statuses; level, name, message, hardware_id and key/values)",
            )
        return StatusType(
            definition,
            root == DIAGNOSTIC_ARRAY,
            _names(status.constants),
            _constant(status.constants, OK),
        )
    specs = SAFETY_TYPES.get(root)
    if specs is None:
        return None
    resolved: list[Resolved] = []
    for spec in specs:
        found = _resolve(definition, spec)
        if found is None:
            return Unrecognised(
                root,
                f"{root}'s definition does not declare {'.'.join(spec.path)} as one"
                f" {'/'.join(sorted(spec.wires))}",
            )
        resolved.append(found)
    return SafetyType(definition, root, tuple(resolved))


def _resolve(definition: Definition, spec: SafetyField) -> Resolved | None:
    owner: MessageDef | None = definition.root_type
    target: FieldDef | None = None
    for depth, name in enumerate(spec.path):
        if owner is None:
            return None
        target = next((f for f in owner.fields if f.name == name), None)
        if target is None or target.array is not None:
            return None
        if depth < len(spec.path) - 1:
            if target.wire is not None:
                return None
            owner = definition.types.get(target.type)
    if target is None or owner is None or target.wire not in spec.wires:
        return None
    if isinstance(spec.normal, bool):
        return Resolved(spec, True, spec.normal, None)
    return Resolved(spec, False, _constant(owner.constants, spec.normal), _names(owner.constants))


def unrecognised_report(kind: Unrecognised, what: str, details: Mapping[str, JsonValue]) -> Report:
    """The finding for a stream whose declared status or safety type is not that type's shape."""
    return Report(
        "status_definition_unrecognised",
        FindingCategory.UNSUPPORTED,
        Severity.INFO,
        f"{what} declares {kind.type}, but {kind.detail}; its messages are rows only, no status"
        " or safety-state records",
        {**details, "type": kind.type},
    )


def recognised(decoding: object) -> Recognised | Unrecognised | None:
    """What a stream's planned decoding makes of its messages: ``recognise`` on its parsed
    definition, or ``None`` where its payloads are not decoded."""
    definition = getattr(decoding, "definition", None)
    return recognise(definition) if isinstance(definition, Definition) else None


# --- Reading ------------------------------------------------------------------------------------


# Text as the wire holds it: a ``str``, or ``BAD_TEXT`` where it is not UTF-8.
Text = object


@dataclass(frozen=True)
class StatusItem:
    """One status as the wire holds it; ``span`` its bytes in the payload, ``None`` for a message
    that is one status."""

    span: tuple[int, int] | None
    level: int
    name: Text
    message: Text
    hardware_id: Text
    values: tuple[tuple[Text, Text], ...]


@dataclass(frozen=True)
class SafetyItem:
    field: Resolved
    span: tuple[int, int]
    value: int | bool


def _status(value: Struct, span: tuple[int, int] | None) -> StatusItem:
    pairs = value.fields["values"]
    assert isinstance(pairs, tuple)
    level = value.fields["level"]
    assert isinstance(level, int)
    return StatusItem(
        span,
        level,
        value.fields["name"],
        value.fields["message"],
        value.fields["hardware_id"],
        tuple(
            (pair.fields["key"], pair.fields["value"]) for pair in pairs if isinstance(pair, Struct)
        ),
    )


def read_statuses(
    kind: StatusType, payload: bytes | memoryview, cdr: bool, limits: DecodeLimits
) -> list[StatusItem]:
    """The statuses one payload holds, in order. Raises ``Malformed``."""
    value = decode_value(kind.definition, payload, cdr, limits)
    if not kind.array:
        return [_status(value, None)]
    items = value.fields["status"]
    assert isinstance(items, tuple)
    return [_status(item, (item.start, item.end)) for item in items if isinstance(item, Struct)]


def read_safety(
    kind: SafetyType, payload: bytes | memoryview, cdr: bool, limits: DecodeLimits
) -> list[SafetyItem]:
    """The safety fields one payload holds, in the order ``SAFETY_TYPES`` lists them."""
    value = decode_value(kind.definition, payload, cdr, limits)
    found: list[SafetyItem] = []
    for resolved in kind.fields:
        holder, path = value, resolved.spec.path
        for name in path[:-1]:
            inner = holder.fields[name]
            assert isinstance(inner, Struct)
            holder = inner
        raw = holder.fields[path[-1]]
        assert isinstance(raw, int)  # a bool is an int
        found.append(SafetyItem(resolved, holder.spans[path[-1]], raw))
    return found


# --- Writing records ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """What a stream adapter knows of one message: the source, the locator its row cites (the
    message's bytes), where its payload starts in the bytes the last step cites, its time on
    each of its stream's clocks, in order (states with ``INHERITED`` provenance), and its place
    as the adapter cites it in a finding."""

    source: ContentId
    steps: tuple[Locator, ...]
    payload_at: int
    times: tuple[Knowledge[Timestamp], ...]
    place: object


@dataclass
class _Left:
    """Per stream, what one call did not record, by reason, and the first one's place."""

    counts: Counter[str] = field(default_factory=Counter)
    first: object | None = None


class StatusWriter:
    """Records of one adapter call, bounded by ``max_status_records`` and by ``max_bytes`` of
    their encoded size. Once either is reached, later messages are counted, never decoded."""

    def __init__(self, config: AdapterConfig, max_bytes: int = MAX_STATUS_RECORD_BYTES) -> None:
        self.transform: TransformRecord = config.transform
        self.limit = config.integer("max_status_records")
        self.max_bytes = max_bytes
        self.nominal = config.flag("nominal_status_records")
        self.records: list[StatusReport | SafetyState] = []
        self.bytes = 0
        self.full = False
        self.left: dict[RecordId, _Left] = {}

    def leave(self, stream: RecordId, reason: str, sample: Sample, count: int = 1) -> None:
        """A message (or ``count`` items of one) whose records are not written, and why."""
        left = self.left.setdefault(stream, _Left())
        left.counts[reason] += count
        if left.first is None:
            left.first = sample.place

    def add(
        self,
        kind: Recognised,
        stream: RecordId,
        definition: EvidenceRef | None,
        sample: Sample,
        payload: bytes | memoryview | None,
        cdr: bool,
        limits: DecodeLimits,
    ) -> None:
        """One message's records: ``definition`` cites the stream's definition (its constants);
        ``payload`` is ``None`` where its bytes are not all at the row's place."""
        if self.full or len(self.records) >= self.limit:
            # counted whole, not decoded: nothing past the bound is written
            self.full = True
            self.leave(stream, "past_limit", sample)
            return
        if payload is None:
            self.leave(stream, "not_local", sample)
            return
        try:
            if isinstance(kind, StatusType):
                items: list[StatusItem] | list[SafetyItem] = read_statuses(
                    kind, payload, cdr, limits
                )
            else:
                items = read_safety(kind, payload, cdr, limits)
        except Malformed as problem:
            self.leave(stream, problem.reason, sample)
            return
        stated = (
            Provenance(definition, self.transform.id, AssertionKind.STATED)
            if definition is not None
            else None
        )
        for item in items:
            if isinstance(item, StatusItem):
                assert isinstance(kind, StatusType)
                if not self.nominal and kind.ok is not None and item.level == kind.ok:
                    continue
                placed = self._place(stream, sample, item.span)
                if placed is not None:
                    provenance, times = placed
                    self._keep(
                        _report(self.transform, provenance, stream, times, kind, item, stated),
                        stream,
                        sample,
                    )
            else:
                assert isinstance(kind, SafetyType)
                normal = item.field.normal
                if not self.nominal and normal is not None and item.value == normal:
                    continue
                placed = self._place(stream, sample, item.span)
                if placed is not None:
                    provenance, times = placed
                    self._keep(
                        _safety(self.transform, provenance, stream, times, kind, item, stated),
                        stream,
                        sample,
                    )

    def _keep(self, record: StatusReport | SafetyState, stream: RecordId, sample: Sample) -> None:
        """Write ``record`` if it fits the byte budget; else count it, and write no more."""
        size = len(json.dumps(record.to_json(), separators=(",", ":"), ensure_ascii=True))
        if self.bytes + size > self.max_bytes:
            self.full = True
            self.leave(stream, "byte_limit", sample)
            return
        self.bytes += size
        self.records.append(record)

    def _place(
        self, stream: RecordId, sample: Sample, span: tuple[int, int] | None
    ) -> tuple[Provenance, tuple[Knowledge[Timestamp], ...]] | None:
        """A record's provenance and times, or ``None`` past this call's limit."""
        if len(self.records) >= self.limit:
            self.full = True
            self.leave(stream, "record_limit", sample)
            return None
        if self.full:  # an earlier item of this message did not fit the byte budget
            self.leave(stream, "byte_limit", sample)
            return None
        message = EvidenceRef(sample.source, sample.steps)
        times = sample.times
        evidence = message
        if span is not None:
            start, end = span
            evidence = EvidenceRef(
                sample.source,
                (*sample.steps, ByteRange(sample.payload_at + start, end - start)),
            )
            # the times are the message's, outside the item's own bytes
            cited = Provenance(message, self.transform.id, AssertionKind.OBSERVED)
            times = tuple(_cite(time, cited) for time in times)
        return Provenance(evidence, self.transform.id, AssertionKind.OBSERVED), times

    def reports(self) -> list[tuple[RecordId, object, Report]]:
        """One finding per stream whose messages here left status records unwritten: the
        stream, the first such message's place, and the report."""
        found: list[tuple[RecordId, object, Report]] = []
        for stream, left in sorted(self.left.items()):
            if left.first is None:
                continue
            total = sum(left.counts.values())
            limit = set(left.counts) <= {
                "byte_limit",
                "past_limit",
                "record_limit",
                "array_limit",
                "message_limit",
                "not_local",
                "walk_limit",
            }
            details: dict[str, JsonValue] = {
                "counts": dict(sorted(left.counts.items())),
                "max_status_records": self.limit,
                "max_status_record_bytes": self.max_bytes,
            }
            found.append(
                (
                    stream,
                    left.first,
                    Report(
                        "status_not_recorded",
                        FindingCategory.LIMIT if limit else FindingCategory.CORRUPT,
                        Severity.WARNING,
                        f"{total} status or safety-state message(s) or item(s) of this stream here"
                        " are not records (past this call's max_status_records or record byte"
                        " budget, or a payload"
                        " that does not read whole by its definition); their rows still cite"
                        " each message",
                        details,
                    ),
                )
            )
        return found


def _cite(time: Knowledge[Timestamp], provenance: Provenance) -> Knowledge[Timestamp]:
    match time:
        case Known(value=value):
            return Known(value, provenance)
        case Unknown():
            return Unknown(provenance)
        case NotCovered():
            return NotCovered(provenance)
        case _:
            return time


def _text(value: Text) -> Knowledge[str]:
    return Known(value) if isinstance(value, str) and value else Unknown()


def _report(
    transform: TransformRecord,
    provenance: Provenance,
    stream: RecordId,
    times: tuple[Knowledge[Timestamp], ...],
    kind: StatusType,
    item: StatusItem,
    stated: Provenance | None,
) -> StatusReport:
    names: Knowledge[tuple[str, ...]]
    if stated is None:
        names = Unknown()
    elif kind.names is None:
        names = Unknown(stated)
    else:
        names = Known(kind.names.get(item.level, ()), stated)
    values: Knowledge[tuple[StatusValue, ...]]
    if all(isinstance(k, str) and isinstance(v, str) for k, v in item.values):
        values = Known(tuple(StatusValue(str(k), str(v)) for k, v in item.values))
    else:
        values = Unknown()
    return StatusReport(
        id=evidence_record_id(StatusReport.kind, provenance.evidence, transform),
        provenance=provenance,
        stream=stream,
        convention=StatusConvention.ROS_DIAGNOSTIC_STATUS,
        times=times,
        level=Known(item.level),
        level_names=names,
        name=_text(item.name),
        message=_text(item.message),
        hardware_id=_text(item.hardware_id),
        values=values,
    )


def _safety(
    transform: TransformRecord,
    provenance: Provenance,
    stream: RecordId,
    times: tuple[Knowledge[Timestamp], ...],
    kind: SafetyType,
    item: SafetyItem,
    stated: Provenance | None,
) -> SafetyState:
    resolved = item.field
    names: Knowledge[tuple[str, ...]]
    if resolved.boolean:
        names = NotApplicable()
    elif stated is None:
        names = Unknown()
    elif resolved.names is None:
        names = Unknown(stated)
    else:
        names = Known(resolved.names.get(int(item.value), ()), stated)
    return SafetyState(
        id=evidence_record_id(SafetyState.kind, provenance.evidence, transform),
        provenance=provenance,
        stream=stream,
        declared_type=kind.type,
        field=".".join(resolved.spec.path),
        condition=resolved.spec.condition,
        times=times,
        value=Known(bool(item.value) if resolved.boolean else int(item.value)),
        value_names=names,
    )


__all__ = [
    "SAFETY_TYPES",
    "STATUS_OPTIONS",
    "Recognised",
    "SafetyType",
    "Sample",
    "StatusType",
    "StatusWriter",
    "Unrecognised",
    "read_safety",
    "read_statuses",
    "recognise",
]
