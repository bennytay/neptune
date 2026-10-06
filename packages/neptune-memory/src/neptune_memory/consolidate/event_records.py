"""Parsing stated events: what the episode consolidator (ADR 0012 §1) and the event index
(ADR 0013 §1, §3) read, and the event index's config.

Parsing is kept apart from policy: each parser turns one Ledger record into a typed value or
raises ``Malformed``, and decides nothing. ``consolidate.episodes`` and ``consolidate.events``
apply their policies. One reader per kind serves both: ``incident_record`` and
``intervention_record`` return the compiler's record, and ``incident`` and ``intervention`` its
``Event`` summary for episodes.

Every kind is a compiler kind, read with the compiler's own strict reader:

- ``incident_record`` and ``intervention`` (root ADR 0051): lifecycle records, ``stated`` by a form,
  a CMMS row or a ticket; Deploy maps them from CMMS exports, ticket systems and Formant. An
  intervention involves ``machines``, names ``related`` records and has a ``start`` / ``end`` on the
  clock its timestamps name; an incident states the instant it ``occurred``. No compiler kind states
  a task attempt yet (root ADR 0047 §9).
- ``maintenance_event`` (root ADR 0051; ADR 0025 §1): a work order or maintenance record,
  ``stated``, ``performed`` at an instant on the machines it names, with its ``diagnosis``, its
  ``actions`` in order and the ``parts`` it replaced.
- ``status_report`` (root ADR 0071, package-schema 10; ADR 0025 §2): one status a typed log
  stream's message reports, ``observed``, with its level, message and key/values, at a sample time
  on each of its stream's clocks.
- ``structured_table`` and ``structured_record`` (root ADR 0020 §5): a table and its rows, read as
  events only where the config declares the table an event table (by its declared name). Deploy's
  ROS 2 diagnostics mapper writes such a table (``diagnostic events``, with a ``@clock:stamp``
  companion naming the clock); a PLC, safety-controller or syslog export is another.
- ``timestamp_domain`` (``identity_records.clock``): a clock's resolution (to scale the
  co-occurrence window) and whether it declares itself civil; ``clock_mapping`` (root ADR 0050 §5,
  ``run_records.mapping``): a stated map between two clocks.

No stand-in kind is read. A bag's topics are read only through the ``status_report`` records the
compiler writes for them, and no time written as text is read.

The config (``EventConfig``) declares the co-occurrence window, the vendor mappings from a source's
own kinds to the registered ``EVENT_KINDS``, and the event tables with the columns that hold each
field. ``resolve_config`` fills in defaults; ``parse_config`` refuses what it cannot use part by
part, so one bad table never disables the others.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Final, TypeVar

from neptune.identity import canonical_json
from neptune.model.ids import RecordId, check_token, parse_record_id
from neptune.model.knowledge import Ambiguous, Known
from neptune.model.lifecycle import (
    IncidentRecord,
    Intervention,
    MaintenanceEvent,
    incident_record_from_json,
    intervention_from_json,
    maintenance_event_from_json,
)
from neptune.model.provenance import Provenance
from neptune.model.status import StatusReport, status_report_from_json
from neptune.model.world import (
    StructuredRecord,
    StructuredTable,
    structured_record_from_json,
    structured_table_from_json,
)
from neptune_memory.consolidate.identity_records import Clock, Malformed, clock, declared
from neptune_memory.consolidate.run_records import Inferred, mapping
from neptune_memory.consolidate.run_records import _strict as _strict  # one gate for every kind
from neptune_memory.schema.predicates import EVENT_KINDS

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from neptune.model.ids import LogicalId
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune.model.provenance import EvidenceRef
    from neptune.model.time import Timestamp

_T = TypeVar("_T")

# Ledger record kinds the episode and event consolidators read.
INCIDENT: Final = "incident_record"
INTERVENTION: Final = "intervention"
MAINTENANCE: Final = "maintenance_event"
STATUS_REPORT: Final = "status_report"
STRUCTURED_TABLE: Final = "structured_table"
STRUCTURED_RECORD: Final = "structured_record"
TIMESTAMP_DOMAIN: Final = "timestamp_domain"
CLOCK_MAPPING: Final = "clock_mapping"

# The vendor names of the lifecycle kinds: an incident's mapping is keyed by its stated severity,
# an intervention's by its stated mode; a maintenance event states no kind of its own, so it is
# always ``maintenance``. Unmapped, each is its own registered kind. A status report's vendor is
# its ``convention`` (``ros_diagnostic_status``), keyed by its level's name or its level.
LIFECYCLE_DEFAULT_KIND: Final[Mapping[str, str]] = {
    INCIDENT: "incident",
    INTERVENTION: "intervention",
    MAINTENANCE: "maintenance",
}

# A vendor mapping's target for a declared kind that is not an event (an OK status, an info line).
# Canonical JSON has no null, so "not an event" is this reserved word, never a registered kind.
NOT_AN_EVENT: Final = "not_an_event"

DEFAULT_WINDOW: Final = "5"  # seconds, as decimal text
MAX_WINDOW: Final = 86_400  # seconds: a day; anything wider is not "at the same time"
_DECIMAL: Final = re.compile(r"(0|[1-9][0-9]{0,5})(\.[0-9]{1,9})?")
DEFAULT_MAX_PARTNERS: Final = 64
MAX_PARTNERS_LIMIT: Final = 4096
DEFAULT_CONFIG: Final[Mapping[str, JsonValue]] = {
    "co_occurrence": {"max_partners": DEFAULT_MAX_PARTNERS, "window_seconds": DEFAULT_WINDOW},
    "tables": [],
    "vendors": {},
}

__all__ = [
    "CLOCK_MAPPING",
    "DEFAULT_CONFIG",
    "INCIDENT",
    "INTERVENTION",
    "LIFECYCLE_DEFAULT_KIND",
    "MAINTENANCE",
    "NOT_AN_EVENT",
    "STATUS_REPORT",
    "STRUCTURED_RECORD",
    "STRUCTURED_TABLE",
    "TIMESTAMP_DOMAIN",
    "Clock",
    "ClockSpec",
    "Event",
    "EventConfig",
    "IdColumn",
    "Inferred",
    "Malformed",
    "Named",
    "TableSpec",
    "TimeSpec",
    "clock",
    "incident",
    "incident_record",
    "intervention",
    "intervention_record",
    "maintenance_record",
    "mapping",
    "parse_config",
    "resolve_config",
    "row",
    "status_report",
    "table",
]


def incident_record(record: Mapping[str, object]) -> IncidentRecord:
    return _strict(incident_record_from_json, record)


def intervention_record(record: Mapping[str, object]) -> Intervention:
    return _strict(intervention_from_json, record)


def maintenance_record(record: Mapping[str, object]) -> MaintenanceEvent:
    return _strict(maintenance_event_from_json, record)


def status_report(record: Mapping[str, object]) -> StatusReport:
    return _strict(status_report_from_json, record)


# --- Event summaries for episodes (ADR 0012 §1) -------------------------------------------------


@dataclass(frozen=True)
class Named:
    """One id a list states: ``ids`` holds one id when it is ``Known``, every candidate when it is
    ``Ambiguous``; ``evidence`` is what the item itself cites (nothing when it inherits)."""

    ids: tuple[LogicalId, ...]
    decided: bool
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class Event:
    """A stated event as the episode policy needs it.

    ``start`` and ``end`` are the instants the record states, each on its own clock; ``end`` is
    inclusive and ``None`` when not stated (an incident states one instant: ``start`` only).
    ``evidence`` cites the record and every value read from it that cites its own place.
    """

    record: RecordId
    kind: str
    machines: tuple[Named, ...]
    related: tuple[Named, ...]
    start: Timestamp | None
    end: Timestamp | None
    evidence: tuple[EvidenceRef, ...]


def _cited(knowledge: object) -> tuple[EvidenceRef, ...]:
    """The evidence a value (or a candidate) cites itself; nothing when it inherits."""
    slot = getattr(knowledge, "provenance", None)
    return (slot.evidence,) if isinstance(slot, Provenance) else ()


def _named(listed: Knowledge[tuple[Knowledge[LogicalId], ...]]) -> tuple[Named, ...]:
    """The ids a ``Listed`` field states; a list not stated (``Unknown``, ``NotCovered``) names
    none. Every id is a declared value (ADR 0006 §9): never blank or padded."""
    if not isinstance(listed, Known):
        return ()
    out: list[Named] = []
    for item in listed.value:
        if isinstance(item, Known):
            out.append(Named((declared(item.value),), True, _cited(item)))
        elif isinstance(item, Ambiguous):
            ids = tuple(declared(c.value) for c in item.candidates)
            cited = tuple(ref for c in item.candidates for ref in _cited(c))
            out.append(Named(ids, False, (*_cited(item), *cited)))
    return tuple(out)


def _instant(knowledge: Knowledge[Timestamp]) -> Timestamp | None:
    """A stated instant; an ``Ambiguous`` or unstated one is not read as any of its readings."""
    return knowledge.value if isinstance(knowledge, Known) else None


def _event(
    record: Intervention | IncidentRecord,
    start: Knowledge[Timestamp],
    end: Knowledge[Timestamp] | None,
) -> Event:
    stated = [start, *([end] if end is not None else [])]
    return Event(
        record=record.id,
        kind=record.kind,
        machines=_named(record.machines),
        related=_named(record.related),
        start=_instant(start),
        end=_instant(end) if end is not None else None,
        evidence=(
            record.provenance.evidence,
            *(ref for k in stated if isinstance(k, Known) for ref in _cited(k)),
        ),
    )


def intervention(record: Mapping[str, object]) -> Event:
    parsed = intervention_record(record)
    return _event(parsed, parsed.start, parsed.end)


def incident(record: Mapping[str, object]) -> Event:
    parsed = incident_record(record)
    return _event(parsed, parsed.occurred, None)


# --- Event tables -------------------------------------------------------------------------------


def table(record: Mapping[str, object]) -> StructuredTable:
    return _strict(structured_table_from_json, record)


def row(record: Mapping[str, object]) -> StructuredRecord:
    return _strict(structured_record_from_json, record)


# --- The config ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class IdColumn:
    """A column holding a declared id's value, and the namespace the config declares for it."""

    column: str
    namespace: str


