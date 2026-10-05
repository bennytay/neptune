"""Parsing what the event consolidator reads: Ledger records and its config (ADR 0013 §1, §3).

Parsing is kept apart from the event policy: each parser turns one Ledger record into a typed
value or raises ``Malformed``, and decides nothing about events. ``consolidate.events`` applies
the policy.

Every kind is a compiler kind, read with the compiler's own strict reader:

- ``incident_record`` and ``intervention`` (root ADR 0051): lifecycle records, ``stated`` by a form,
  a CMMS row or a ticket; Deploy maps them from CMMS exports, ticket systems and Formant.
- ``structured_table`` and ``structured_record`` (root ADR 0020 §5): a table and its rows, read as
  events only where the config declares the table an event table (by its declared name). Deploy's
  ROS 2 diagnostics mapper writes such a table (``diagnostic events``, with a ``@clock:stamp``
  companion naming the clock); a PLC, safety-controller or syslog export is another.
- ``timestamp_domain``: a clock's resolution (to scale the co-occurrence window) and whether it
  declares itself civil; ``clock_mapping`` (root ADR 0050 §5): a stated map between two clocks.

No stand-in kind is read. A bag's topics, a flight log's logged messages and any time written as
text are not records the compiler produces as events yet, so nothing here reads them.

The config (``EventConfig``) declares the co-occurrence window, the vendor mappings from a source's
own kinds to the registered ``EVENT_KINDS``, and the event tables with the columns that hold each
field. ``resolve_config`` fills in defaults; ``parse_config`` refuses what it cannot use part by
part, so one bad table never disables the others.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import TYPE_CHECKING, Final, TypeVar

from neptune.identity import canonical_json
from neptune.model.ids import RecordId, check_token, parse_record_id
from neptune.model.knowledge import Known
from neptune.model.lifecycle import (
    IncidentRecord,
    Intervention,
    incident_record_from_json,
    intervention_from_json,
)
from neptune.model.reference import timestamp_domain_from_json
from neptune.model.world import (
    StructuredRecord,
    StructuredTable,
    structured_record_from_json,
    structured_table_from_json,
)
from neptune_memory.consolidate.identity_records import Malformed
from neptune_memory.consolidate.run_records import Inferred, mapping
from neptune_memory.schema.interval import CivilClock
from neptune_memory.schema.predicates import EVENT_KINDS

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from neptune.model.jsonvalue import JsonValue

_T = TypeVar("_T")

# Ledger record kinds the event consolidator reads.
INCIDENT_RECORD: Final = "incident_record"
INTERVENTION: Final = "intervention"
STRUCTURED_TABLE: Final = "structured_table"
STRUCTURED_RECORD: Final = "structured_record"
TIMESTAMP_DOMAIN: Final = "timestamp_domain"
CLOCK_MAPPING: Final = "clock_mapping"

# The vendor names of the two lifecycle kinds: an incident's mapping is keyed by its stated
# severity, an intervention's by its stated mode. Unmapped, each is its own registered kind.
LIFECYCLE_DEFAULT_KIND: Final[Mapping[str, str]] = {
    INCIDENT_RECORD: "incident",
    INTERVENTION: "intervention",
}

# A vendor mapping's target for a declared kind that is not an event (an OK status, an info line).
# Canonical JSON has no null, so "not an event" is this reserved word, never a registered kind.
NOT_AN_EVENT: Final = "not_an_event"

DEFAULT_WINDOW: Final = "5"  # seconds, as decimal text
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
    "INCIDENT_RECORD",
    "INTERVENTION",
    "LIFECYCLE_DEFAULT_KIND",
    "NOT_AN_EVENT",
    "STRUCTURED_RECORD",
    "STRUCTURED_TABLE",
    "TIMESTAMP_DOMAIN",
    "ClockSpec",
    "Domain",
    "EventConfig",
    "IdColumn",
    "Inferred",
    "Malformed",
    "TableSpec",
    "TimeSpec",
    "domain",
    "incident",
    "intervention",
    "mapping",
    "parse_config",
    "resolve_config",
    "row",
    "table",
]


def _strict(parse: Callable[[JsonValue], _T], record: Mapping[str, object]) -> _T:
    """A compiler reader over one record; whatever it refuses is malformed here."""
    provenance = record.get("provenance")
    if isinstance(provenance, dict) and provenance.get("assertion_kind") == "inferred":
        raise Inferred(f"an inferred {record.get('kind')!r} record is a derived/ record")
    try:
        return parse(dict(record))  # type: ignore[arg-type]
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise Malformed(str(exc) or type(exc).__name__) from exc


def incident(record: Mapping[str, object]) -> IncidentRecord:
    return _strict(incident_record_from_json, record)


def intervention(record: Mapping[str, object]) -> Intervention:
    return _strict(intervention_from_json, record)


def table(record: Mapping[str, object]) -> StructuredTable:
    return _strict(structured_table_from_json, record)


def row(record: Mapping[str, object]) -> StructuredRecord:
    return _strict(structured_record_from_json, record)


@dataclass(frozen=True)
class Domain:
    """A clock as the event policy needs it: its stated resolution (seconds per tick), and the
    ``CivilClock`` it names when it declares a civil timescale, an absolute epoch and its
    resolution (ADR 0002 §3)."""

    record: RecordId
    resolution: Fraction | None
    civil: CivilClock | None


def domain(record: Mapping[str, object]) -> Domain:
    parsed = _strict(timestamp_domain_from_json, record)

    def known(value: object) -> object:
        return value.value if isinstance(value, Known) else None

    resolution, timescale, epoch = (
        known(parsed.resolution),
        known(parsed.timescale),
        known(parsed.epoch),
    )
    civil: CivilClock | None = None
    if isinstance(resolution, Fraction) and timescale is not None and epoch is not None:
        try:
            civil = CivilClock(timescale, epoch, resolution)  # type: ignore[arg-type]
        except ValueError:  # a timescale or epoch that is not civil
            civil = None
    return Domain(parsed.id, resolution if isinstance(resolution, Fraction) else None, civil)


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
    vendors: Mapping[str, Mapping[str, str | None]]
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
        out["co_occurrence"] = {**DEFAULT_CONFIG["co_occurrence"], **co}  # type: ignore[dict-item]
    return canonical_json.loads(canonical_json.dumps(out))  # type: ignore[return-value]


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
    if isinstance(value, bool):
        raise _Bad("co_occurrence.window_seconds must be a decimal text or an integer")
    if isinstance(value, int):
        seconds = Fraction(value)
    elif isinstance(value, str):
        try:
            decimal = Decimal(value)
        except InvalidOperation:
            raise _Bad("co_occurrence.window_seconds is not a decimal number") from None
        if not decimal.is_finite():
            raise _Bad("co_occurrence.window_seconds must be finite")
        seconds = Fraction(decimal)
    else:
        raise _Bad("co_occurrence.window_seconds must be a decimal text or an integer")
    if seconds <= 0:
        raise _Bad("co_occurrence.window_seconds must be positive")
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


def _vendor(name: str, value: object) -> dict[str, str | None]:
    where = f"vendors.{name}"
    if not isinstance(value, dict):
        raise _Bad(f"{where} must be an object of declared kind -> event kind")
    out: dict[str, str | None] = {}
    for declared, target in sorted(value.items()):
        if not isinstance(declared, str) or not declared:
            raise _Bad(f"{where} has an empty declared kind")
        if target == NOT_AN_EVENT:
            if name in LIFECYCLE_DEFAULT_KIND:
                raise _Bad(f"{where}.{declared}: a {name} is always an event; map it to a kind")
            out[declared] = None
            continue
        if not isinstance(target, str) or target not in EVENT_KINDS:
            raise _Bad(f"{where}.{declared}: {target!r} is not a registered event kind")
        out[declared] = target
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
    vendors: dict[str, dict[str, str | None]] = {}
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
