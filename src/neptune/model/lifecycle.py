"""Deployment lifecycle records: what commissioning, authorisation, intervention, maintenance,
requalification, incident, change and risk records state, as they state it (ADR 0051).

A deployment is certified, not a robot (ISO 10218:2025, ISO 3691-4, ANSI/RIA R15.08), and its
record layer is a lifecycle: commission, authorise, operate and intervene, maintain, requalify,
learn from incidents and changes. Those records arrive as forms, CMMS rows, tickets and PDFs. Each
kind here is an evidence record of the ``world`` family, ``stated`` by one declaration (a form, a
ticket, a register row), and every value is stored as declared: a severity ``S2`` stays the text
``S2``, a risk score stays its declared text, a speed limit keeps its declared number and unit.
Nothing here orders the lifecycle, checks one record against another, or infers anything.

Every kind shares the fields that place a record in its deployment, as declared ids (ADR 0019 §2):

- ``identifiers``: the ids the declaration gives the record itself (a form number, a ticket key).
- ``site``: the declared id of the site it applies to.
- ``machines``: the declared ids of the machines it covers or involves.
- ``configuration``: the declared id of the configuration it is bound to (a commissioning
  baseline's configuration, a maintenance event's resulting as-maintained configuration).
- ``related``: the other records and evidence it names by id (an incident a requalification
  answers, a video an incident links, a change a rollback undoes).

Linking a declared id to the record it names is identity resolution's (MVL-35), never this
module's. A list of stated values is structural: an empty list means the declaration states none.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Final, Generic, TypeAlias, TypeVar

from neptune.model._fields import (
    Identifiers,
    check_identifiers,
    check_text_values,
    check_type,
    exact_object,
    identifiers_from_json,
    identifiers_to_json,
    json_array,
    json_str,
    text_decoder,
    unit_json,
)
from neptune.model.ids import LogicalId, RecordId, check_text, logical_id_from_json
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
    from_json,
    to_json,
)
from neptune.model.provenance import (
    Provenance,
    check_evidence_record,
    evidence_record_json,
    evidence_record_object,
    provenance_from_json,
)
from neptune.model.record import Family
from neptune.model.scalars import NonFinite, Real, real_from_json, real_to_json
from neptune.model.time import Timestamp, timestamp_from_json
from neptune.model.units import Unit, unit_from_json
from neptune.model.versions import VersionPrimitive, version_from_json, version_to_json

# The schema version that added these kinds (ADR 0037 §1, ADR 0051).
LIFECYCLE_SINCE: Final = 3

# Stated texts in source order, each ``Known`` or ``Ambiguous`` and non-empty: commands issued,
# corrective actions, mitigations. Order is the declaration's; a text may repeat (two resets).
Statements: TypeAlias = tuple[Knowledge[str], ...]

T = TypeVar("T")


# --- Field codecs ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Codec(Generic[T]):
    """How one field is checked, written and read. Each class lists one per field."""

    check: Callable[[str, T], None]
    encode: Callable[[T], JsonValue]
    decode: Callable[[JsonValue, str], T]


def _check_state(name: str, value: Knowledge[Any]) -> None:
    """A field is a state, never a bare value (non-negotiable 3), so a blank cannot pass as one."""
    if not isinstance(
        value, Known | KnownAbsent | Unknown | NotCovered | NotApplicable | Ambiguous
    ):
        raise TypeError(f"{name} must be a Knowledge state, got {value!r}")


def _knowledge(
    kind: type | Any,
    encode: Callable[[Any], JsonValue] | None,
    decode: Callable[[JsonValue], Any],
) -> _Codec[Knowledge[Any]]:
    def check(name: str, value: Knowledge[Any]) -> None:
        _check_state(name, value)
        check_type(name, value, kind)

    return _Codec(
        check,
        lambda value: to_json(value, encode),
        lambda data, _name: from_json(data, decode, provenance_from_json),
    )


def _text_codec() -> _Codec[Knowledge[str]]:
    def check(name: str, value: Knowledge[str]) -> None:
        _check_state(name, value)
        check_text_values(name, value)

    return _Codec(
        check,
        to_json,
        lambda data, name: from_json(data, text_decoder(name), provenance_from_json),
    )


def _check_statements(name: str, statements: Statements) -> None:
    if not isinstance(statements, tuple):
        raise TypeError(f"{name} must be a tuple, got {type(statements).__name__}")
    for statement in statements:
        if not isinstance(statement, Known | Ambiguous):
            raise ValueError(f"{name} lists what the evidence states, got {statement!r}")
        check_text_values(name, statement)


def _check_label(name: str, label: str) -> None:
    if not isinstance(label, str):
        raise TypeError(f"{name} must be a str, got {type(label).__name__}")
    check_text(name, label)


TEXT: Final = _text_codec()
LABEL: Final[_Codec[str]] = _Codec(_check_label, lambda value: value, json_str)
ID: Final = _knowledge(LogicalId, LogicalId.to_json, logical_id_from_json)
TIME: Final = _knowledge(Timestamp, Timestamp.to_json, timestamp_from_json)
VERSION: Final = _knowledge(VersionPrimitive, version_to_json, version_from_json)
NUMBER: Final = _knowledge(float | NonFinite, real_to_json, real_from_json)
UNIT: Final = _knowledge(Unit, unit_json, unit_from_json)
REFS: Final[_Codec[Identifiers]] = _Codec(
    check_identifiers,
    identifiers_to_json,
    lambda data, _name: identifiers_from_json(data, provenance_from_json),
)
STATEMENTS: Final[_Codec[Statements]] = _Codec(
    _check_statements,
    lambda statements: [to_json(statement) for statement in statements],
    lambda data, name: tuple(
        from_json(item, text_decoder(name), provenance_from_json) for item in json_array(data, name)
    ),
)


class _Declared:
    """Fields checked, written and read by the codecs in ``_CODECS``, one per dataclass field
    (but a record's ``id`` and ``provenance``, which the envelope handles)."""

    _CODECS: ClassVar[Mapping[str, _Codec[Any]]]

    def _check_fields(self) -> None:
        for name, codec in self._CODECS.items():
            codec.check(name, getattr(self, name))

    def _fields_json(self) -> dict[str, JsonValue]:
        return {name: codec.encode(getattr(self, name)) for name, codec in self._CODECS.items()}

    @classmethod
    def _fields_from_json(cls, obj: Mapping[str, JsonValue]) -> dict[str, Any]:
        return {name: codec.decode(obj[name], name) for name, codec in cls._CODECS.items()}


V = TypeVar("V", bound="_Value")


class _Value(_Declared):
    """A part of a record: no id or provenance of its own; its states cite or inherit."""

    _WHAT: ClassVar[str]

    def __post_init__(self) -> None:
        self._check_fields()

    def to_json(self) -> JsonObject:
        return self._fields_json()

    @classmethod
    def from_json(cls: type[V], data: JsonValue) -> V:
        obj = exact_object(data, cls._WHAT, set(cls._CODECS))
        return cls(**cls._fields_from_json(obj))


def _items(cls: type[V]) -> _Codec[tuple[V, ...]]:
    """A tuple of ``cls`` values in source order."""

    def check(name: str, items: tuple[V, ...]) -> None:
        if not isinstance(items, tuple):
            raise TypeError(f"{name} must be a tuple, got {type(items).__name__}")
        for item in items:
            if not isinstance(item, cls):
                raise TypeError(f"{name} holds {cls.__name__} values, got {item!r}")

    return _Codec(
        check,
        lambda items: [item.to_json() for item in items],
        lambda data, name: tuple(cls.from_json(item) for item in json_array(data, name)),
    )


def _value(cls: type[V]) -> _Codec[V]:
    def check(name: str, value: V) -> None:
        if not isinstance(value, cls):
            raise TypeError(f"{name} must be a {cls.__name__}, got {value!r}")

    return _Codec(check, lambda value: value.to_json(), lambda data, _name: cls.from_json(data))


# --- Parts -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Quantity(_Value):
    """A declared number and its declared unit: ``1.5`` ``m.s^-1``, never converted (ADR 0013)."""

    _WHAT: ClassVar[str] = "quantity"
    value: Knowledge[Real]
    unit: Knowledge[Unit]
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {"unit": UNIT, "value": NUMBER}


@dataclass(frozen=True)
class Decision(_Value):
    """A decision as declared: what was decided (``approved``, ``returned to service with speed
    restriction``), the authority that decided it (a role or a name, as written) and when."""

    _WHAT: ClassVar[str] = "decision"
    decision: Knowledge[str]
    authority: Knowledge[str]
    time: Knowledge[Timestamp]
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        "authority": TEXT,
        "decision": TEXT,
        "time": TIME,
    }