@dataclass(frozen=True)
class TimeSpec:
    """Where an instant is: integer ``ticks`` of its clock, or integer ``seconds`` and
    ``nanoseconds`` (a ROS ``stamp``), which need the clock's stated resolution to become ticks."""

    ticks: str | None = None
    seconds: str | None = None
    nanoseconds: str | None = None

    def columns(self) -> tuple[str, ...]:
        return tuple(c for c in (self.ticks, self.seconds, self.nanoseconds) if c is not None)


@dataclass(frozen=True)
class ClockSpec:
    """Which clock a row's instants are on: a companion column whose cell is a
    ``timestamp_domain`` record id, or one record id the config declares for the whole table."""

    column: str | None = None
    record: RecordId | None = None


@dataclass(frozen=True)
class TableSpec:
    """One declared event table: which columns hold which field (by header name)."""

    name: str
    vendor: str
    kind: str
    at: TimeSpec
    clock: ClockSpec
    end: TimeSpec | None = None
    machine: IdColumn | None = None
    site: IdColumn | None = None
    zone: IdColumn | None = None
    severity: str | None = None
    description: str | None = None

    def columns(self) -> tuple[str, ...]:
        """Every column the spec names, so a table missing one is refused, not half-read."""
        named: list[str] = [self.kind, *self.at.columns()]
        if self.clock.column is not None:
            named.append(self.clock.column)
        if self.end is not None:
            named.extend(self.end.columns())
        for ids in (self.machine, self.site, self.zone):
            if ids is not None:
                named.append(ids.column)
        named.extend(c for c in (self.severity, self.description) if c is not None)
        return tuple(dict.fromkeys(named))


