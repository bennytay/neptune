"""Declared candidates: what only looks like a task or world declaration (ADR 0063 §7).

The declared-records pass (``neptune.declared``) makes task and world records from explicit
declarations only. Where a block or table looks like one without saying so (a numbered heading
in a procedure that is not labelled ``Step``, a sentence with ``shall`` that carries no
requirement label, a CSV whose undeclared first row reads like a register's header), the pass
writes a ``declared_candidate`` line here instead: inferred, with the rule that fired and its
confidence, pointing at the parsed record it read. Nothing reads a candidate as a fact; a user
who agrees states it (a label, a manifest's ``csv_header``) and the next ingest makes the record.
"""

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final

from neptune.derived.provenance import DERIVED_SCHEMA_VERSION, INFERRED, InferredProvenance
from neptune.derived.provenance import derived_object as _derived_object
from neptune.identity.ids import record_id
from neptune.model._fields import json_array, json_str
from neptune.model.ids import RecordId, check_text, check_token, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json

if TYPE_CHECKING:
    from collections.abc import Mapping

CANDIDATE_KIND: Final = "declared_candidate"


def candidate_id(transform: RecordId, subject: RecordId, proposes: str, rule: str) -> RecordId:
    """One rule proposing one kind for one parsed record, under one transform, is one line."""
    return record_id(
        CANDIDATE_KIND,
        {"proposes": proposes, "rule": rule, "subject": subject, "transform": transform},
    )


@dataclass(frozen=True)
class DeclaredCandidate:
    """A parsed record that may hold a declaration of kind ``proposes``, by ``rule``.

    ``subject`` is the ``DocumentBlock`` or ``StructuredTable`` the rule read; ``text`` is the
    matched text, verbatim; ``evidence`` cites it. ``confidence`` is the rule's, in ``(0, 1]``.
    """

    kind: ClassVar[str] = CANDIDATE_KIND
    id: RecordId
    transform: RecordId
    subject: RecordId
    proposes: str
    rule: str
    confidence: float
    text: str
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        parse_record_id(self.subject)
        check_token("proposes", self.proposes)
        check_token("rule", self.rule)
        if self.id != candidate_id(self.transform, self.subject, self.proposes, self.rule):
            raise ValueError(f"{self.id} is not the id of this candidate")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, float)
            or not math.isfinite(self.confidence)
            or not 0.0 < self.confidence <= 1.0
        ):
            raise ValueError(f"confidence is a float in (0, 1], got {self.confidence!r}")
        if not isinstance(self.text, str):
            raise TypeError(f"text must be a str, got {type(self.text).__name__}")
        check_text("text", self.text)
        InferredProvenance(self.evidence, self.transform)  # checks the evidence

    @property
    def assertion_kind(self) -> str:
        return INFERRED

    @property
    def provenance(self) -> InferredProvenance:
        return InferredProvenance(self.evidence, self.transform)

    def to_json(self) -> JsonObject:
        return {
            "assertion_kind": INFERRED,
            "confidence": self.confidence,
            "evidence": [ref.to_json() for ref in self.evidence],
            "id": self.id,
            "kind": CANDIDATE_KIND,
            "proposes": self.proposes,
            "rule": self.rule,
            "schema_version": DERIVED_SCHEMA_VERSION,
            "subject": self.subject,
            "text": self.text,
            "transform": self.transform,
        }


def declared_candidate_from_json(data: JsonValue) -> DeclaredCandidate:
    obj: Mapping[str, JsonValue] = _derived_object(
        data,
        CANDIDATE_KIND,
        {"confidence", "evidence", "id", "proposes", "rule", "subject", "text", "transform"},
    )
    confidence = obj["confidence"]
    if not isinstance(confidence, float):
        raise ValueError(f"confidence must be a float, got {confidence!r}")
    return DeclaredCandidate(
        id=parse_record_id(json_str(obj["id"], "id")),
        transform=parse_record_id(json_str(obj["transform"], "transform")),
        subject=parse_record_id(json_str(obj["subject"], "subject")),
        proposes=json_str(obj["proposes"], "proposes"),
        rule=json_str(obj["rule"], "rule"),
        confidence=confidence,
        text=json_str(obj["text"], "text"),
        evidence=tuple(
            evidence_ref_from_json(ref) for ref in json_array(obj["evidence"], "evidence")
        ),
    )