@dataclass(frozen=True)
class InventoryItem(_Value):
    """One line of a declared inventory: a part or a piece of software, its declared ``model``,
    its ids (serials, asset tags) and its version, of the kind the source names (ADR 0014)."""

    _WHAT: ClassVar[str] = "inventory item"
    name: Knowledge[str]
    model: Knowledge[str]
    identifiers: Identifiers
    version: Knowledge[VersionPrimitive]
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        "identifiers": REFS,
        "model": TEXT,
        "name": TEXT,
        "version": VERSION,
    }


@dataclass(frozen=True)
class TestResult(_Value):
    """One declared test: its name, its result as written (``PASS``, ``3.2 s``), and when."""

    __test__: ClassVar[bool] = False  # not a pytest class
    _WHAT: ClassVar[str] = "test result"
    name: Knowledge[str]
    result: Knowledge[str]
    performed: Knowledge[Timestamp]
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        "name": TEXT,
        "performed": TIME,
        "result": TEXT,
    }


@dataclass(frozen=True)
class ZoneLimit(_Value):
    """A zone an authorisation names, by its declared id (a spatial record's), and the speed
    limit it declares there."""

    _WHAT: ClassVar[str] = "zone limit"
    zone: Knowledge[LogicalId]
    speed_limit: Quantity
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {"speed_limit": _value(Quantity), "zone": ID}