@dataclass(frozen=True)
class EventConfig:
    """The resolved config: ``window`` in seconds (``None`` when it is unusable, so nothing
    co-occurs), the vendor mappings (``None`` where the config declares ``NOT_AN_EVENT``) and the
    tables."""

    window: Fraction | None
    max_partners: int
    vendors: Mapping[str, Mapping[Key, str | None]]
    tables: tuple[TableSpec, ...]
    problems: tuple[str, ...] = field(default=())


def resolve_config(config: Mapping[str, JsonValue] | None = None) -> dict[str, JsonValue]:
    """``config`` with the defaults filled in, so an explicit default and an omitted one hash the
    same (the ``Consolidator`` contract). Keys it does not know are kept, and refused by
    ``parse_config``."""
    given = dict(config or {})
    out: dict[str, JsonValue] = {**DEFAULT_CONFIG, **given}
    co = given.get("co_occurrence")
    if isinstance(co, dict):
        merged: dict[str, JsonValue] = {**DEFAULT_CONFIG["co_occurrence"], **co}  # type: ignore[dict-item]
        # One spelling per window: 5, "5" and "5.0" are one config. A window it cannot read is
        # kept as given, for parse_config to refuse.
        with contextlib.suppress(_Bad):
            merged["window_seconds"] = _decimal_text(_window(merged["window_seconds"]))
        out["co_occurrence"] = merged
    return canonical_json.loads(canonical_json.dumps(out))  # type: ignore[return-value]


