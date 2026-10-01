"""The claim: Memory's unit of memory, and every edge in the graph (ADR 0002 §2).

A claim says ``subject predicate object`` holds over a valid-time interval, was recorded at a Ledger
transaction, and may since have been superseded. It reuses the compiler's types: ``Timestamp`` for
valid time, ``EvidenceRef`` for evidence, ``Knowledge`` for confidence and units, ``AssertionKind``
for observed and stated, and the compiler's ``inferred`` spelling for inference.

A claim's ``id`` covers what it asserts and who asserted it, not its bookkeeping: ``recorded_at``,
``superseded_at`` and ``supersedes`` are set by the Ledger and the superseding resolver.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING, Literal, NewType, TypeAlias

from neptune.derived.provenance import INFERRED
from neptune.identity.canonical_json import dumps
from neptune.identity.hashing import content_id
from neptune.model.ids import (
    ConfigHash,
    RecordId,
    check_text,
    check_token,
    check_verbatim,
    parse_config_hash,
    parse_record_id,
)
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Inherited,
    Knowledge,
    Known,
    NotApplicable,
    Unknown,
    to_json,
)
from neptune.model.provenance import EvidenceRef
from neptune.model.scalars import NonFinite, real_to_json
from neptune.model.time import Timestamp
from neptune.model.units import Unit
from neptune_memory.schema.interval import OPEN, Interval, LedgerTx, Open, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject, JsonValue

# "claim:sha256:<64 lowercase hex>": a Memory id, kept apart from compiler record ids by prefix.
ClaimId = NewType("ClaimId", str)
_CLAIM_ID = re.compile(r"claim:sha256:[0-9a-f]{64}")


def parse_claim_id(text: str) -> ClaimId:
    if not isinstance(text, str) or not _CLAIM_ID.fullmatch(text):
        raise ValueError(f"not a claim id (want 'claim:sha256:<64 lowercase hex>'): {text!r}")
    return ClaimId(text)


# observed | stated (the compiler's AssertionKind) | "inferred" (the compiler's derived/ spelling).
ClaimAssertionKind: TypeAlias = AssertionKind | Literal["inferred"]


def is_inferred(kind: ClaimAssertionKind) -> bool:
    return not isinstance(kind, AssertionKind)


def _check_assertion_kind(kind: object) -> None:
    if isinstance(kind, AssertionKind):
        return
    if type(kind) is str and kind == INFERRED:
        return
    raise ValueError(f"assertion_kind must be observed, stated or {INFERRED!r}, got {kind!r}")


# --- Objects ----------------------------------------------------------------------------------


class ValueType(StrEnum):
    """What a non-node object is. ``record`` is an Episode-tier reference, not a value."""

    TEXT = "text"
    INTEGER = "integer"
    REAL = "real"  # a finite float or a NonFinite the source wrote (an unlimited joint: inf)
    BOOLEAN = "boolean"
    QUANTITY = "quantity"  # a number with its unit as declared (Known, Unknown or Ambiguous)
    INSTANT = "instant"  # a compiler Timestamp, on its own clock
    RECORD = "record"  # a Ledger record by id (LedgerRecordRef)


LiteralValue: TypeAlias = str | int | bool | float | NonFinite | Timestamp


@dataclass(frozen=True)
class TypedLiteral:
    """A value with its type, and its unit exactly as declared. Units are never converted here.

    ``unit`` is ``NotApplicable`` for every datatype but ``quantity``, where it is ``Known``,
    ``Unknown`` (the source gave none) or ``Ambiguous`` (the declared text has several readings).
    ``5 mm`` and ``0.5 cm`` are different objects; relating them is a derived transform.
    """

    datatype: ValueType
    value: LiteralValue
    unit: Knowledge[Unit] = field(default_factory=NotApplicable)

    def __post_init__(self) -> None:
        datatype, value = self.datatype, self.value
        if not isinstance(datatype, ValueType) or datatype is ValueType.RECORD:
            raise ValueError(
                f"a literal's datatype is a value type other than record: {datatype!r}"
            )
        ok = {
            ValueType.TEXT: lambda: isinstance(value, str),
            ValueType.INTEGER: lambda: _is_int(value),
            ValueType.REAL: lambda: _is_real(value),
            ValueType.BOOLEAN: lambda: isinstance(value, bool),
            ValueType.QUANTITY: lambda: _is_int(value) or _is_real(value),
            ValueType.INSTANT: lambda: isinstance(value, Timestamp),
        }[datatype]()
        if not ok:
            raise TypeError(f"{value!r} is not a {datatype} value")
        if isinstance(value, str):
            check_verbatim("value", value)
        if datatype is ValueType.QUANTITY:
            if not isinstance(self.unit, Known | Unknown | Ambiguous):
                raise ValueError(f"a quantity's unit is Known, Unknown or Ambiguous: {self.unit!r}")
            readings = (
                [self.unit]
                if isinstance(self.unit, Known)
                else list(self.unit.candidates)
                if isinstance(self.unit, Ambiguous)
                else []
            )
            if not all(isinstance(reading.value, Unit) for reading in readings):
                raise TypeError(f"a quantity's unit must be a Unit: {self.unit!r}")
            # The claim's provenance grounds the unit; a unit-level citation would make two equal
            # declared values compare unequal.
            slots = [r.provenance for r in readings]
            if isinstance(self.unit, Unknown):
                slots.append(self.unit.provenance)
            if not all(isinstance(slot, Inherited) for slot in slots):
                raise ValueError("a literal's unit inherits the claim's provenance (INHERITED)")
        elif not isinstance(self.unit, NotApplicable):
            raise ValueError(f"only a quantity has a unit; a {datatype} has NotApplicable")

    def to_json(self) -> JsonObject:
        value = self.value
        encoded: JsonValue
        if isinstance(value, Timestamp):
            encoded = value.to_json()
        elif isinstance(value, float | NonFinite):
            encoded = real_to_json(value)
        else:
            encoded = value
        return {
            "datatype": str(self.datatype),
            "kind": "literal",
            "unit": to_json(self.unit, Unit.to_json),
            "value": encoded,
        }


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_real(value: object) -> bool:
    return isinstance(value, NonFinite) or (isinstance(value, float) and math.isfinite(value))


@dataclass(frozen=True)
class LedgerRecordRef:
    """An Episode-tier record by its compiler record id: referenced, never copied."""

    record_id: RecordId

    def __post_init__(self) -> None:
        parse_record_id(self.record_id)

    def to_json(self) -> JsonObject:
        return {"kind": "record", "record_id": self.record_id}


ClaimObject: TypeAlias = NodeRef | TypedLiteral | LedgerRecordRef


def object_type(obj: ClaimObject) -> NodeType | ValueType:
    """The type a predicate's range is checked against."""
    if isinstance(obj, NodeRef):
        return obj.node_type
    if isinstance(obj, TypedLiteral):
        return obj.datatype
    return ValueType.RECORD