@dataclass(frozen=True)
class PartReplacement(_Value):
    """A declared part swap: what part, and the ids of the units removed and installed."""

    _WHAT: ClassVar[str] = "part replacement"
    part: Knowledge[str]
    removed: Identifiers
    installed: Identifiers
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        "installed": REFS,
        "part": TEXT,
        "removed": REFS,
    }


@dataclass(frozen=True)
class TimelineEntry(_Value):
    """One entry of a declared timeline: when, and what the record says happened."""

    _WHAT: ClassVar[str] = "timeline entry"
    time: Knowledge[Timestamp]
    text: Knowledge[str]
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {"text": TEXT, "time": TIME}


@dataclass(frozen=True)
class ChangeItem(_Value):
    """One declared change: its ``category`` as written (software, parameter, map, zone), the
    ``target`` it changes, and the values before and after, as text."""

    _WHAT: ClassVar[str] = "change item"
    category: Knowledge[str]
    target: Knowledge[str]
    before: Knowledge[str]
    after: Knowledge[str]
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        "after": TEXT,
        "before": TEXT,
        "category": TEXT,
        "target": TEXT,
    }


@dataclass(frozen=True)
class Score(_Value):
    """One declared score: its ``name`` as the source labels it (a column, ``severity``,
    ``PLr``), and its value as written (``S2``, ``12``, ``d``). Never converted or ranked."""

    _WHAT: ClassVar[str] = "score"
    name: str
    value: Knowledge[str]
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {"name": LABEL, "value": TEXT}


@dataclass(frozen=True)
class Hazard(_Value):
    """One hazard a risk assessment states, its scores in source order and its mitigations."""

    _WHAT: ClassVar[str] = "hazard"
    hazard: Knowledge[str]
    scores: tuple[Score, ...]
    mitigations: Statements
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        "hazard": TEXT,
        "mitigations": STATEMENTS,
        "scores": _items(Score),
    }

    def __post_init__(self) -> None:
        super().__post_init__()
        names = [score.name for score in self.scores]
        if len(set(names)) != len(names):
            raise ValueError(f"a hazard's score names are unique: {names}")


# --- Records -----------------------------------------------------------------------------------