def _decimal_text(seconds: Fraction) -> str:
    """A window's canonical decimal text: no exponent, no trailing zeros."""
    text = format(Decimal(seconds.numerator) / Decimal(seconds.denominator), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


_TOP: Final = frozenset({"co_occurrence", "tables", "vendors"})
_CO: Final = frozenset({"max_partners", "window_seconds"})
_TABLE: Final = frozenset(
    {
        "at",
        "clock",
        "description",
        "end",
        "kind",
        "machine",
        "name",
        "severity",
        "site",
        "vendor",
        "zone",
    }
)
_TABLE_REQUIRED: Final = frozenset({"at", "clock", "kind", "name", "vendor"})


class _Bad(ValueError):
    """One part of the config cannot be used."""


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _Bad(f"{where} must be non-empty text with no surrounding whitespace")
    return value


def _keys(value: object, allowed: frozenset[str], where: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise _Bad(f"{where} must be an object")
    extra = sorted(set(value) - allowed)
    if extra:
        raise _Bad(f"{where} has unexpected keys {extra}")
    return value


def _window(value: object) -> Fraction:
    """Seconds in ``(0, MAX_WINDOW]``: an integer, or decimal text with at most 9 decimals."""
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise _Bad("co_occurrence.window_seconds must be a decimal text or an integer")
    if isinstance(value, str) and not _DECIMAL.fullmatch(value):
        raise _Bad("co_occurrence.window_seconds is not a plain decimal number of seconds")
    seconds = Fraction(Decimal(value)) if isinstance(value, str) else Fraction(value)
    if not 0 < seconds <= MAX_WINDOW:
        raise _Bad(f"co_occurrence.window_seconds must be positive and at most {MAX_WINDOW}")
    return seconds


def _time(value: object, where: str) -> TimeSpec:
    spec = _keys(value, frozenset({"ticks", "seconds", "nanoseconds"}), where)
    if set(spec) == {"ticks"}:
        return TimeSpec(ticks=_text(spec["ticks"], f"{where}.ticks"))
    if set(spec) == {"seconds", "nanoseconds"}:
        return TimeSpec(
            seconds=_text(spec["seconds"], f"{where}.seconds"),
            nanoseconds=_text(spec["nanoseconds"], f"{where}.nanoseconds"),
        )
    raise _Bad(f"{where} is {{ticks}} or {{seconds, nanoseconds}}")


def _clock(value: object) -> ClockSpec:
    spec = _keys(value, frozenset({"column", "record"}), "clock")
    if set(spec) == {"column"}:
        return ClockSpec(column=_text(spec["column"], "clock.column"))
    if set(spec) == {"record"}:
        try:
            return ClockSpec(record=parse_record_id(spec["record"]))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise _Bad("clock.record is not a record id") from None
    raise _Bad("clock is {column} or {record}")


def _ids(value: object, where: str) -> IdColumn:
    spec = _keys(value, frozenset({"column", "namespace"}), where)
    if set(spec) != {"column", "namespace"}:
        raise _Bad(f"{where} is {{column, namespace}}")
    namespace = _text(spec["namespace"], f"{where}.namespace")
    try:
        check_token(f"{where}.namespace", namespace)
    except ValueError as exc:
        raise _Bad(str(exc)) from None
    if namespace == "record":
        raise _Bad(f"{where}.namespace 'record' is reserved for record-keyed nodes")
    return IdColumn(_text(spec["column"], f"{where}.column"), namespace)


def _table(value: object, index: int, vendors: Mapping[str, object]) -> TableSpec:
    where = f"tables[{index}]"
    spec = _keys(value, _TABLE, where)
    missing = sorted(_TABLE_REQUIRED - set(spec))
    if missing:
        raise _Bad(f"{where} is missing {missing}")
    vendor = _text(spec["vendor"], f"{where}.vendor")
    if vendor in LIFECYCLE_DEFAULT_KIND:
        raise _Bad(f"{where}.vendor {vendor!r} is a lifecycle record's mapping")
    if vendor not in vendors:
        raise _Bad(f"{where}.vendor {vendor!r} declares no mapping under 'vendors'")

    def optional(key: str, read: Callable[[object, str], _T]) -> _T | None:
        return read(spec[key], f"{where}.{key}") if key in spec else None

    return TableSpec(
        name=_text(spec["name"], f"{where}.name"),
        vendor=vendor,
        kind=_text(spec["kind"], f"{where}.kind"),
        at=_time(spec["at"], f"{where}.at"),
        clock=_clock(spec["clock"]),
        end=optional("end", _time),
        machine=optional("machine", _ids),
        site=optional("site", _ids),
        zone=optional("zone", _ids),
        severity=optional("severity", _text),
        description=optional("description", _text),
    )


# A declared kind is keyed by its type and its value, so the integer level 2 and the text "2" are
# different kinds and neither is coerced into the other.
TEXT: Final = "text"
INTEGER: Final = "integer"
_INTEGER: Final = re.compile(r"-?(0|[1-9][0-9]*)")
Key = tuple[str, str]  # (TEXT or INTEGER, the declared value as written in the config)


def _vendor(name: str, value: object) -> dict[Key, str | None]:
    """``{"text": {kind: target}, "integer": {"2": target}}``; a lifecycle record's mapping is
    keyed by text only (its severity or mode)."""
    where = f"vendors.{name}"
    sections = frozenset({TEXT}) if name in LIFECYCLE_DEFAULT_KIND else frozenset({TEXT, INTEGER})
    spec = _keys(value, sections, where)
    out: dict[Key, str | None] = {}
    for section, entries in sorted(spec.items()):
        if not isinstance(entries, dict):
            raise _Bad(f"{where}.{section} must be an object of declared kind -> event kind")
        for stated, target in sorted(entries.items()):
            if not isinstance(stated, str) or not stated:
                raise _Bad(f"{where}.{section} has an empty declared kind")
            if section == INTEGER and not _INTEGER.fullmatch(stated):
                raise _Bad(f"{where}.integer.{stated}: not an integer written canonically")
            if target == NOT_AN_EVENT:
                if name in LIFECYCLE_DEFAULT_KIND:
                    raise _Bad(f"{where}.{stated}: a {name} is always an event; map it to a kind")
                out[(section, stated)] = None
                continue
            if not isinstance(target, str) or target not in EVENT_KINDS:
                raise _Bad(f"{where}.{section}.{stated}: {target!r} is not a registered event kind")
            out[(section, stated)] = target
    return out


def parse_config(config: Mapping[str, JsonValue]) -> EventConfig:
    """The usable parts of a resolved config, and a problem for each part refused."""
    problems: list[str] = []
    extra = sorted(set(config) - _TOP)
    if extra:
        problems.append(f"unexpected keys {extra}; ignored")
    window: Fraction | None = None
    partners = DEFAULT_MAX_PARTNERS
    try:
        co = _keys(config.get("co_occurrence", {}), _CO, "co_occurrence")
        window = _window(co.get("window_seconds", DEFAULT_WINDOW))
        given = co.get("max_partners", DEFAULT_MAX_PARTNERS)
        if (
            isinstance(given, bool)
            or not isinstance(given, int)
            or not 1 <= given <= MAX_PARTNERS_LIMIT
        ):
            raise _Bad(f"co_occurrence.max_partners is an integer in [1, {MAX_PARTNERS_LIMIT}]")
        partners = given
    except _Bad as exc:
        window = None
        problems.append(f"{exc}; no co-occurrence is claimed")
    vendors: dict[str, dict[Key, str | None]] = {}
    raw_vendors = config.get("vendors", {})
    if not isinstance(raw_vendors, dict):
        problems.append("vendors must be an object; no vendor mapping is used")
        raw_vendors = {}
    for name, value in sorted(raw_vendors.items()):
        try:
            vendors[_text(name, "vendor name")] = _vendor(name, value)
        except _Bad as exc:
            problems.append(f"{exc}; vendor ignored")
    tables: list[TableSpec] = []
    raw_tables = config.get("tables", [])
    if not isinstance(raw_tables, list):
        problems.append("tables must be a list; no table is read")
        raw_tables = []
    for index, value in enumerate(raw_tables):
        try:
            spec = _table(value, index, vendors)
            if any(t.name == spec.name for t in tables):
                raise _Bad(f"tables[{index}].name {spec.name!r} is declared twice")
            tables.append(spec)
        except _Bad as exc:
            problems.append(f"{exc}; table ignored")
    return EventConfig(window, partners, vendors, tuple(tables), tuple(problems))
