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

Two kinds are Ledger stand-ins until Memory reads the catalog API (MVL-85) and the compiler emits
configuration lineage (MVL-38): ``ledger_thread {id, logical_id, node_type, valid_from, evidence}``
and ``configuration_lineage {id, predecessor, successor, valid_from, evidence}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypeVar

from neptune.model.alignment import IdentityLink, identity_link_from_json
from neptune.model.assertion import AssertionType, assertion_from_json
from neptune.model.ids import LogicalId, RecordId, logical_id_from_json, parse_record_id
from neptune.model.knowledge import Ambiguous, AssertionKind, Known
from neptune.model.provenance import EvidenceRef, Provenance, evidence_ref_from_json
from neptune.model.reference import timestamp_domain_from_json
from neptune.model.time import Timestamp, timestamp_from_json
from neptune_memory.schema.interval import OPEN, CivilClock, Open
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import is_declared_value

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

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

# What grounds a ``same_as`` (ADR 0003 §1.2): the record kind it rests on.
Ground = Literal["identity_link", "configuration_lineage", "operator_assertion"]


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
    """When a statement holds, as its record states: ``start`` ``None`` when it states none."""

    start: Timestamp | None
    end: Timestamp | Open


UNSTATED: Final = Window(None, OPEN)


@dataclass(frozen=True)
class Link:
    """One identity statement from one record: ``left`` names what ``right`` names.

    ``decided`` is ``True`` when the record states the identity (one side); ``False`` when the
    evidence leaves it ambiguous (the compiler's ``Ambiguous`` right side, or an ``Ambiguous``
    shared identifier), and then every side is a candidate.
    """

    record: RecordId
    ground: Ground
    assertion_kind: AssertionKind
    left: LogicalId
    right: tuple[Side, ...]
    decided: bool
    window: Window
    evidence: tuple[EvidenceRef, ...]


def _window(link: IdentityLink) -> Window:
    """``[start, end)`` as the link states it. An end that is not ``Known`` is ``OPEN``: valid
    until further notice, as for a run whose last instant is not stated (ADR 0008 §2)."""
    if not isinstance(link.validity, Known):
        return UNSTATED
    window = link.validity.value
    end = _known(window.end)
    return Window(_known(window.start), OPEN if end is None else end)


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
        decided=isinstance(link.right, Known) and not isinstance(link.identifier, Ambiguous),
        window=_window(link),
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
        window=Window(_parsed(record, "valid_from", timestamp_from_json), OPEN),
        evidence=_evidence(record),
    )


# --- Human assertions ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Statement:
    """A person's assertion (root ADR 0062), as far as identity reads it.

    ``None`` is a field the record does not state as ``Known``. ``nodes`` are the logical ids of
    a ``Known`` scope in declared order, each once (record ids in a scope name evidence, not
    things, so identity does not read them); ``None`` when the scope is not ``Known``.
    """

    record: RecordId
    identifier: LogicalId | None
    assertion_type: AssertionType | None
    nodes: tuple[LogicalId, ...] | None
    retracts: LogicalId | None
    authored_at: Timestamp | None
    evidence: tuple[EvidenceRef, ...]


def assertion(record: Mapping[str, object]) -> Statement:
    """The compiler's ``Assertion``, read by the compiler's strict reader."""
    parsed = _strict(assertion_from_json, record)
    nodes: tuple[LogicalId, ...] | None = None
    if isinstance(parsed.scope, Known):
        found: list[LogicalId] = []
        for ref in parsed.scope.value:
            if isinstance(ref, LogicalId) and declared(ref) not in found:
                found.append(ref)
        nodes = tuple(found)
    identifier, retracts = _known(parsed.identifier), _known(parsed.retracts)
    return Statement(
        record=parsed.id,
        identifier=None if identifier is None else declared(identifier),
        assertion_type=_known(parsed.assertion_type),
        nodes=nodes,
        retracts=None if retracts is None else declared(retracts),
        authored_at=_known(parsed.authored_at),
        evidence=(parsed.provenance.evidence,),
    )


# --- Clocks -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Clock:
    """A ``TimestampDomain``, and the ``CivilClock`` it names when it declares a civil timescale,
    an absolute epoch and its resolution (ADR 0002 §3); ``civil`` is ``None`` for any other clock
    (a boot clock, a GPS week count, an unset RTC), whose instants stay on their own domain."""

    record: RecordId
    civil: CivilClock | None


def clock(record: Mapping[str, object]) -> Clock:
    domain = _strict(timestamp_domain_from_json, record)
    timescale: Timescale | None = _known(domain.timescale)
    epoch: Epoch | None = _known(domain.epoch)
    resolution = _known(domain.resolution)
    if timescale is None or epoch is None or resolution is None:
        return Clock(domain.id, None)
    try:
        return Clock(domain.id, CivilClock(timescale, epoch, resolution))
    except ValueError:  # a timescale or epoch that is not civil
        return Clock(domain.id, None)