R = TypeVar("R", bound="_Lifecycle")

_COMMON: Final[Mapping[str, _Codec[Any]]] = {
    "configuration": ID,
    "identifiers": REFS,
    "machines": REFS,
    "related": REFS,
    "site": ID,
}


@dataclass(frozen=True)
class _Lifecycle(_Declared):
    """The envelope and shared fields of every lifecycle record (module docstring)."""

    kind: ClassVar[str]
    since: ClassVar[int] = LIFECYCLE_SINCE
    id: RecordId
    provenance: Provenance

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        # A lifecycle record is what a form, ticket or work order states; never an observation.
        if self.provenance.assertion_kind is not AssertionKind.STATED:
            raise ValueError(
                f"a {self.kind} is stated by its declaration, not {self.provenance.assertion_kind}"
            )
        self._check_fields()

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind, self.id, self.provenance, self._fields_json(), self.since
        )

    @classmethod
    def from_json(cls: type[R], data: JsonValue) -> R:
        """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
        obj, record_id, provenance = evidence_record_object(
            data, cls.kind, set(cls._CODECS), cls.since
        )
        return cls(id=record_id, provenance=provenance, **cls._fields_from_json(obj))


@dataclass(frozen=True)
class CommissioningBaseline(_Lifecycle):
    """What a commissioning record states the deployment was when it was commissioned.

    ``commissioned``: when. ``hardware`` and ``software``: the declared inventory, versions as
    declared. ``calibrations``: the calibration records it names. ``tests``: the acceptance tests
    and their results as written. ``constraints``: the residual constraints it states (``no
    operation above 35 °C``). ``sign_off``: who accepted it, and when. ``configuration`` is the
    configuration the baseline is bound to.
    """

    kind: ClassVar[str] = "commissioning_baseline"
    family: ClassVar[Family] = Family.WORLD
    identifiers: Identifiers
    site: Knowledge[LogicalId]
    machines: Identifiers
    configuration: Knowledge[LogicalId]
    related: Identifiers
    commissioned: Knowledge[Timestamp]
    hardware: tuple[InventoryItem, ...]
    software: tuple[InventoryItem, ...]
    calibrations: Identifiers
    tests: tuple[TestResult, ...]
    constraints: Statements
    sign_off: Decision
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        **_COMMON,
        "calibrations": REFS,
        "commissioned": TIME,
        "constraints": STATEMENTS,
        "hardware": _items(InventoryItem),
        "sign_off": _value(Decision),
        "software": _items(InventoryItem),
        "tests": _items(TestResult),
    }


@dataclass(frozen=True)
class AuthorisationEnvelope(_Lifecycle):
    """What an authorisation permits, as declared: the operating envelope a deployment is
    approved for.

    ``missions``: the permitted mission classes. ``payload_min`` / ``payload_max``: the payload
    range. ``zones``: the zones it names (declared ids of spatial records) with their speed
    limits. ``supervision``: the supervision mode as written (``remote, 1 operator : 5 robots``).
    ``dependencies``: the infrastructure it depends on (``Wi-Fi AP-3``, ``door interlock D2``).
    ``valid_from`` / ``valid_until``: its validity window. ``approval``: who granted it, when.
    """

    kind: ClassVar[str] = "authorisation_envelope"
    family: ClassVar[Family] = Family.WORLD
    identifiers: Identifiers
    site: Knowledge[LogicalId]
    machines: Identifiers
    configuration: Knowledge[LogicalId]
    related: Identifiers
    missions: Statements
    payload_min: Quantity
    payload_max: Quantity
    zones: tuple[ZoneLimit, ...]
    supervision: Knowledge[str]
    dependencies: Statements
    valid_from: Knowledge[Timestamp]
    valid_until: Knowledge[Timestamp]
    approval: Decision
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        **_COMMON,
        "approval": _value(Decision),
        "dependencies": STATEMENTS,
        "missions": STATEMENTS,
        "payload_max": _value(Quantity),
        "payload_min": _value(Quantity),
        "supervision": TEXT,
        "valid_from": TIME,
        "valid_until": TIME,
        "zones": _items(ZoneLimit),
    }


@dataclass(frozen=True)
class Intervention(_Lifecycle):
    """A human intervention as declared: a remote assist or an on-site action.

    ``mode``: as written (``remote assist``, ``on-site``). ``authority``: the authority level it
    states. ``reason``: why, as stated. ``commands``: the commands issued, in order. ``start`` /
    ``end``: on the clock the record names (the timestamps' domain). ``outcome``: as stated.
    """

    kind: ClassVar[str] = "intervention"
    family: ClassVar[Family] = Family.WORLD
    identifiers: Identifiers
    site: Knowledge[LogicalId]
    machines: Identifiers
    configuration: Knowledge[LogicalId]
    related: Identifiers
    mode: Knowledge[str]
    authority: Knowledge[str]
    reason: Knowledge[str]
    commands: Statements
    start: Knowledge[Timestamp]
    end: Knowledge[Timestamp]
    outcome: Knowledge[str]
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        **_COMMON,
        "authority": TEXT,
        "commands": STATEMENTS,
        "end": TIME,
        "mode": TEXT,
        "outcome": TEXT,
        "reason": TEXT,
        "start": TIME,
    }


@dataclass(frozen=True)
class MaintenanceEvent(_Lifecycle):
    """A maintenance event as declared: ``performed`` when, the ``diagnosis`` that led to it,
    the ``actions`` taken and the ``parts`` replaced, by serial. ``configuration`` is the
    as-maintained configuration it states resulted."""

    kind: ClassVar[str] = "maintenance_event"
    family: ClassVar[Family] = Family.WORLD
    identifiers: Identifiers
    site: Knowledge[LogicalId]
    machines: Identifiers
    configuration: Knowledge[LogicalId]
    related: Identifiers
    performed: Knowledge[Timestamp]
    diagnosis: Knowledge[str]
    actions: Statements
    parts: tuple[PartReplacement, ...]
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        **_COMMON,
        "actions": STATEMENTS,
        "diagnosis": TEXT,
        "parts": _items(PartReplacement),
        "performed": TIME,
    }


@dataclass(frozen=True)
class RequalificationRecord(_Lifecycle):
    """A requalification as declared: its ``cause``, the ``corrective_actions``, the regression
    ``tests`` and their results, the overall ``result`` as written, and the
    ``return_to_service`` decision. ``related`` names what caused it (an incident, a change)."""

    kind: ClassVar[str] = "requalification_record"
    family: ClassVar[Family] = Family.WORLD
    identifiers: Identifiers
    site: Knowledge[LogicalId]
    machines: Identifiers
    configuration: Knowledge[LogicalId]
    related: Identifiers
    performed: Knowledge[Timestamp]
    cause: Knowledge[str]
    corrective_actions: Statements
    tests: tuple[TestResult, ...]
    result: Knowledge[str]
    return_to_service: Decision
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        **_COMMON,
        "cause": TEXT,
        "corrective_actions": STATEMENTS,
        "performed": TIME,
        "result": TEXT,
        "return_to_service": _value(Decision),
        "tests": _items(TestResult),
    }


@dataclass(frozen=True)
class IncidentRecord(_Lifecycle):
    """An incident as declared.

    ``occurred``: when. ``severity``: the class as written (``S2``, ``near miss``), never ranked.
    ``zone``: the declared id of the zone; ``location``: the place as written (``aisle 14``).
    ``machines`` and ``assets``: what was involved, by declared id. ``timeline``: its entries in
    source order. ``description`` and ``root_cause``: as stated. ``related``: the evidence and
    records it links (a video, a log, a ticket).
    """

    kind: ClassVar[str] = "incident_record"
    family: ClassVar[Family] = Family.WORLD
    identifiers: Identifiers
    site: Knowledge[LogicalId]
    machines: Identifiers
    configuration: Knowledge[LogicalId]
    related: Identifiers
    occurred: Knowledge[Timestamp]
    severity: Knowledge[str]
    zone: Knowledge[LogicalId]
    location: Knowledge[str]
    assets: Identifiers
    timeline: tuple[TimelineEntry, ...]
    description: Knowledge[str]
    root_cause: Knowledge[str]
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        **_COMMON,
        "assets": REFS,
        "description": TEXT,
        "location": TEXT,
        "occurred": TIME,
        "root_cause": TEXT,
        "severity": TEXT,
        "timeline": _items(TimelineEntry),
        "zone": ID,
    }


@dataclass(frozen=True)
class ChangeRecord(_Lifecycle):
    """A change as declared: what changed (``changes``), its ``approval``, when it took effect
    (``effective``), and the declared ``rollback`` reference (a release, a map revision)."""

    kind: ClassVar[str] = "change_record"
    family: ClassVar[Family] = Family.WORLD
    identifiers: Identifiers
    site: Knowledge[LogicalId]
    machines: Identifiers
    configuration: Knowledge[LogicalId]
    related: Identifiers
    changes: tuple[ChangeItem, ...]
    approval: Decision
    effective: Knowledge[Timestamp]
    rollback: Knowledge[LogicalId]
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        **_COMMON,
        "approval": _value(Decision),
        "changes": _items(ChangeItem),
        "effective": TIME,
        "rollback": ID,
    }


@dataclass(frozen=True)
class RiskAssessment(_Lifecycle):
    """A risk assessment as declared: ``assessed`` when, by the ``method`` it names (``ISO 12100``,
    a site's own matrix), its ``hazards`` with their scores and mitigations, and its
    ``approval``. Its scope is the shared ``site``, ``machines`` and ``configuration``."""

    kind: ClassVar[str] = "risk_assessment"
    family: ClassVar[Family] = Family.WORLD
    identifiers: Identifiers
    site: Knowledge[LogicalId]
    machines: Identifiers
    configuration: Knowledge[LogicalId]
    related: Identifiers
    assessed: Knowledge[Timestamp]
    method: Knowledge[str]
    hazards: tuple[Hazard, ...]
    approval: Decision
    _CODECS: ClassVar[Mapping[str, _Codec[Any]]] = {
        **_COMMON,
        "approval": _value(Decision),
        "assessed": TIME,
        "hazards": _items(Hazard),
        "method": TEXT,
    }


LIFECYCLE_KINDS: Final[tuple[type[_Lifecycle], ...]] = (
    CommissioningBaseline,
    AuthorisationEnvelope,
    Intervention,
    MaintenanceEvent,
    RequalificationRecord,
    IncidentRecord,
    ChangeRecord,
    RiskAssessment,
)
_PARTS: Final[tuple[type[_Value], ...]] = (
    Quantity,
    Decision,
    InventoryItem,
    TestResult,
    ZoneLimit,
    PartReplacement,
    TimelineEntry,
    ChangeItem,
    Score,
    Hazard,
)

# Every field has exactly one codec, so what is checked, written and read is what is declared.
for _cls in (*LIFECYCLE_KINDS, *_PARTS):
    _declared = {f.name for f in fields(_cls)} - {"id", "provenance"}  # type: ignore[arg-type]
    if _declared != set(_cls._CODECS):
        raise TypeError(f"{_cls.__name__}: fields {sorted(_declared)} != codecs")


def commissioning_baseline_from_json(data: JsonValue) -> CommissioningBaseline:
    return CommissioningBaseline.from_json(data)


def authorisation_envelope_from_json(data: JsonValue) -> AuthorisationEnvelope:
    return AuthorisationEnvelope.from_json(data)


def intervention_from_json(data: JsonValue) -> Intervention:
    return Intervention.from_json(data)


def maintenance_event_from_json(data: JsonValue) -> MaintenanceEvent:
    return MaintenanceEvent.from_json(data)


def requalification_record_from_json(data: JsonValue) -> RequalificationRecord:
    return RequalificationRecord.from_json(data)


def incident_record_from_json(data: JsonValue) -> IncidentRecord:
    return IncidentRecord.from_json(data)


def change_record_from_json(data: JsonValue) -> ChangeRecord:
    return ChangeRecord.from_json(data)


def risk_assessment_from_json(data: JsonValue) -> RiskAssessment:
    return RiskAssessment.from_json(data)