# --- Provenance -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimProvenance:
    """What a claim rests on and what produced it.

    - ``evidence``: the source bytes cited, in the order the consolidator read them; at least one.
    - ``records``: the Ledger records (Episode tier) the consolidator read, unique and sorted.
    - ``consolidator_id`` / ``consolidator_version`` / ``config_hash``: the transform. The id
      ``memory.supersede`` is reserved for the resolver's closure versions (``supersede.py``).
    """

    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]
    consolidator_id: str
    consolidator_version: str
    config_hash: ConfigHash

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, tuple) or not self.evidence:
            raise ValueError("a claim cites at least one EvidenceRef")
        for ref in self.evidence:
            if not isinstance(ref, EvidenceRef):
                raise TypeError(f"evidence must be EvidenceRefs, got {type(ref).__name__}")
        if len(set(self.evidence)) != len(self.evidence):
            raise ValueError("evidence refs repeat")
        if not isinstance(self.records, tuple):
            raise TypeError("records must be a tuple of record ids")
        for record in self.records:
            parse_record_id(record)
        if list(self.records) != sorted(set(self.records)):
            raise ValueError("records must be unique and sorted")
        check_token("consolidator_id", self.consolidator_id)
        check_text("consolidator_version", self.consolidator_version)
        parse_config_hash(self.config_hash)

    def to_json(self) -> JsonObject:
        return {
            "config_hash": self.config_hash,
            "consolidator_id": self.consolidator_id,
            "consolidator_version": self.consolidator_version,
            "evidence": [ref.to_json() for ref in self.evidence],
            "records": list(self.records),
        }


# --- The claim --------------------------------------------------------------------------------

CLAIM_ID_SCHEME = "neptune-memory.claim-id/1"


