"""Parsing the Ledger records the identity consolidator reads (ADR 0003 §1, ADR 0008 §1).

Parsing is kept apart from the identity policy: each parser turns one Ledger record into a typed
value or raises ``Malformed``, and decides nothing about identity. ``consolidate.identity`` applies
the policy to what these return.

Compiler kinds are parsed by the compiler's own strict readers, so Memory reads exactly the
package-schema shape (``identity_link`` since 3, ``assertion`` since 5, ``timestamp_domain``):

- ``identity_link`` (root ADR 0050 §4): ``left`` a ``LogicalId``; ``right`` ``Known`` or
  ``Ambiguous``; ``identifier`` ``NotApplicable`` (``co_declared``) or ``Known``/``Ambiguous``
  (``shared_identifier``); ``evidence`` the other declarations (empty for ``co_declared``, whose
  citation is its ``provenance``); ``validity`` a window ``[start, end)`` on a named clock.
- ``assertion`` (root ADR 0062): what a person stated, ``stated`` by construction; a ``retract``
  names the assertion it withdraws by that assertion's declared ``identifier``.
- ``timestamp_domain``: read only to place a stated instant on a shared civil clock when the
  domain declares its timescale, epoch and resolution (Memory ADR 0002 §3).
- ``incident_record`` and ``intervention`` (root ADR 0051): read only for the ids they declare
  themselves by (``identifiers``), so an assertion that names an event by its declared id reaches
  the event node ``memory.events`` keyed by the record (ADR 0019 §1).
- ``machine`` (root ADR 0019 §1, ADR 0072 §1): the ids one declaration gives one machine, a
  manifest entry's id and its aliases among them (Memory ADR 0021).

Two kinds are Ledger stand-ins until Memory reads the catalog API (MVL-85) and the compiler emits
configuration lineage (MVL-38): ``ledger_thread {id, logical_id, node_type, valid_from, evidence}``
and ``configuration_lineage {id, predecessor, successor, valid_from, evidence}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypeVar

from neptune.model.alignment import identity_link_from_json
from neptune.model.assertion import AssertionType, assertion_from_json
from neptune.model.ids import LogicalId, RecordId, logical_id_from_json, parse_record_id
from neptune.model.knowledge import Ambiguous, AssertionKind, Candidate, Known
from neptune.model.lifecycle import incident_record_from_json, intervention_from_json
from neptune.model.machine import machine_from_json
from neptune.model.provenance import EvidenceRef, Provenance, evidence_ref_from_json
from neptune.model.reference import timestamp_domain_from_json
from neptune.model.time import Timestamp, timestamp_from_json
from neptune_memory.schema.interval import OPEN, CivilClock, Open
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import is_declared_value

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from fractions import Fraction

    from neptune.model.alignment import ValidityWindow
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.knowledge import Knowledge
    from neptune.model.time import Epoch, Timescale

_T = TypeVar("_T")

# Ledger record kinds the identity consolidator reads.
THREAD: Final = "ledger_thread"
IDENTITY_LINK: Final = "identity_link"
CONFIGURATION_LINEAGE: Final = "configuration_lineage"
ASSERTION: Final = "assertion"
TIMESTAMP_DOMAIN: Final = "timestamp_domain"
INCIDENT_RECORD: Final = "incident_record"
INTERVENTION: Final = "intervention"
MACHINE: Final = "machine"

# What grounds a ``same_as`` (ADR 0003 §1.2, ADR 0021): the record kind it rests on.
Ground = Literal[
    "identity_link", "configuration_lineage", "operator_assertion", "machine_declaration"
]

# Node ids in this namespace are Memory's own record-keyed nodes (``record:<record id>``: runs,
# streams, events), so a declared id in it would name one of them (ADR 0021 §4).
RESERVED_NAMESPACE: Final = "record"


class Malformed(ValueError):
    """A record the identity consolidator cannot read; it becomes a finding, never a claim."""


class Inferred(ValueError):
    """An inferred identity link (a ``derived/`` record): never a ground for ``same_as``."""


def _field(record: Mapping[str, object], name: str) -> object:
    if name not in record:
        raise Malformed(f"missing {name!r}")
    return record[name]


def _parsed(record: Mapping[str, object], name: str, parse: Callable[[JsonValue], _T]) -> _T:
    try:
        return parse(_field(record, name))  # type: ignore[arg-type]
    except Malformed:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise Malformed(f"{name!r}: {exc}") from exc


def declared(node: LogicalId) -> LogicalId:
    """A declared logical id; a blank or whitespace-padded value is malformed (ADR 0006 §9)."""
    if not is_declared_value(node.value):
        raise Malformed(f"logical id value is blank or padded with whitespace: {node.value!r}")
    return node


def _logical_id(data: JsonValue) -> LogicalId:
    return declared(logical_id_from_json(data))


def _evidence(record: Mapping[str, object]) -> tuple[EvidenceRef, ...]:
    value = _field(record, "evidence")
    if not isinstance(value, (list, tuple)) or not value:
        raise Malformed("'evidence' must be a non-empty list of evidence refs")
    try:
        return tuple(evidence_ref_from_json(item) for item in value)
    except (ValueError, TypeError, KeyError) as exc:
        raise Malformed(f"'evidence': {exc}") from exc


def _strict(parse: Callable[[JsonValue], _T], record: Mapping[str, object]) -> _T:
    """A compiler reader over one record; whatever it refuses is malformed here."""
    try:
        return parse(dict(record))  # type: ignore[arg-type]
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise Malformed(str(exc) or type(exc).__name__) from exc


def _known(knowledge: Knowledge[_T]) -> _T | None:
    return knowledge.value if isinstance(knowledge, Known) else None


def _cited(provenance: object) -> tuple[EvidenceRef, ...]:
    """The evidence a value's own provenance cites; nothing when it inherits its record's."""
    return (provenance.evidence,) if isinstance(provenance, Provenance) else ()


# --- Ledger stand-ins ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Thread:
    """A Ledger thread: one declared logical id and the node type it declares."""

    record: RecordId
    node: LogicalId
    node_type: NodeType
    valid_from: Timestamp
    evidence: tuple[EvidenceRef, ...]


def thread(record: Mapping[str, object]) -> Thread:
    return Thread(
        _parsed(record, "id", parse_record_id),  # type: ignore[arg-type]
        _parsed(record, "logical_id", _logical_id),
        _parsed(record, "node_type", NodeType),  # type: ignore[arg-type]
        _parsed(record, "valid_from", timestamp_from_json),
        _evidence(record),
    )


# --- Identity statements ------------------------------------------------------------------------


@dataclass(frozen=True)
class Side:
    """One id the right side of a statement names, with the evidence it cites of its own."""

    node: LogicalId
    evidence: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class Window:
    """When a statement holds, as its record states: ``start`` ``None`` when it states none.

    ``evidence`` is what the window's own candidates cite, when the evidence left the window (or
    a bound of it) ``Ambiguous`` and this is one of its readings; empty otherwise.
    """

    start: Timestamp | None
    end: Timestamp | Open
    evidence: tuple[EvidenceRef, ...] = ()


UNSTATED: Final = Window(None, OPEN)

# The most windows one statement's ambiguous validity is read as (candidate windows times their
# bounds' candidates): past it the statement is refused, so hostile input cannot multiply claims.
MAX_WINDOWS: Final = 64


@dataclass(frozen=True)
class Link:
    """One identity statement from one record: ``left`` names what ``right`` names.

    ``decided`` is ``True`` when the record states the identity (one side) over one window;
    ``False`` when the evidence leaves it ambiguous (the compiler's ``Ambiguous`` right side, an
    ``Ambiguous`` shared identifier, or an ``Ambiguous`` validity or bound), and then every side
    over every window is a candidate. ``windows`` is empty when the ambiguous validity has more
    readings than ``MAX_WINDOWS``.
    """

    record: RecordId
    ground: Ground
    assertion_kind: AssertionKind
    left: LogicalId
    right: tuple[Side, ...]
    decided: bool
    windows: tuple[Window, ...]
    evidence: tuple[EvidenceRef, ...]
    also: tuple[RecordId, ...] = ()  # other records its claims cite (an ambiguous retraction)


def _readings(knowledge: Knowledge[_T]) -> tuple[tuple[_T | None, tuple[EvidenceRef, ...]], ...]:
    """What a value may be, each with the evidence its own candidate cites: one ``Known`` value,
    every candidate of an ``Ambiguous`` one, or ``None`` (not stated) for any other state."""
    if isinstance(knowledge, Known):
        return ((knowledge.value, ()),)
    if isinstance(knowledge, Ambiguous):
        return tuple((c.value, _cited(c.provenance)) for c in knowledge.candidates)
    return ((None, ()),)


def windows(validity: Knowledge[ValidityWindow]) -> tuple[Window, ...]:
    """``[start, end)`` as a link states it, one ``Window`` per reading (ADR 0008 §2).

    A bound that is not stated (``KnownAbsent``, ``Unknown``, ``NotCovered``) follows ADR 0008
    §2: no start, or an ``OPEN`` end, valid until further notice. An ``Ambiguous`` window or
    bound is never read as unstated: each of its candidates is a window of its own, citing that
    candidate, and the statement holds over none of them for certain. Empty past ``MAX_WINDOWS``.
    """
    found: list[Window] = []
    total = 0
    for value, cited in _readings(validity):
        if value is None:
            return (UNSTATED,)
        starts, ends = _readings(value.start), _readings(value.end)
        total += len(starts) * len(ends)
        if total > MAX_WINDOWS:
            return ()
        for start, from_start in starts:
            for end, from_end in ends:
                window = Window(
                    start, OPEN if end is None else end, (*cited, *from_start, *from_end)
                )
                if window not in found:
                    found.append(window)
    return tuple(found)


def _ambiguous(validity: Knowledge[ValidityWindow]) -> bool:
    return isinstance(validity, Ambiguous) or (
        isinstance(validity, Known)
        and any(isinstance(b, Ambiguous) for b in (validity.value.start, validity.value.end))
    )


def identity_link(record: Mapping[str, object]) -> Link:
    """The compiler's ``IdentityLink``, read by the compiler's strict reader."""
    provenance = record.get("provenance")
    if isinstance(provenance, dict) and provenance.get("assertion_kind") == "inferred":
        raise Inferred("an inferred identity link is a derived/ record")
    link = _strict(identity_link_from_json, record)
    match link.right:
        case Known(value=value, provenance=cited):
            sides: tuple[Side, ...] = (Side(declared(value), _cited(cited)),)
        case Ambiguous(candidates=candidates):
            sides = tuple(Side(declared(c.value), _cited(c.provenance)) for c in candidates)
    for value in _values(link.identifier):
        declared(value)
    return Link(
        record=link.id,
        ground="identity_link",
        assertion_kind=link.provenance.assertion_kind,
        left=declared(link.left),
        right=sides,
        decided=isinstance(link.right, Known)
        and not isinstance(link.identifier, Ambiguous)
        and not _ambiguous(link.validity),
        windows=windows(link.validity),
        evidence=(link.provenance.evidence, *link.evidence),
    )


def _values(knowledge: Knowledge[LogicalId]) -> Iterable[LogicalId]:
    if isinstance(knowledge, Known):
        return (knowledge.value,)
    if isinstance(knowledge, Ambiguous):
        return tuple(c.value for c in knowledge.candidates)
    return ()


def configuration_lineage(record: Mapping[str, object]) -> Link:
    """A Ledger record declaring one thread the continuation of another (stand-in shape)."""
    return Link(
        record=_parsed(record, "id", parse_record_id),  # type: ignore[arg-type]
        ground="configuration_lineage",
        assertion_kind=AssertionKind.OBSERVED,
        left=_parsed(record, "predecessor", _logical_id),
        right=(Side(_parsed(record, "successor", _logical_id)),),
        decided=True,
        windows=(Window(_parsed(record, "valid_from", timestamp_from_json), OPEN),),
        evidence=_evidence(record),
    )


# --- Human assertions ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Statement:
    """A person's assertion (root ADR 0062), as far as identity reads it.

    ``None`` is a field the record does not state as ``Known``. ``nodes`` are the logical ids of
    a ``Known`` scope in declared order, each once; ``None`` when the scope is not ``Known``.
    ``records`` are its record ids, likewise: a record id names evidence, and identity reads one
    only where it is the record of an event node (ADR 0019 §1).
    ``windows`` holds from ``authored_at``, one per reading: several, and ``timed`` ``False``,
    when ``authored_at`` is ``Ambiguous`` (empty past ``MAX_WINDOWS``). ``identifiers`` and
    ``retracts`` are every id the field may be: one when ``Known``, each candidate when
    ``Ambiguous`` (and then its ``*_ambiguous`` flag is set), none otherwise.
    """

    record: RecordId
    identifiers: tuple[LogicalId, ...]
    identifier_ambiguous: bool
    assertion_type: AssertionType | None
    nodes: tuple[LogicalId, ...] | None
    retracts: tuple[LogicalId, ...]
    retracts_ambiguous: bool
    windows: tuple[Window, ...]
    timed: bool
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...] = ()


def assertion(record: Mapping[str, object]) -> Statement:
    """The compiler's ``Assertion``, read by the compiler's strict reader."""
    parsed = _strict(assertion_from_json, record)
    nodes: tuple[LogicalId, ...] | None = None
    records: list[RecordId] = []
    if isinstance(parsed.scope, Known):
        found: list[LogicalId] = []
        for ref in parsed.scope.value:
            if isinstance(ref, LogicalId):
                if declared(ref) not in found:
                    found.append(ref)
            elif ref not in records:
                records.append(ref)
        nodes = tuple(found)
    starts = _readings(parsed.authored_at)
    return Statement(
        record=parsed.id,
        identifiers=tuple(declared(i) for i in _values(parsed.identifier)),
        identifier_ambiguous=isinstance(parsed.identifier, Ambiguous),
        assertion_type=_known(parsed.assertion_type),
        nodes=nodes,
        retracts=tuple(declared(r) for r in _values(parsed.retracts)),
        retracts_ambiguous=isinstance(parsed.retracts, Ambiguous),
        windows=()
        if len(starts) > MAX_WINDOWS
        else tuple(Window(start, OPEN, cited) for start, cited in starts),
        timed=not isinstance(parsed.authored_at, Ambiguous),
        evidence=(parsed.provenance.evidence,),
        records=tuple(records),
    )


# --- Event records ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Declaring:
    """The ids an event's record declares itself by (its ``identifiers``): ``certain`` the
    ``Known`` items of a ``Known`` list; ``possible`` every candidate of an ``Ambiguous`` item, or
    of every reading of an ``Ambiguous`` list. A list not stated declares nothing. ``refused``
    counts ids that are blank or padded (ADR 0006 §9): they name nothing, and the rest still do."""

    record: RecordId
    certain: tuple[LogicalId, ...]
    possible: tuple[LogicalId, ...]
    refused: int = 0


def _declaring(
    record_id: RecordId, listed: Knowledge[tuple[Knowledge[LogicalId], ...]]
) -> Declaring:
    certain: list[LogicalId] = []
    possible: list[LogicalId] = []
    refused = 0
    lists = (
        [(listed.value, True)]
        if isinstance(listed, Known)
        else [(c.value, False) for c in listed.candidates]
        if isinstance(listed, Ambiguous)
        else []
    )
    for items, decided in lists:
        for item in items:
            for value in _values(item):
                into = certain if decided and isinstance(item, Known) else possible
                if not is_declared_value(value.value):
                    refused += 1
                elif value not in into:
                    into.append(value)
    possible = [p for p in possible if p not in certain]
    return Declaring(record_id, tuple(certain), tuple(possible), refused)


def incident_identifiers(record: Mapping[str, object]) -> Declaring:
    parsed = _strict(incident_record_from_json, record)
    return _declaring(parsed.id, parsed.identifiers)


def intervention_identifiers(record: Mapping[str, object]) -> Declaring:
    parsed = _strict(intervention_from_json, record)
    return _declaring(parsed.id, parsed.identifiers)


# --- Machine declarations -----------------------------------------------------------------------


@dataclass(frozen=True)
class Declaration:
    """A ``Machine`` record, as far as identity reads it (ADR 0021).

    ``known`` are its ``Known`` ids in canonical order, each with its own citation; ``ambiguous``
    one tuple per ``Ambiguous`` identifier, its candidates. ``refused`` are ids Memory cannot key a
    node by (blank or padded, or in ``RESERVED_NAMESPACE``), with every other id still read.
    ``document`` is the source the declaration cites: two machines one document lists are two.
    """

    record: RecordId
    assertion_kind: AssertionKind
    known: tuple[Side, ...]
    ambiguous: tuple[tuple[Side, ...], ...]
    refused: tuple[LogicalId, ...]
    evidence: tuple[EvidenceRef, ...]
    document: object


def _usable(node: LogicalId) -> bool:
    return is_declared_value(node.value) and node.namespace != RESERVED_NAMESPACE


def machine(record: Mapping[str, object]) -> Declaration:
    """The compiler's ``Machine``, read by the compiler's strict reader."""
    provenance = record.get("provenance")
    if isinstance(provenance, dict) and provenance.get("assertion_kind") == "inferred":
        raise Inferred("an inferred machine is a derived/ record")
    parsed = _strict(machine_from_json, record)
    known: list[Side] = []
    ambiguous: list[tuple[Side, ...]] = []
    refused: list[LogicalId] = []
    for item in parsed.identifiers:
        readings = item.candidates if isinstance(item, Ambiguous) else (item,)
        sides = tuple(
            Side(c.value, _cited(c.provenance))
            for c in readings
            if isinstance(c, Known | Candidate)
        )
        refused.extend(s.node for s in sides if not _usable(s.node))
        usable = tuple(s for s in sides if _usable(s.node))
        if isinstance(item, Ambiguous):
            if usable:
                ambiguous.append(usable)
        else:
            known.extend(usable)
    return Declaration(
        record=parsed.id,
        assertion_kind=parsed.provenance.assertion_kind,
        known=tuple(known),
        ambiguous=tuple(ambiguous),
        refused=tuple(refused),
        evidence=(parsed.provenance.evidence,),
        document=parsed.provenance.evidence.source,
    )


# --- Clocks -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Clock:
    """A ``TimestampDomain``, and the ``CivilClock`` it names when it declares a civil timescale,
    an absolute epoch and its resolution (ADR 0002 §3); ``civil`` is ``None`` for any other clock
    (a boot clock, a GPS week count, an unset RTC), whose instants stay on their own domain.
    ``resolution`` is its stated seconds per tick, or ``None`` when it states none."""

    record: RecordId
    civil: CivilClock | None
    resolution: Fraction | None = None


def clock(record: Mapping[str, object]) -> Clock:
    domain = _strict(timestamp_domain_from_json, record)
    timescale: Timescale | None = _known(domain.timescale)
    epoch: Epoch | None = _known(domain.epoch)
    resolution = _known(domain.resolution)
    if timescale is None or epoch is None or resolution is None:
        return Clock(domain.id, None, resolution)
    try:
        return Clock(domain.id, CivilClock(timescale, epoch, resolution), resolution)
    except ValueError:  # a timescale or epoch that is not civil
        return Clock(domain.id, None, resolution)
