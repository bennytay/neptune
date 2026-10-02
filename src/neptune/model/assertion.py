"""Human assertions as stated evidence: what a person declared about other records (ADR 0062).

An ``Assertion`` is one statement a person makes about records or real-world things: that two
robots' ids name one machine, that a manipulator cell's commissioning baseline is accepted, a
note on an incident, or the retraction of an earlier assertion. It reaches the compiler as a
source like any other (an assertion file, a console export) and is stored as its author declared
it, ``stated`` and cited, with nothing resolved or applied:

- ``author`` is a declared identity, never matched to a person or account.
- ``authored_at`` is the civil time the source writes, as ticks by ADR 0023 §2, and
  ``authored_zone`` the IANA zone name it declares, never looked up. When the Ledger registered
  the assertion is the Ledger's transaction time, not part of this record.
- ``scope`` lists the record ids and logical ids the assertion is about, as declared.
- ``retracts`` names an earlier assertion by its declared id. A retraction is a new record; the
  retracted one is never deleted or changed.
- ``payload`` is the declared payload's JSON text exactly as written; ``rationale`` the free text
  that explains it, citing the span it is written at.

What an assertion does to anything else (a merge, a baseline, a retraction taking effect) is a
consumer's decision (Memory's identity and baseline policies), never the compiler's.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Final, TypeAlias

from neptune.model._fields import (
    check_text_values,
    check_type,
    enum_decoder,
    json_array,
    text_decoder,
    values_of,
)
from neptune.model.ids import LogicalId, RecordId, logical_id_from_json, parse_record_id
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
from neptune.model.time import Timestamp, timestamp_from_json

# The schema version that added ``Assertion`` (ADR 0062, ADR 0037 §1). Provisional: kind-adding
# PRs are numbered in merge order.
ASSERTION_SINCE: Final = 4

# A thing an assertion is about: a canonical record by its id, or a real-world thing by a
# logical id. In JSON a record id is a string and a logical id an object.
ScopeRef: TypeAlias = RecordId | LogicalId

# An IANA time zone database name, by syntax only: components that start with a letter, then
# letters, digits and ``._+-``, joined by ``/`` (``Europe/Berlin``, ``America/Port-au-Prince``,
# ``Etc/GMT-5``, ``UTC``).
# Whether a tz database release holds the name is never checked: a record's bytes may not depend
# on one (ADR 0062 §4).
_ZONE_PART: Final = r"[A-Za-z][A-Za-z0-9._+\-]*"
_IANA_ZONE: Final = re.compile(f"{_ZONE_PART}(?:/{_ZONE_PART})*")
IANA_ZONE_MAX: Final = 255


class AssertionType(StrEnum):
    """What an assertion declares (ADR 0062 §2). The compiler stores it and applies none."""

    SAME_IDENTITY = "same_identity"  # the scope's ids name one real-world thing
    DISTINCT_IDENTITY = "distinct_identity"  # the scope's ids name different things
    ACCEPT_BASELINE = "accept_baseline"  # the scope's records are accepted as a baseline
    REJECT_BASELINE = "reject_baseline"  # the scope's records are rejected as a baseline
    ANNOTATE = "annotate"  # a note about the scope, in its rationale and payload
    RETRACT = "retract"  # withdraws the earlier assertion ``retracts`` names


def is_iana_zone(name: str) -> bool:
    """Whether ``name`` is spelled as an IANA zone name; it is not looked up anywhere."""
    return len(name) <= IANA_ZONE_MAX and _IANA_ZONE.fullmatch(name) is not None


def _check_states(name: str, value: Knowledge[Any], allowed: tuple[type, ...]) -> None:
    if not isinstance(value, allowed):
        states = ", ".join(cls.__name__ for cls in allowed)
        raise ValueError(f"{name} must be one of {states}, got {value!r}")


# Fields every assertion has a value for, which the source states or does not.
_DECLARED: Final = (Known, Ambiguous, Unknown, NotCovered)
# Optional parts: the format defines leaving one out as stating none (ADR 0062 §3).
_OPTIONAL: Final = (Known, Ambiguous, KnownAbsent, Unknown, NotCovered)


def _scope_ref_json(ref: ScopeRef) -> JsonValue:
    return ref if isinstance(ref, str) else ref.to_json()


def _scope_ref(data: JsonValue) -> ScopeRef:
    if isinstance(data, str):
        return parse_record_id(data)
    return logical_id_from_json(data)


def _scope_json(scope: tuple[ScopeRef, ...]) -> JsonValue:
    return [_scope_ref_json(ref) for ref in scope]


def _scope_from_json(data: JsonValue) -> tuple[ScopeRef, ...]:
    return tuple(_scope_ref(item) for item in json_array(data, "scope"))


def _check_scope(scope: Knowledge[tuple[ScopeRef, ...]]) -> None:
    _check_states("scope", scope, _DECLARED)
    check_type("scope", scope, tuple)
    for refs in values_of(scope):
        for ref in refs:
            if isinstance(ref, str):
                parse_record_id(ref)
            elif not isinstance(ref, LogicalId):
                raise ValueError(f"a scope entry is a record id or a LogicalId, got {ref!r}")


def _logical_json(value: LogicalId) -> JsonValue:
    return value.to_json()


@dataclass(frozen=True)
class Assertion:
    """One assertion a person declared, as declared (ADR 0062).

    ``provenance`` is ``stated`` and cites the assertion where its source writes it (one entry of
    an assertion file). Every value cites its own place or inherits that one.

    - ``identifier``: the id the source gives the assertion, which a retraction names.
    - ``assertion_type``: what it declares; ``retracts`` is the declared id of the assertion a
      ``retract`` withdraws, and ``NotApplicable`` for every other type.
    - ``author``, ``authored_at`` and ``authored_zone``: who declared it and when, as declared.
    - ``scope``: what it is about, in declared order; ``Known(())`` declares nothing in scope.
    - ``payload``, ``rationale``, ``signature``, ``ticket``: optional parts, ``KnownAbsent``
      where the source leaves one out.
    """

    kind: ClassVar[str] = "assertion"
    family: ClassVar[Family] = Family.ASSERTION
    since: ClassVar[int] = ASSERTION_SINCE
    id: RecordId
    provenance: Provenance
    identifier: Knowledge[LogicalId]
    assertion_type: Knowledge[AssertionType]
    author: Knowledge[LogicalId]
    authored_at: Knowledge[Timestamp]
    authored_zone: Knowledge[str]
    scope: Knowledge[tuple[ScopeRef, ...]]
    retracts: Knowledge[LogicalId]
    payload: Knowledge[str]
    rationale: Knowledge[str]
    signature: Knowledge[str]
    ticket: Knowledge[LogicalId]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        if self.provenance.assertion_kind is not AssertionKind.STATED:
            raise ValueError("an assertion is what a person stated: its provenance is stated")
        for name in ("identifier", "author"):
            _check_states(name, getattr(self, name), _DECLARED)
            check_type(name, getattr(self, name), LogicalId)
        _check_states("assertion_type", self.assertion_type, _DECLARED)
        check_type("assertion_type", self.assertion_type, AssertionType)
        _check_states("authored_at", self.authored_at, _DECLARED)
        check_type("authored_at", self.authored_at, Timestamp)
        # A civil time always has a zone; the question is only whether the source states it.
        _check_states("authored_zone", self.authored_zone, _DECLARED)
        check_type("authored_zone", self.authored_zone, str)
        for zone in values_of(self.authored_zone):
            if not is_iana_zone(zone):
                raise ValueError(f"authored_zone is not spelled as an IANA zone name: {zone!r}")
        _check_scope(self.scope)
        self._check_retracts()
        for name in ("payload", "rationale", "signature"):
            _check_states(name, getattr(self, name), _OPTIONAL)
            check_text_values(name, getattr(self, name))
        _check_states("ticket", self.ticket, _OPTIONAL)
        check_type("ticket", self.ticket, LogicalId)

    def _check_retracts(self) -> None:
        """A retract names what it retracts; no other type does; an unstated type may."""
        check_type("retracts", self.retracts, LogicalId)
        match self.assertion_type:
            case Known(value=AssertionType.RETRACT):
                _check_states("retracts", self.retracts, _DECLARED)
            case Known():
                if not isinstance(self.retracts, NotApplicable):
                    raise ValueError("only a retract names an assertion it retracts")
            case _:
                _check_states("retracts", self.retracts, (*_DECLARED, NotApplicable))

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "assertion_type": to_json(self.assertion_type, str),
                "author": to_json(self.author, _logical_json),
                "authored_at": to_json(self.authored_at, Timestamp.to_json),
                "authored_zone": to_json(self.authored_zone),
                "identifier": to_json(self.identifier, _logical_json),
                "payload": to_json(self.payload),
                "rationale": to_json(self.rationale),
                "retracts": to_json(self.retracts, _logical_json),
                "scope": to_json(self.scope, _scope_json),
                "signature": to_json(self.signature),
                "ticket": to_json(self.ticket, _logical_json),
            },
            self.since,
        )


def _knowledge(data: JsonValue, decode: Callable[[JsonValue], Any]) -> Knowledge[Any]:
    return from_json(data, decode, provenance_from_json)


def assertion_from_json(data: JsonValue) -> Assertion:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        Assertion.kind,
        {
            "assertion_type",
            "author",
            "authored_at",
            "authored_zone",
            "identifier",
            "payload",
            "rationale",
            "retracts",
            "scope",
            "signature",
            "ticket",
        },
        Assertion.since,
    )
    return Assertion(
        id=record_id,
        provenance=provenance,
        identifier=_knowledge(obj["identifier"], logical_id_from_json),
        assertion_type=_knowledge(obj["assertion_type"], enum_decoder(AssertionType)),
        author=_knowledge(obj["author"], logical_id_from_json),
        authored_at=_knowledge(obj["authored_at"], timestamp_from_json),
        authored_zone=_knowledge(obj["authored_zone"], text_decoder("authored_zone")),
        scope=_knowledge(obj["scope"], _scope_from_json),
        retracts=_knowledge(obj["retracts"], logical_id_from_json),
        payload=_knowledge(obj["payload"], text_decoder("payload")),
        rationale=_knowledge(obj["rationale"], text_decoder("rationale")),
        signature=_knowledge(obj["signature"], text_decoder("signature")),
        ticket=_knowledge(obj["ticket"], logical_id_from_json),
    )