@dataclass(frozen=True)
class Claim:
    """``subject predicate object`` over ``[valid_from, valid_to)``, as recorded at ``recorded_at``.

    Field contract (ADR 0002 §2):

    - ``subject``: a node. ``predicate``: a registered predicate name (``predicates.py``).
    - ``object``: a node (the claim is an edge), a ``TypedLiteral``, or a ``LedgerRecordRef``.
    - ``valid_from`` / ``valid_to``: valid time on one clock, half-open; ``valid_to`` may be
      ``OPEN``.
    - ``recorded_at`` / ``superseded_at``: Ledger transaction time; ``superseded_at`` is ``OPEN``
      while this version is current.
    - ``assertion_kind``: observed | stated | inferred.
    - ``confidence``: ``NotApplicable`` for observed and stated (deterministic) claims;
      ``Known(p)`` with ``0 <= p <= 1`` or ``Unknown`` for inferred ones.
    - ``provenance``: evidence refs, Ledger records, consolidator id + version + config hash.
    - ``supersedes``: ids of the claims this version superseded, unique and sorted.
    """

    subject: NodeRef
    predicate: str
    object: ClaimObject
    valid_from: Timestamp
    valid_to: Timestamp | Open
    recorded_at: LedgerTx
    assertion_kind: ClaimAssertionKind
    confidence: Knowledge[float]
    provenance: ClaimProvenance
    superseded_at: LedgerTx | Open = OPEN
    supersedes: tuple[ClaimId, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.subject, NodeRef):
            raise TypeError(f"subject must be a NodeRef, got {type(self.subject).__name__}")
        check_token("predicate", self.predicate)
        if not isinstance(self.object, NodeRef | TypedLiteral | LedgerRecordRef):
            raise TypeError(f"object must be a node, literal or record ref: {self.object!r}")
        Interval(self.valid_from, self.valid_to)
        ledger_tx(self.recorded_at)
        if not isinstance(self.superseded_at, Open):
            ledger_tx(self.superseded_at)
            if self.superseded_at < self.recorded_at:
                raise ValueError("superseded_at precedes recorded_at")
        _check_assertion_kind(self.assertion_kind)
        _check_confidence(self.assertion_kind, self.confidence)
        if not isinstance(self.provenance, ClaimProvenance):
            raise TypeError(f"provenance must be a ClaimProvenance: {self.provenance!r}")
        if not isinstance(self.supersedes, tuple):
            raise TypeError("supersedes must be a tuple of claim ids")
        for claim_id in self.supersedes:
            parse_claim_id(claim_id)
        if list(self.supersedes) != sorted(set(self.supersedes)):
            raise ValueError("supersedes must be unique and sorted")
        if self.id in self.supersedes:
            raise ValueError("a claim cannot supersede itself")

    @property
    def valid(self) -> Interval:
        return Interval(self.valid_from, self.valid_to)

    @property
    def is_current(self) -> bool:
        """Not superseded in transaction time (it may still have ended in valid time)."""
        return isinstance(self.superseded_at, Open)

    def content_json(self) -> JsonObject:
        """What the claim asserts and who asserted it: the input its id is derived from."""
        return {
            "assertion_kind": str(self.assertion_kind),
            "confidence": to_json(self.confidence),
            "object": self.object.to_json(),
            "predicate": self.predicate,
            "provenance": self.provenance.to_json(),
            "subject": self.subject.to_json(),
            "valid": self.valid.to_json(),
        }

    @cached_property
    def id(self) -> ClaimId:
        payload: JsonObject = {"claim": self.content_json(), "scheme": CLAIM_ID_SCHEME}
        return ClaimId("claim:" + content_id(dumps(payload)))

    def to_json(self) -> JsonObject:
        return {
            **self.content_json(),
            "id": self.id,
            "recorded_at": self.recorded_at,
            "superseded_at": self.superseded_at
            if not isinstance(self.superseded_at, Open)
            else self.superseded_at.to_json(),
            "supersedes": list(self.supersedes),
        }


def _check_confidence(kind: ClaimAssertionKind, confidence: object) -> None:
    if not is_inferred(kind):
        if not isinstance(confidence, NotApplicable):
            raise ValueError(f"a {kind} claim is deterministic: confidence is NotApplicable")
        return
    if isinstance(confidence, Unknown):
        return
    if not isinstance(confidence, Known):
        raise ValueError(f"an inferred claim's confidence is Known or Unknown: {confidence!r}")
    value = confidence.value
    if not isinstance(value, float) or not 0.0 <= value <= 1.0:
        raise ValueError(f"confidence must be a float in [0, 1]: {value!r}")
